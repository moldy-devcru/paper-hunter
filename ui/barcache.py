"""Bar cache — a MUTABLE, rebuildable sqlite store of fetched bars.

Why this file is allowed to be mutable while everything else is append-only
-------------------------------------------------------------------------
``journal.db`` and ``ivrank.db`` are the experiment. This is not: it is a cache of a
public market-data feed, and the spec puts it outside the append-only zone on purpose
(``docs/ui-design.md``: "it's a CACHE, not a record; rebuildable by re-fetching").
Deleting ``data/barcache.db`` loses nothing a re-fetch cannot restore in one request.
It is named ``barcache.db`` rather than ``bars.db`` for the same reason, and it lives
in ``data/`` beside the IV store rather than inside the journal.

Cache-first, never re-pull held history
---------------------------------------
``BarCache.bars()`` reads the requested window out of sqlite first and then fetches
**only the gap**: from one step past the newest cached bar to the end of the window.
A second call for the same window issues zero HTTP requests, which is the property the
offline test pins with a counting ``MockTransport``.

The end of the gap is clamped to ``now - 15 minutes`` when the feed is SIP. That is
Alpaca's documented free-tier floor (a SIP historical query with a fresher ``end`` is
rejected with 42210000), and it is the same rule the executor encodes in
:func:`executor.alpaca_client.sip_end_is_queryable`. We do not pretend the newest
fifteen minutes are consolidated; we simply do not ask for them and let the API stamp
what it has.

Provenance is a column, not a key
---------------------------------
The primary key is ``(symbol, timeframe, t)`` and ``feed`` is a column that the newest
fetch covering that bar overwrites. Storing separate rows per feed would mean the
chart had to choose a feed, and the honest picture of a live session is a series that
*is* mixed — consolidated history with an IEX-only tail. One row per timestamp with
the latest provenance attached, and :func:`ui.aggregate.worse_feed` downgrading any
bucket that mixes the two, is the representation that tells the truth.

# INTERPRETATION: daily bars get the same 15-minute SIP clamp as intraday. Alpaca's
# rule is about SIP history generally rather than about intraday specifically, and a
# not-yet-final daily bar is not worth a 422. The clamp is a no-op for historical
# windows, which is every window the UI actually opens.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from executor.alpaca_client import (
    KEY_ENV,
    RATE_LIMIT_CALLS,
    RATE_LIMIT_WINDOW_SECONDS,
    SECRET_ENV,
    AlpacaClient,
    Bar,
    RateLimiter,
    Transport,
    UrllibTransport,
    sip_end_is_queryable,
)
from ui.aggregate import AggBar

#: Default cache location. ``data/`` is where the IV store already lives, and
#: ``*.db`` is gitignored, so a stale cache is never committed by accident.
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "barcache.db"

#: Alpaca wire timeframes this cache stores. Everything the chart shows is derived
#: from one of these two by ``ui.aggregate``; we never cache a derived bucket.
CACHE_TIMEFRAMES: tuple[str, ...] = ("1Min", "1Day")

#: Step between consecutive bars, used to compute where the gap starts.
_TIMEFRAME_STEP: dict[str, dt.timedelta] = {
    "1Min": dt.timedelta(minutes=1),
    "1Day": dt.timedelta(days=1),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol     TEXT    NOT NULL,
    timeframe  TEXT    NOT NULL,
    t          TEXT    NOT NULL,          -- UTC ISO-8601 with 'Z', bar START
    o          REAL    NOT NULL,
    h          REAL    NOT NULL,
    l          REAL    NOT NULL,
    c          REAL    NOT NULL,
    v          REAL    NOT NULL,
    n          INTEGER,
    vw         REAL,
    feed       TEXT    NOT NULL,          -- provenance of the newest fetch covering t
    fetched_at TEXT    NOT NULL,          -- UTC ISO-8601, when we learned it
    PRIMARY KEY (symbol, timeframe, t)
);
CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars (symbol, timeframe, t);

-- Fetch bookkeeping. Purely diagnostic (how stale is the tail, what was asked for);
-- nothing in the read path depends on it, so losing it costs nothing.
CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT    NOT NULL,
    timeframe  TEXT    NOT NULL,
    feed       TEXT    NOT NULL,
    start      TEXT,
    end        TEXT,
    bars       INTEGER NOT NULL,
    fetched_at TEXT    NOT NULL
);
"""


def _iso(moment: dt.datetime) -> str:
    """UTC ISO-8601 with a trailing ``Z`` — the journal's timestamp convention."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _as_dt(value: str | dt.datetime | None, *, default: dt.datetime) -> dt.datetime:
    if value is None:
        return default
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return _parse(value)


class BarCache:
    """SQLite-backed bar cache with gap-only fetching.

    The Alpaca client is injected, so every test in this repo runs with a
    ``MockTransport`` and never opens a socket. ``client=None`` means "cache only" —
    the mode the FastAPI app uses when no credentials are configured, so a LAN browser
    still sees whatever history the cache already holds instead of a 500.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        client: AlpacaClient | Any | None = None,
        feed: str = "sip",
        now: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        db_path:
            Cache file. ``None`` uses ``data/barcache.db``.
        client:
            An ``AlpacaClient`` (already carrying a ``RateLimiter``), any object with
            a compatible ``get_intraday_bars``/``get_daily_bars``, or ``None`` for
            cache-only operation.
        feed:
            Provenance to request and to stamp. ``"sip"`` is the only feed that yields
            consolidated intraday history on the free tier.
        now:
            Injectable clock (``() -> datetime``) so tests are deterministic. Defaults
            to the real UTC clock.
        """
        self.path = Path(db_path) if db_path is not None else DEFAULT_CACHE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.feed = feed
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        # check_same_thread=False because the server is multi-threaded: uvicorn runs
        # sync endpoints on a worker pool and the cache is one long-lived, shared
        # handle by design. busy_timeout covers the other-process case (the executor's
        # own scripts touching the same file), and WAL keeps a reader from blocking the
        # writer.
        #
        # check_same_thread=False is NECESSARY but NOT SUFFICIENT, and the U2 browser
        # smoke found out which one it was not sufficient about. A sqlite3 Connection
        # is not safe for concurrent use even when the C library is compiled SERIALIZED
        # (which this one is: sqlite3.threadsafety == 3): SERIALIZED protects the
        # database handle, while CPython's Connection object still shares one statement
        # cache and one implicit-transaction state across threads. Interleaved
        # execute()/fetchall() from a worker pool therefore hands a reader another
        # thread's half-stepped rows — reproduced at 24 HTTP 500s per 1200 concurrent
        # requests, all of them `IndexError` or silently short result sets (a 300-bar
        # read coming back with 138). So every use of the connection goes through
        # _db() below, which holds an RLock for the duration of the statement.
        #
        # The lock is deliberately NOT held across a network fetch: bars() calls
        # _fetch() outside any _db() block, so one slow Alpaca request cannot block a
        # chart read. RLock rather than Lock because the write paths call the read
        # helpers.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        #: Number of HTTP fetches this cache instance has issued. Tests assert on it.
        self.fetch_calls = 0

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """The one supported way to touch :attr:`conn`: serialized, see __init__."""
        with self._lock:
            yield self.conn

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> BarCache:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def row_count(self, *, symbol: str | None = None, timeframe: str | None = None) -> int:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if timeframe:
            clauses.append("timeframe = ?")
            params.append(timeframe)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM bars{where}", params).fetchone()
        return int(row["n"])

    def last_cached(self, symbol: str, timeframe: str) -> dt.datetime | None:
        """Newest bar start held for ``(symbol, timeframe)``, or ``None`` if empty."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT MAX(t) AS t FROM bars WHERE symbol = ? AND timeframe = ?",
                (symbol.upper(), timeframe),
            ).fetchone()
        return _parse(row["t"]) if row and row["t"] else None

    # -- reads -----------------------------------------------------------------

    def stored(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> list[AggBar]:
        """Bars already in the cache for a window, oldest-first, as ``AggBar``s."""
        clauses = ["symbol = ?", "timeframe = ?"]
        params: list[Any] = [symbol.upper(), timeframe]
        if start is not None:
            clauses.append("t >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("t <= ?")
            params.append(_iso(end))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT * FROM bars WHERE {' AND '.join(clauses)} ORDER BY t ASC", params
            ).fetchall()
        return [self._to_agg(row) for row in rows]
    def series(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> list[AggBar]:
        """Alias of :meth:`stored` — the shape ``ui.aggregate`` consumes."""
        return self.stored(symbol, timeframe, start=start, end=end)

    @staticmethod
    def _to_agg(row: sqlite3.Row) -> AggBar:
        return AggBar(
            t=_parse(row["t"]),
            o=float(row["o"]),
            h=float(row["h"]),
            l=float(row["l"]),
            c=float(row["c"]),
            v=float(row["v"]),
            n=int(row["n"]) if row["n"] is not None else None,
            vw=float(row["vw"]) if row["vw"] is not None else None,
            feed=str(row["feed"]),
        )

    # -- writes ----------------------------------------------------------------

    def put(
        self,
        symbol: str,
        timeframe: str,
        bars: Iterable[Bar | AggBar],
        *,
        feed: str,
    ) -> int:
        """Store bars, replacing any existing row with the same ``(symbol, tf, t)``.

        Upsert on ``(symbol, timeframe, t)`` and overwrite ``feed``: a bar that was
        IEX-partial a moment ago and consolidated now is one bar that became better,
        not two bars. See the module docstring.
        """
        stamp = _iso(self._now())
        rows = []
        for bar in bars:
            agg = bar if isinstance(bar, AggBar) else self._from_alpaca(bar)
            rows.append(
                (
                    symbol.upper(),
                    timeframe,
                    _iso(agg.t),
                    agg.o,
                    agg.h,
                    agg.l,
                    agg.c,
                    agg.v,
                    agg.n,
                    agg.vw,
                    feed,
                    stamp,
                )
            )
        with self._db() as conn:
            conn.executemany(
                """
                INSERT INTO bars (symbol, timeframe, t, o, h, l, c, v, n, vw, feed, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, timeframe, t) DO UPDATE SET
                    o = excluded.o, h = excluded.h, l = excluded.l, c = excluded.c,
                    v = excluded.v, n = excluded.n, vw = excluded.vw,
                    feed = excluded.feed, fetched_at = excluded.fetched_at
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    @staticmethod
    def _from_alpaca(bar: Bar) -> AggBar:
        return AggBar(
            t=bar.t,
            o=bar.o,
            h=bar.h,
            l=bar.l,
            c=bar.c,
            v=bar.v,
            n=bar.n,
            vw=bar.vw,
        )

    def _log_fetch(
        self,
        symbol: str,
        timeframe: str,
        feed: str,
        start: dt.datetime | None,
        end: dt.datetime | None,
        count: int,
    ) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO fetch_log (symbol, timeframe, feed, start, end, bars, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol.upper(),
                    timeframe,
                    feed,
                    _iso(start) if start else None,
                    _iso(end) if end else None,
                    count,
                    _iso(self._now()),
                ),
            )
            conn.commit()

    def fetch_log(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    # -- the cache-first read path ---------------------------------------------

    def bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | str | None = None,
        end: dt.datetime | str | None = None,
        feed: str | None = None,
        fetch: bool = True,
    ) -> list[AggBar]:
        """Cached bars for a window, fetching only the gap if there is one.

        Parameters
        ----------
        timeframe:
            ``"1Min"`` or ``"1Day"`` — a raw wire timeframe. Higher timeframes are
            derived by :mod:`ui.aggregate`, never cached.
        fetch:
            ``False`` makes this a pure cache read (the API's ``?fetch=false`` mode
            and every offline test).
        """
        if timeframe not in CACHE_TIMEFRAMES:
            raise ValueError(f"barcache stores {CACHE_TIMEFRAMES}, not {timeframe!r}")
        requested_feed = feed or self.feed
        window_start = _as_dt(start, default=dt.datetime(1970, 1, 1, tzinfo=dt.UTC))
        window_end = _as_dt(end, default=self._now())

        cached = self.stored(symbol, timeframe, start=window_start, end=window_end)
        if not fetch or self.client is None:
            return cached

        gap_start, gap_end = self._gap(
            symbol,
            timeframe,
            window_start=window_start,
            window_end=window_end,
            feed=requested_feed,
            cached=cached,
        )
        if gap_start is None or gap_end is None:
            return cached
        fetched = self._fetch(symbol, timeframe, requested_feed, gap_start, gap_end)
        self.fetch_calls += 1
        self._log_fetch(symbol, timeframe, requested_feed, gap_start, gap_end, len(fetched))
        if not fetched:
            # Nothing new upstream. Re-read the window so a caller that asked for a
            # window that ends mid-session still gets the union of old and new.
            return self.stored(symbol, timeframe, start=window_start, end=window_end)
        self.put(symbol, timeframe, fetched, feed=requested_feed)
        return self.stored(symbol, timeframe, start=window_start, end=window_end)

    def _gap(
        self,
        symbol: str,
        timeframe: str,
        *,
        window_start: dt.datetime,
        window_end: dt.datetime,
        feed: str,
        cached: Sequence[AggBar],
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """``(gap_start, gap_end)`` to fetch, or ``(None, None)`` when there is none."""
        step = _TIMEFRAME_STEP[timeframe]
        gap_start = window_start
        if cached:
            gap_start = max(gap_start, cached[-1].t + step)
        gap_end = window_end
        if feed.lower() == "sip":
            # The free tier will reject a SIP query whose end is fresher than 15
            # minutes; ask for what we are allowed to ask for and stamp the rest.
            clamped = self._now() - dt.timedelta(minutes=15)
            if not sip_end_is_queryable(gap_end, now=self._now()):
                gap_end = clamped
        if gap_start >= gap_end:
            return None, None
        return gap_start, gap_end

    def _fetch(
        self,
        symbol: str,
        timeframe: str,
        feed: str,
        start: dt.datetime,
        end: dt.datetime,
    ) -> list[AggBar]:
        """One bar request through the injected client, honouring its rate limiter."""
        params = {"start": _iso(start), "end": _iso(end), "feed": feed}
        if timeframe == "1Day":
            payload = self._client_call("get_daily_bars", symbol, params)
        else:
            payload = self._client_call(
                "get_intraday_bars", symbol, {**params, "timeframe": "1Min"}
            )
        bars = list(getattr(payload, "bars", payload) or [])
        return [self._from_alpaca(bar) for bar in bars]

    def _client_call(self, method: str, symbol: str, params: dict[str, Any]) -> Any:
        fn = getattr(self.client, method, None)
        if fn is None:
            raise TypeError(f"injected client has no {method}()")
        return fn(symbol, **params)

    # -- diagnostics -----------------------------------------------------------

    def provenance_summary(self) -> dict[str, Any]:
        """Per-symbol/per-timeframe bar counts and the worst feed we are holding.

        ``/api/health`` reports this so the chart's "SIP consolidated" vs "IEX
        partial" badge is backed by the cache rather than by a hopeful constant.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT symbol, timeframe, feed, COUNT(*) AS n, MIN(t) AS first, MAX(t) AS last"
                " FROM bars GROUP BY symbol, timeframe, feed"
            ).fetchall()
        by_series: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = f"{row['symbol']}/{row['timeframe']}"
            entry = by_series.setdefault(
                key,
                {
                    "symbol": row["symbol"],
                    "timeframe": row["timeframe"],
                    "bars": 0,
                    "first": row["first"],
                    "last": row["last"],
                    "feeds": {},
                },
            )
            entry["bars"] += int(row["n"])
            entry["feeds"][row["feed"]] = int(row["n"])
            entry["first"] = min(entry["first"], row["first"])
            entry["last"] = max(entry["last"], row["last"])
        return {
            "path": str(self.path),
            "series": sorted(by_series.values(), key=lambda e: (e["symbol"], e["timeframe"])),
            "fetch_calls": self.fetch_calls,
        }


def default_client(transport: Transport | None = None) -> AlpacaClient:
    """An ``AlpacaClient`` with the free-tier ``RateLimiter`` attached.

    Constructed lazily and explicitly rather than at import time: importing
    ``ui.barcache`` must never require credentials, or the test suite (and the
    ``uvicorn ui.api:app`` smoke run) would fail on a machine with no keys.
    """
    return AlpacaClient(
        transport=transport or UrllibTransport(),
        key=os.environ.get(KEY_ENV, ""),
        secret=os.environ.get(SECRET_ENV, ""),
        rate_limiter=RateLimiter(
            max_calls=RATE_LIMIT_CALLS,
            window_seconds=RATE_LIMIT_WINDOW_SECONDS,
        ),
    )


def open_cache(
    db_path: str | Path | None = None,
    *,
    with_client: bool = True,
    transport: Transport | None = None,
) -> BarCache:
    """Cache with a live client when credentials exist, cache-only when they do not.

    No credentials is a normal state for a read-only research box, not an error: the
    API serves whatever is already cached and reports ``feed_provenance: "cache-only"``
    instead of raising on the first request.
    """
    have_keys = bool(os.environ.get(KEY_ENV)) and bool(os.environ.get(SECRET_ENV))
    client = default_client(transport) if (with_client and have_keys) else None
    return BarCache(db_path, client=client)


__all__ = [
    "BarCache",
    "CACHE_TIMEFRAMES",
    "DEFAULT_CACHE_PATH",
    "default_client",
    "open_cache",
]
