"""Self-built IV-rank store (T5) — SQLite, no free point-in-time source exists.

Why this module exists
----------------------
The research note (``docs/research/2026-10-02-options-data-sources.md`` §1) is blunt
about it: Alpaca serves IV only as a *current* snapshot field, historical option data
starts February 2024, Tradier and Massive have no point-in-time Greeks/IV either. So
**nobody gives us IV history for free** — we have to accumulate it ourselves, one EOD
poll per day, from before the window opens. This module is that accumulator, and its
whole design is about not lying about how much history it actually has.

The honesty rules, in priority order
------------------------------------
1. **Warmup is a real state, not an error and not zero.** Below ``min_observations``
   (default 60) the store returns ``None`` with status ``"warmup"``. The checklist's
   T5 turns a ``None`` IV rank into PENDING, which blocks rather than passes, so an
   under-built history can never wave a trade through.
2. **Proxy data is flagged, always.** The Cboe VIX daily CSV is a *different index*
   from SPY IV — it is a seed, not a substitute. Rows carry ``is_proxy``; the default
   computation **excludes** proxy rows. A caller has to ask for them explicitly, and
   the result then carries ``proxy_included=True`` plus ``status="proxy"`` so the
   journal can see it. Mixing VIX into a SPY IV rank without that flag would be the
   single most dishonest thing this layer could do.
3. **Tenor is part of the key.** An IV rank for a 7-DTE 650 call and a 180-DTE 400
   call are different statistics. The store keys on ``(underlying, expiry, right,
   strike_bucket)`` via an explicit ``tenor_key`` so a caller cannot accidentally rank
   one contract's IV against another's history.

Storage choice: **separate database** (``data/ivrank.db``), not a table in the
journal DB. The journal is an append-only, immutability-enforced experiment ledger
with trigger-protected UPDATE/DELETE; an IV store is a *mutable* time series that gets
re-read and re-scored. Mixing them would force a mutable, overwritable table to live
underneath triggers written for a different purpose. Separating them also means a
corrupt IV rebuild can never touch the experiment record.

Lookback: 365 calendar days default (brief: "IV rank = ~1y history", rulebook
``t5_options_chain.arm_b.iv_rank_lookback: "1y"``).

Percentile convention: percentage of window observations **strictly below** the
current value, times 100 — the same strict-below convention as
``indicators.bandwidth_percentile``, so "IV rank < 50" means the same kind of thing
in both places. Ties are not counted as below.

Python 3.12+, stdlib sqlite3 only.
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "ivrank.db"

#: Minimum stored observations before IV rank is reported as defined. 60 daily
#: observations is ~3 months of trading days — not a year, and NOT a substitute for
#: one. Chosen because it is the smallest window where a percentile rank is not
#: dominated by a handful of readings, and because the checklist treats an undefined
#: rank as PENDING so waiting costs nothing but trades.
MIN_OBSERVATIONS = 60

#: Default lookback. Matches the rulebook's ``iv_rank_lookback: "1y"``.
DEFAULT_LOOKBACK_DAYS = 365

IVSource = Literal["alpaca_chain", "cboe_vix_csv", "manual"]
IVRankStatus = Literal["ok", "warmup", "no_history", "empty_window"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS iv_observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    underlying    TEXT    NOT NULL,
    as_of         TEXT    NOT NULL,          -- YYYY-MM-DD, the observation date (UTC)
    tenor_key     TEXT    NOT NULL,          -- caller-defined tenor identity
    expiry        TEXT,                       -- YYYYMMDD, null for proxy series
    right         TEXT,                       -- 'call' | 'put', null for proxy series
    strike        REAL,                       -- null for proxy series
    iv            REAL    NOT NULL,
    source        TEXT    NOT NULL,
    is_proxy      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    UNIQUE (underlying, as_of, tenor_key, source)
);
CREATE INDEX IF NOT EXISTS idx_iv_lookup
    ON iv_observations (underlying, tenor_key, is_proxy, as_of);
"""

#: Anything accepted where a date is expected.
DateLike = str | date | datetime

VIX_PROXY_TENOR = "VIX_INDEX_SPOT"
VIX_PROXY_SOURCE = "cboe_vix_csv"
VIX_PROXY_UNDERLYING = "VIX"


class IvRankError(ValueError):
    """Raised on malformed input to the store — a data bug, surfaced loudly."""


@dataclass(frozen=True, slots=True)
class IvObservation:
    """One stored IV reading."""

    underlying: str
    as_of: str                      # YYYY-MM-DD
    tenor_key: str
    iv: float
    source: str
    is_proxy: bool = False
    expiry: str | None = None
    right: str | None = None
    strike: float | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class IvRankResult:
    """Outcome of an IV-rank query.

    ``rank`` is 0-100 or ``None``. ``status``:

      * ``ok``          — defined, real (non-proxy) data only.
      * ``proxy``       — defined, but the window includes VIX proxy rows. Must be
                          visible in the journal; never report this as plain "ok".
      * ``warmup``      — not enough observations to define a rank.
      * ``no_history``  — no rows at all for this tenor.
      * ``empty_window``— rows exist outside the lookback window only.
    """

    rank: float | None
    status: IVRankStatus
    observations: int
    window_days: int
    underlying: str
    tenor_key: str
    lookback_days: int
    is_proxy: bool = False
    proxy_observations: int = 0
    reason: str = ""

    @property
    def defined(self) -> bool:
        return self.rank is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "iv_rank": self.rank,
            "status": self.status,
            "observations": self.observations,
            "window_days": self.window_days,
            "underlying": self.underlying,
            "tenor_key": self.tenor_key,
            "lookback_days": self.lookback_days,
            "is_proxy": self.is_proxy,
            "proxy_observations": self.proxy_observations,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# tenor keys
# ---------------------------------------------------------------------------


def tenor_key(
    *,
    expiry: str,
    right: str,
    strike: float,
    bucket_size: float = 5.0,
) -> str:
    """Stable identity for one option tenor: ``expiry-right-bucket``.

    # INTERPRETATION: strikes are bucketed to ``bucket_size`` (default $5) before
    # keying. A raw-strike key means every strike in a chain accumulates its own
    separate history, so a rank computed for 650.00 says nothing about 652.50 and the
    store stays in warmup for months. Bucketing trades a little precision for an
    actually-populated series. The bucket size is a parameter, not a constant, so it
    can be frozen in the rulebook later without a code change.

    Bucketing rounds **half away from zero** (``floor(x + 0.5)``), not Python's
    built-in ``round``. Built-in ``round`` is round-half-to-*even*, which makes
    bucketing non-monotonic at the edges: 702.50 lands in the 700 bucket, 702.51 in
    the 705 bucket, and 703.90 -- which is nearer 705 -- back in 700. Two strikes a
    few cents apart would end up with different IV histories, and an IV rank stops
    being comparable across the chain it came from.
    """
    if bucket_size <= 0:
        raise IvRankError(f"bucket_size must be > 0, got {bucket_size}")
    side = right.strip().lower()
    if side not in ("call", "put"):
        raise IvRankError(f"right must be 'call' or 'put', got {right!r}")
    bucket = math.floor(strike / bucket_size + 0.5)
    return f"{expiry}-{side}-{bucket * bucket_size:.2f}"


def atm_tenor_key(expiry: str, right: str, strike: float, bucket_size: float = 5.0) -> str:
    """Alias for :func:`tenor_key` that names the ATM use case in the call site."""
    return tenor_key(expiry=expiry, right=right, strike=strike, bucket_size=bucket_size)


def dte_tenor_key(
    *,
    dte: int,
    right: str,
    strike_bucket: float,
) -> str:
    """Rolling tenor key: ``dte<bucket>-<right>-<strike_bucket>``.

    # INTERPRETATION: the EOD soak records BOTH this key and :func:`tenor_key` for
    # every ATM contract it polls, and T5 may read either — the operator picks at
    # ratification. Reason: :func:`tenor_key` includes the calendar expiry, so a
    # tenor's history lives and dies with that expiry. A weekly expiry therefore
    # accrues ~5 observations before it rolls off, and a monthly one ~20, and
    # *neither ever reaches* ``MIN_OBSERVATIONS = 60``. A key that can never leave
    # warmup is a key that makes T5 PENDING forever, which blocks rather than lies —
    # honest, but it also means the IV store we are paying to accumulate every day
    # would never produce a rank at all. The DTE-bucketed key rolls with the
    # underlying instead of the expiry, so it accumulates across expiries and does
    # reach the warmup floor inside one quarter.
    #
    # The trade-off is real and is not hidden: a DTE bucket mixes expiries, so its
    # rank is a statement about "roughly a week out, ATM-ish" rather than about one
    # specific contract. That is the same kind of approximation as bucketing strikes
    # to $5 (which :func:`tenor_key` already does), taken one step further. Storing
    # both keeps the precise-but-never-warm series available for audit, so the
    # approximation can be measured against the exact thing later instead of trusted
    # on faith.
    #
    # ``dte`` is floored to a non-negative integer: a contract that has already
    # expired reads as DTE 0 (0DTE is a real bucket, and it is the one Arm B trades)
    # rather than as a negative key.
    """
    side = right.strip().lower()
    if side not in ("call", "put"):
        raise IvRankError(f"right must be 'call' or 'put', got {right!r}")
    if dte < 0:
        raise IvRankError(f"dte must be >= 0, got {dte}")
    return f"dte{int(dte)}-{side}-{strike_bucket:.2f}"


def strike_bucket(strike: float, bucket_size: float = 5.0) -> float:
    """The $5-bucketed strike a tenor key is built from (same half-away-from-zero rule).

    Exposed so a caller polling a chain does not have to re-implement the rounding to
    discover which bucket a contract belongs to.
    """
    if bucket_size <= 0:
        raise IvRankError(f"bucket_size must be > 0, got {bucket_size}")
    return math.floor(strike / bucket_size + 0.5) * bucket_size


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class IvRankStore:
    """SQLite-backed IV history + percentile rank.

    The connection is injected (like ``journal.store``) so tests get a real database
    in a tmpdir and never share state, and so the module works inside the executor's
    existing connection model.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        min_observations: int = MIN_OBSERVATIONS,
    ) -> None:
        if min_observations < 2:
            raise IvRankError(f"min_observations must be >= 2, got {min_observations}")
        self.conn = conn
        self.min_observations = min_observations
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- construction ----------------------------------------------------------

    @classmethod
    def open(cls, db_path: str | Path | None = None, **kwargs) -> IvRankStore:
        path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return cls(conn, **kwargs)

    # -- writes ----------------------------------------------------------------

    def record(
        self,
        *,
        underlying: str,
        as_of: DateLike,
        tenor: str,
        iv: float,
        source: IVSource = "alpaca_chain",
        is_proxy: bool = False,
        expiry: str | None = None,
        right: str | None = None,
        strike: float | None = None,
    ) -> None:
        """Upsert one observation. Re-recording the same key replaces the value.

        Upsert (not insert-only) because a re-run of the EOD poll for a day we already
        captured should correct the number, not fail or duplicate. The journal is the
        append-only record; this store is a time series, and a stale IV from a
        half-finished poll is worse than a corrected one.

        ``as_of`` accepts ``YYYY-MM-DD`` or a ``date``/``datetime``.
        """
        day = _as_day(as_of)
        # Bound as an ISO string, not a date object: sqlite3's implicit date adapter is
        # deprecated in 3.12 and warns on every write. The column is TEXT either way.
        day_iso = day.isoformat()
        if iv is None or iv <= 0:
            raise IvRankError(f"iv must be a positive number, got {iv!r}")
        if is_proxy and (expiry or right or strike) is not None:
            # A proxy row claiming option tenor fields would let proxy data leak into
            # an option-tenor query. Refuse at the door.
            raise IvRankError(
                "proxy observations must not carry expiry/right/strike — a VIX row is "
                "not an option tenor and must never be selectable as one"
            )
        self.conn.execute(
            """
            INSERT INTO iv_observations
                (underlying, as_of, tenor_key, expiry, right, strike, iv, source,
                 is_proxy, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (underlying, as_of, tenor_key, source) DO UPDATE SET
                iv = excluded.iv,
                expiry = excluded.expiry,
                right = excluded.right,
                strike = excluded.strike,
                is_proxy = excluded.is_proxy,
                created_at = excluded.created_at
            """,
            (
                underlying.upper(),
                day_iso,
                tenor,
                expiry,
                right,
                strike,
                float(iv),
                source,
                1 if is_proxy else 0,
                _now_iso(),
            ),
        )
        self.conn.commit()

    def record_many(self, observations: Sequence[IvObservation]) -> int:
        """Bulk upsert. Returns the number of rows written."""
        for obs in observations:
            self.record(
                underlying=obs.underlying,
                as_of=obs.as_of,
                tenor=obs.tenor_key,
                iv=obs.iv,
                source=obs.source,
                is_proxy=obs.is_proxy,
                expiry=obs.expiry,
                right=obs.right,
                strike=obs.strike,
            )
        return len(observations)

    # -- reads -----------------------------------------------------------------

    def history(
        self,
        underlying: str,
        tenor: str,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        include_proxy: bool = False,
        as_of: DateLike | None = None,
    ) -> list[float]:
        """Stored IV values in the trailing window, oldest-first.

        Excludes proxy rows unless ``include_proxy`` — see the module docstring.
        """
        end_day = _as_day(as_of) if as_of is not None else _today()
        start_day = (end_day - timedelta(days=lookback_days)).isoformat()
        sql = [
            "SELECT iv FROM iv_observations",
            "WHERE underlying = ? AND tenor_key = ? AND as_of >= ? AND as_of <= ?",
        ]
        params: list[object] = [underlying.upper(), tenor, start_day, end_day.isoformat()]
        if not include_proxy:
            sql.append("AND is_proxy = 0")
        sql.append("ORDER BY as_of ASC")
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [float(r["iv"]) for r in rows]

    def count_observations(
        self,
        underlying: str,
        tenor: str,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        include_proxy: bool = False,
        as_of: DateLike | None = None,
    ) -> int:
        return len(
            self.history(
                underlying,
                tenor,
                lookback_days=lookback_days,
                include_proxy=include_proxy,
                as_of=as_of,
            )
        )

    def latest(
        self,
        underlying: str,
        tenor: str,
        *,
        include_proxy: bool = False,
    ) -> IvObservation | None:
        sql = [
            "SELECT * FROM iv_observations WHERE underlying = ? AND tenor_key = ?",
        ]
        params: list[object] = [underlying.upper(), tenor]
        if not include_proxy:
            sql.append("AND is_proxy = 0")
        sql.append("ORDER BY as_of DESC, id DESC LIMIT 1")
        row = self.conn.execute(" ".join(sql), params).fetchone()
        return _observation_from_row(row) if row else None

    def observations(self, underlying: str | None = None) -> list[IvObservation]:
        """Everything stored, for the seeding report and for tests."""
        sql = "SELECT * FROM iv_observations"
        params: list[object] = []
        if underlying:
            sql += " WHERE underlying = ?"
            params.append(underlying.upper())
        sql += " ORDER BY as_of ASC, tenor_key ASC"
        return [_observation_from_row(r) for r in self.conn.execute(sql, params).fetchall()]

    # -- rank ------------------------------------------------------------------

    def iv_rank(
        self,
        current_iv: float | None,
        underlying: str,
        tenor: str,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        include_proxy: bool = False,
        min_observations: int | None = None,
        as_of: DateLike | None = None,
    ) -> IvRankResult:
        """Percentile rank (0-100) of ``current_iv`` within the stored window.

        Returns ``rank=None`` plus a status explaining the shortfall whenever the
        window cannot support a rank. ``current_iv=None`` always yields
        ``status="empty_window"``/``no_history`` — never a zero rank.

        Whether the current reading itself is included in the window: it is **not**.
        We score the present reading against *prior* observations, the same way
        ``indicators.bandwidth_percentile`` excludes the newest value from its own
        comparison window. A series vouching for itself would bias every reading
        toward 50.

        ``min_observations`` overrides the store default so tests can exercise the
        warmup boundary without seeding 60 rows.
        """
        floor = self.min_observations if min_observations is None else min_observations
        if floor < 2:
            raise IvRankError(f"min_observations must be >= 2, got {floor}")

        base = {
            "underlying": underlying.upper(),
            "tenor_key": tenor,
            "lookback_days": lookback_days,
        }

        total_rows = self.count_observations(
            underlying, tenor, lookback_days=10_000, include_proxy=True
        )
        if total_rows == 0:
            return IvRankResult(
                rank=None,
                status="no_history",
                observations=0,
                window_days=0,
                reason=(
                    f"no IV observations stored for {underlying.upper()} {tenor} — "
                    f"build the history with the EOD chain poll before relying on T5"
                ),
                **base,
            )

        window = self.history(
            underlying,
            tenor,
            lookback_days=lookback_days,
            include_proxy=include_proxy,
            as_of=as_of,
        )
        proxy_count = 0
        if include_proxy:
            proxy_count = self.count_observations(
                underlying,
                tenor,
                lookback_days=lookback_days,
                include_proxy=True,
                as_of=as_of,
            ) - len(window)

        if not window:
            return IvRankResult(
                rank=None,
                status="empty_window",
                observations=total_rows,
                window_days=0,
                reason=(
                    f"{total_rows} stored observation(s) exist but none fall inside the "
                    f"{lookback_days}-day window ending "
                    f"{_as_day(as_of).isoformat() if as_of is not None else _today()}"
                ),
                **base,
            )

        if current_iv is None or current_iv <= 0:
            return IvRankResult(
                rank=None,
                status="empty_window",
                observations=len(window),
                window_days=lookback_days,
                reason="no current IV supplied — cannot score a reading that does not exist",
                is_proxy=include_proxy,
                proxy_observations=proxy_count,
                **base,
            )

        if len(window) < floor:
            return IvRankResult(
                rank=None,
                status="warmup",
                observations=len(window),
                window_days=lookback_days,
                reason=(
                    f"warmup: {len(window)} observation(s) in window, need >= {floor} "
                    f"before IV rank is defined (T5 reads undefined as PENDING, which "
                    f"blocks rather than passes)"
                ),
                is_proxy=include_proxy,
                proxy_observations=proxy_count,
                **base,
            )

        below = sum(1 for value in window if value < current_iv)
        rank = 100.0 * below / len(window)
        return IvRankResult(
            rank=rank,
            status="proxy" if include_proxy else "ok",
            observations=len(window),
            window_days=lookback_days,
            is_proxy=include_proxy,
            proxy_observations=proxy_count,
            reason=(
                f"{below}/{len(window)} window observations strictly below current IV "
                f"({current_iv:.4f})"
            ),
            **base,
        )

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# VIX proxy seeding
# ---------------------------------------------------------------------------

VIX_CSV_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


def parse_vix_csv(text: str | IO[str]) -> list[IvObservation]:
    """Parse the free Cboe VIX daily CSV into proxy observations.

    Parsed from *text*, not a URL, so the seeding path is testable offline and this
    module never performs network I/O. Callers fetch ``VIX_CSV_URL`` themselves (or
    pass a downloaded file) — the executor's data layer owns transport.

    Every row produced is ``is_proxy=True`` and carries the tenor
    ``VIX_INDEX_SPOT``. Provenance travels in the row, not in a docstring.
    """
    handle = text if isinstance(text, str) else text.read()
    if isinstance(handle, bytes):  # a binary file object or bytes
        handle = handle.decode("utf-8")
    reader = csv.DictReader(io.StringIO(handle))
    if reader.fieldnames is None or "CLOSE" not in reader.fieldnames:
        raise IvRankError(
            f"VIX CSV must have a CLOSE column; got columns {reader.fieldnames}. "
            f"Expected the Cboe format from {VIX_CSV_URL}"
        )
    out: list[IvObservation] = []
    for row in reader:
        raw_close = (row.get("CLOSE") or "").strip()
        if raw_close in ("", "."):
            continue  # Cboe pads holidays with '.'
        try:
            close = float(raw_close)
        except ValueError:
            continue
        if close <= 0:
            continue
        day_text = (row.get("DATE") or "").strip()
        if not day_text:
            continue
        # The Cboe file uses MM/DD/YYYY, not ISO. Normalised here because every
        # lookback comparison in this module is against an ISO day, and "01/02/2024"
        # sorts and compares as nonsense next to "2024-01-02".
        day: date | None = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                day = datetime.strptime(day_text, fmt).date()
                break
            except ValueError:
                continue
        if day is None:
            continue  # unparseable date -> skip rather than store a broken key
        out.append(
            IvObservation(
                underlying=VIX_PROXY_UNDERLYING,
                as_of=day.isoformat(),
                tenor_key=VIX_PROXY_TENOR,
                iv=close,
                source=VIX_PROXY_SOURCE,
                is_proxy=True,
            )
        )
    if not out:
        raise IvRankError("VIX CSV parsed to zero usable rows — refusing to seed an empty proxy")
    return out


def seed_vix_proxy(
    store: IvRankStore,
    text: str,
    *,
    underlying: str = VIX_PROXY_UNDERLYING,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    today: str | None = None,
) -> int:
    """Backfill VIX rows into the store as flagged proxy data.

    Returns the number of rows written (rows outside ``lookback_days`` are skipped —
    a 1990-2026 CSV backfilled wholesale would make every future window contain 9,000
    observations and give a percentile that looks authoritative and means nothing).

    The rows land under the ``VIX`` underlying with ``is_proxy=1``. They therefore do
    **not** mix into a ``SPY`` tenor's rank unless a caller passes
    ``include_proxy=True`` *and* asks for the VIX tenor explicitly — double
    protection, because the flag and the key both have to be wrong for a real T5 to
    read VIX.
    """
    cutoff = (_as_day(today) if today else _today()) - timedelta(days=lookback_days)
    rows = [
        obs
        for obs in parse_vix_csv(text)
        if obs.underlying == underlying.upper() and _as_day(obs.as_of) >= cutoff
    ]
    return store.record_many(rows)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _as_day(value: DateLike) -> date:
    """Coerce ``YYYY-MM-DD`` / ``date`` / ``datetime`` to a ``datetime.date``."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise IvRankError(f"bad date {value!r}: expected YYYY-MM-DD") from exc
    raise IvRankError(f"cannot interpret {value!r} as a date")


def _today() -> date:
    return datetime.now(UTC).date()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _observation_from_row(row: sqlite3.Row) -> IvObservation:
    return IvObservation(
        underlying=row["underlying"],
        as_of=row["as_of"],
        tenor_key=row["tenor_key"],
        iv=float(row["iv"]),
        source=row["source"],
        is_proxy=bool(row["is_proxy"]),
        expiry=row["expiry"],
        right=row["right"],
        strike=row["strike"],
        created_at=row["created_at"],
    )


def iter_proxy(observations: Sequence[IvObservation]) -> Iterator[IvObservation]:
    """Every flagged-proxy observation in ``observations`` — for reporting/audits."""
    return (obs for obs in observations if obs.is_proxy)


__all__ = [
    "DEFAULT_DB_PATH",
    "DateLike",
    "DEFAULT_LOOKBACK_DAYS",
    "IvObservation",
    "IvRankError",
    "IvRankResult",
    "IVRankStatus",
    "IvRankStore",
    "IVSource",
    "MIN_OBSERVATIONS",
    "VIX_CSV_URL",
    "VIX_PROXY_SOURCE",
    "VIX_PROXY_TENOR",
    "VIX_PROXY_UNDERLYING",
    "atm_tenor_key",
    "dte_tenor_key",
    "strike_bucket",
    "iter_proxy",
    "parse_vix_csv",
    "seed_vix_proxy",
    "tenor_key",
]