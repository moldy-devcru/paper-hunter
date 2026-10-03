"""FastAPI app for the paper-hunter terminal — U1 backend, U2 static shell.

Static assets (``ui/static/``) are served by four explicit GET routes rather than a
``StaticFiles`` mount; see :func:`app_script` for why that matters to the read-only
assertion. No write path reaches any of them.

Read-only by construction
-------------------------
Three independent mechanisms, because "we were careful" is not a property a research
tool should be trusted on:

1. **Every database is opened ``file:...?mode=ro``.** :func:`open_ro` is the only way
   this module gets a connection, and a write against one raises
   ``sqlite3.OperationalError: attempt to write a readonly database`` — a test asserts
   that, so a future edit cannot quietly downgrade it to a normal ``sqlite3.connect``.
2. **Zero non-GET routes.** ``tests/test_ui_api.py`` walks ``app.routes`` and fails if
   any route accepts POST/PUT/PATCH/DELETE. There is no order-placement endpoint and
   there never will be one: the executor is a separate process and the UI is a window,
   not a door (spec: "no order placement from the UI, ever").
3. **The bar cache is the only writable thing here**, and it is a cache of a public
   feed at ``data/barcache.db`` — outside the append-only zone by design, rebuildable
   by re-fetching, and holding no experiment state.

Dogfooding the indicators
-------------------------
``/api/indicators`` imports :mod:`executor.indicators` and calls it. It does not
reimplement an EMA, an RSI or a Bollinger band. What mads sees on the chart is the
same function the checklist evaluated at decision time, to the same bit — the test
asserts equality against a direct call, so a second implementation cannot creep in.

Empty is not an error
---------------------
Every endpoint answers with a structured, explicitly-empty body when the journal has
no rows, when a database file is missing, or when there is not enough history for an
indicator. The experiment's window has not opened yet; a terminal that 500s on an empty
journal is a terminal that looks broken on the most important day of the run.
``status``/``reason``/``sufficient`` fields carry the truth; nothing is defaulted to a
zero that could be mistaken for a measurement.

# INTERPRETATION: ``/api/bars`` returns an envelope, not a bare list. The spec sketches
``[{t,o,h,l,c,v,feed}]``; the response is ``{symbol, timeframe, bars: [...], excluded,
partial_buckets, feed_provenance, ...}`` because the pre/post-market exclusion count,
the partial-last-bucket flag and the per-bar provenance stamp have nowhere else to live
in a bare array, and dropping them would mean the chart silently lies about coverage.
``bars`` is exactly the spec's array, unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import urllib.parse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from analysis import rollup
from config.loader import DEFAULT_RULES_PATH, RulesError, load_rules
from executor import indicators
from executor.calibration import flow_calibration
from ui import aggregate
from ui.aggregate import AggBar
from ui.barcache import BarCache, open_cache

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_PAGE = STATIC_DIR / "index.html"
APP_SCRIPT = STATIC_DIR / "app.js"
APP_STYLES = STATIC_DIR / "style.css"
VENDOR_BUNDLE = STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js"
#: Retained as an alias for the U1 name; the page it pointed at is now the real one.
PLACEHOLDER_PAGE = INDEX_PAGE


def _static_file(path: Path, media_type: str) -> FileResponse:
    """Serve one known static file, or a 404 that says which file is missing.

    Named explicitly per file rather than by path parameter: a path parameter would be
    a directory-walk surface, and this server's whole posture is "the smallest set of
    routes that can exist".
    """
    if not path.exists():  # pragma: no cover - every file ships with the module
        raise HTTPException(status_code=404, detail=f"{path.name} is missing from the package")
    # no-cache (revalidate, don't blindly reuse) rather than no-store: this is a local
    # tool that gets edited, and a browser holding yesterday's app.js after a fix is a
    # bug report nobody can reproduce. ETag/Last-Modified still make revalidation cheap.
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})

#: The symbol the whole experiment is built on. Every endpoint defaults to it so the
#: first request in a browser is a useful one.
DEFAULT_SYMBOL = "SPY"

#: Kinds that become a marker on the chart (spec: TRADE/STOP/ROLL/PROPOSAL/VETO).
#: ``NO_TRADE`` is deliberately absent: a plan-time no-trade is a veto, and the veto
#: histogram is where it belongs. Drawing it as a marker would imply we almost traded.
SIGNAL_KINDS: tuple[str, ...] = ("TRADE", "STOP", "ROLL", "PROPOSAL", "VETO")

Arm = Literal["A", "B", "C", "EXCEPTION"]
DecisionKind = Literal["TRADE", "NO_TRADE", "ROLL", "STOP", "PROPOSAL", "VETO"]
PositionStatus = Literal["OPEN", "CLOSED"]
#: ``created_at`` is sortable for the same reason it is rendered: the immutable-journal
#: story on the Ledger page is "this row was written at T, and if it was corrected the
#: correction is a LATER row that references it" — which is a statement about write
#: order, so ordering by event time alone hides it.
SortField = Literal["ts", "id", "arm", "kind", "symbol", "conviction", "created_at"]
Timeframe = Literal["5m", "10m", "15m", "30m", "1h", "4h", "1d", "1w", "1M", "1Q"]
IndicatorSet = Literal["ema50", "ema200", "rsi", "macd", "bb"]

#: Indicator names the ``set`` parameter accepts, in the order the UI sends them.
ALL_INDICATORS: tuple[str, ...] = ("ema50", "ema200", "rsi", "macd", "bb")

#: EMA/indicator parameters, frozen here rather than taken from the rulebook because
#: the spec pins them ("EMA(50/200), RSI(14), MACD(12,26,9), Bollinger(20,2)") and the
#: point of dogfooding is that the chart shows the checklist's numbers, not a
#: configurable set of numbers that happens to include them.
EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BB_PERIOD, BB_STD = 20, 2.0

#: Which raw wire timeframe a derived timeframe is built from.
SOURCE_TIMEFRAME: dict[str, str] = {
    **{tf: "1Min" for tf in aggregate.INTRADAY_MINUTES},
    "1d": "1Day",
    **{tf: "1Day" for tf in aggregate.CALENDAR_TIMEFRAMES},
}

#: Nominal bar width per timeframe, used only for bar↔signal joins and for the
#: ``partial_last``/``expected`` bookkeeping. Not a bucketing input.
TIMEFRAME_MINUTES: dict[str, int] = {
    **{tf: minutes for tf, minutes in aggregate.INTRADAY_MINUTES.items()},
    "1d": 390,
    "1w": 5 * 390,
    "1M": 21 * 390,
    "1Q": 63 * 390,
}


# ---------------------------------------------------------------------------
# read-only connections
# ---------------------------------------------------------------------------


def ro_uri(path: str | Path) -> str:
    """A ``file:`` URI with ``mode=ro``, the only way this module opens a database.

    ``mode=ro`` is enforced by SQLite itself, not by our discipline: a stray
    ``INSERT`` raises instead of succeeding quietly.
    """
    resolved = Path(path).resolve()
    return "file:" + urllib.parse.quote(resolved.as_posix(), safe="/") + "?mode=ro"


def open_ro(path: str | Path) -> sqlite3.Connection | None:
    """Open ``path`` read-only, or return ``None`` when the file does not exist.

    ``None`` — not an exception — is the missing-database answer, because before the
    window opens there is legitimately no journal on a fresh checkout and every
    endpoint must still render an honest empty page.
    """
    target = Path(path)
    if not target.exists():
        return None
    conn = sqlite3.connect(ro_uri(target), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def ro_connect(path: str | Path) -> Iterator[sqlite3.Connection | None]:
    """Context-managed read-only open; always closes."""
    conn = open_ro(path)
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection | None, table: str) -> int | None:
    if conn is None or not _has_table(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _loads(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _stamp(moment: dt.datetime) -> str:
    """UTC ISO-8601 with a trailing ``Z`` — the journal's convention, used everywhere.

    Bucketed bar starts are constructed in ET wall-clock, so a bare ``.isoformat()``
    would leak ``-04:00`` offsets into a response whose other timestamps are all
    ``Z``. One helper, so every timestamp in the API has the same shape.
    """
    aware = moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
    return aware.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# settings + context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UISettings:
    """Where the terminal reads from. Every path is injectable for tests."""

    journal_path: Path = REPO_ROOT / "data" / "journal.db"
    ivrank_path: Path = REPO_ROOT / "data" / "ivrank.db"
    rules_path: Path = DEFAULT_RULES_PATH
    barcache_path: Path | None = None
    symbol: str = DEFAULT_SYMBOL
    #: U1 ships the server without a live Alpaca client by default: a LAN research
    #: box with no credentials should still serve the cache instead of erroring. A
    #: request with ``fetch=true`` still works when credentials ARE present.
    allow_fetch: bool = False


@dataclass
class AppContext:
    """Per-app state: settings, the lazily-opened bar cache, and DB paths.

    Nothing here is cached across requests except the bar cache handle — a journal
    connection is opened and closed per request so a long-running server cannot pin a
    stale snapshot of a file another process is appending to.
    """

    settings: UISettings = field(default_factory=UISettings)
    _barcache: BarCache | None = None
    _live_barcache: BarCache | None = None

    # -- databases -------------------------------------------------------------

    @contextmanager
    def journal(self) -> Iterator[tuple[sqlite3.Connection | None, str]]:
        """Yield ``(connection_or_None, note)`` for the journal."""
        with ro_connect(self.settings.journal_path) as conn:
            note = (
                "journal present (opened read-only)"
                if conn is not None
                else f"no journal at {self.settings.journal_path} — the window has not opened yet"
            )
            yield conn, note

    @contextmanager
    def ivrank(self) -> Iterator[tuple[sqlite3.Connection | None, str]]:
        with ro_connect(self.settings.ivrank_path) as conn:
            note = (
                "ivrank present (opened read-only)"
                if conn is not None
                else f"no IV store at {self.settings.ivrank_path} — no IV history yet"
            )
            yield conn, note

    # -- bar cache -------------------------------------------------------------

    @property
    def barcache(self) -> BarCache:
        if self._barcache is None:
            self._barcache = open_cache(self.settings.barcache_path, with_client=False)
        return self._barcache

    def live_barcache(self) -> BarCache | None:
        """A cache with an Alpaca client, or ``None`` when there are no credentials.

        Separate handle from :attr:`barcache` so a cache-only server never holds a
        live transport, and so the test suite can hand the context a counting
        ``MockTransport`` without the module reaching for the environment.
        """
        if not self.settings.allow_fetch:
            return None
        if self._live_barcache is None:
            self._live_barcache = open_cache(self.settings.barcache_path, with_client=True)
        return self._live_barcache if self._live_barcache.client is not None else None

    def close(self) -> None:
        for cache in (self._barcache, self._live_barcache):
            if cache is not None:
                cache.close()
        self._barcache = None
        self._live_barcache = None

    # -- convenience -----------------------------------------------------------

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


def get_context() -> AppContext:
    """FastAPI dependency: the context installed on ``app.state``."""
    ctx = getattr(app.state, "context", None)
    if ctx is None:  # pragma: no cover - defensive; configure() always sets it
        ctx = AppContext()
        app.state.context = ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_context)]


# ---------------------------------------------------------------------------
# query models (pydantic validation of every list-bearing parameter)
# ---------------------------------------------------------------------------


def _csv(value: str | None, allowed: Sequence[str], *, name: str) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    for part in (p.strip() for p in value.split(",")):
        if not part:
            continue
        if part not in allowed:
            raise ValueError(f"{name}={part!r} is not one of {', '.join(allowed)}")
        out.append(part)
    return out


class BarsQuery(BaseModel):
    """``/api/bars`` parameters, validated before anything touches the cache."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = DEFAULT_SYMBOL
    timeframe: Timeframe = "15m"
    start: dt.date | None = None
    end: dt.date | None = None
    fetch: bool = False

    @property
    def source_timeframe(self) -> str:
        return SOURCE_TIMEFRAME[self.timeframe]


class IndicatorsQuery(BaseModel):
    """``/api/indicators`` parameters."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = DEFAULT_SYMBOL
    timeframe: Timeframe = "15m"
    names: list[IndicatorSet] = Field(default_factory=lambda: list(ALL_INDICATORS))
    start: dt.date | None = None
    end: dt.date | None = None


class TradeQuery(BaseModel):
    """``/api/trades`` — search, filter, sort, paginate."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = None
    arm: Arm | None = None
    kind: DecisionKind | None = None
    symbol: str | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    sort: SortField = "ts"
    order: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1, le=10_000)
    per_page: int = Field(default=50, ge=1, le=500)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


class SignalQuery(BaseModel):
    """``/api/signals`` — which decisions become chart markers, over which window."""

    model_config = ConfigDict(extra="forbid")

    arm: Arm | None = None
    kind: list[DecisionKind] = Field(default_factory=lambda: list(SIGNAL_KINDS))
    symbol: str | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    limit: int = Field(default=2000, ge=1, le=20_000)


def _validate(model: type[BaseModel], **kwargs: Any) -> Any:
    """Build a pydantic model, turning a validation failure into a 422.

    FastAPI already 422s on its own parameter types; this covers the cross-field and
    csv-list rules, and keeps the error body a pydantic-shaped list of problems.
    """
    try:
        return model(**kwargs)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from None


# ---------------------------------------------------------------------------
# bar + indicator assembly
# ---------------------------------------------------------------------------


def _window_bounds(
    query_start: dt.date | None,
    query_end: dt.date | None,
    timeframe: str,
) -> tuple[dt.datetime, dt.datetime]:
    """Default window for a bar request: the last few sessions, not all of history.

    A 15m chart over a year of 1m bars is 26 000 candles and a browser tab that eats
    a gigabyte; the UI zooms, and zooming backwards is a re-request with a wider
    window. Intraday defaults to the last 5 calendar days, daily-and-up to the last
    400 calendar days (enough for a 200-day EMA with room to spare).
    """
    now = dt.datetime.now(dt.UTC)
    if query_start is not None:
        start = dt.datetime.combine(query_start, dt.time(0, 0), tzinfo=aggregate.MARKET_TZ)
    else:
        days = 5 if timeframe in aggregate.INTRADAY_MINUTES else 400
        start = dt.datetime.combine(
            now.astimezone(aggregate.MARKET_TZ).date() - dt.timedelta(days=days),
            dt.time(0, 0),
            tzinfo=aggregate.MARKET_TZ,
        )
    if query_end is not None:
        end = dt.datetime.combine(
            query_end, dt.time(23, 59, 59), tzinfo=aggregate.MARKET_TZ
        )
    else:
        end = now
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def _load_bars(
    ctx: AppContext,
    query: BarsQuery,
    *,
    fetch: bool | None = None,
) -> tuple[list[AggBar], aggregate.BucketSeries, str]:
    """Cached raw bars -> aggregated bars for the requested timeframe.

    Returns ``(raw, buckets, cache_note)``. Aggregation happens on whatever the cache
    holds; with an empty cache the result is an empty ``BucketSeries`` rather than an
    error, because "we hold no bars for that window" is a fact the UI can render.
    """
    start, end = _window_bounds(query.start, query.end, query.timeframe)
    source = query.source_timeframe
    cache = ctx.live_barcache() if (query.fetch or fetch) else None
    if cache is None:
        raw = ctx.barcache.stored(query.symbol, source, start=start, end=end)
        note = "cache-only: no live fetch requested (or no credentials on this host)"
    else:
        raw = cache.bars(query.symbol, source, start=start, end=end, fetch=True)
        note = f"cache-first fetch against {source} (gap-only; rate limiter applies)"
    return raw, aggregate.aggregate(raw, query.timeframe), note


def _feed_summary(bars: Sequence[AggBar]) -> dict[str, Any]:
    """Provenance histogram of a bar set, plus the worst label present."""
    counts: dict[str, int] = {}
    for bar in bars:
        counts[bar.feed] = counts.get(bar.feed, 0) + 1
    worst = "sip"
    for bar in bars:
        worst = aggregate.worse_feed(worst, bar.feed)
    return {
        "by_bar": dict(sorted(counts.items())),
        "worst": worst if bars else None,
        "label": _provenance_label(worst if bars else "sip", partial=bool(bars)),
    }


def _provenance_label(feed: str, *, partial: bool) -> str:
    """The operator-facing badge text. Deliberately literal, never optimistic."""
    if partial and aggregate.provenance_rank(feed) >= 1:
        return f"{feed.upper()} partial — not consolidated"
    if feed.lower() in ("sip", "consolidated"):
        return "SIP consolidated"
    return f"{feed} — provenance unverified"


def _indicator_payload(
    bars: Sequence[AggBar],
    names: Sequence[str],
) -> dict[str, Any]:
    """Indicator values computed by :mod:`executor.indicators`, aligned to ``bars``.

    Every series keeps the input length with ``None`` at undefined positions, which is
    that module's stated policy and the reason a 15m RSI(14) reads ``null`` for its
    first 14 candles instead of being quietly truncated. ``InsufficientData`` is
    reported as a status, never raised: "not enough history" is a normal state for a
    freshly-seeded cache.
    """
    closes = [b.c for b in bars]
    timestamps = [b.t for b in bars]
    series: dict[str, list[float | None]] = {}
    status: dict[str, str] = {}
    reason: dict[str, str] = {}

    for name in names:
        try:
            if name == "ema50":
                values = indicators.ema(closes, EMA_FAST)
            elif name == "ema200":
                values = indicators.ema(closes, EMA_SLOW)
            elif name == "rsi":
                values = indicators.rsi(closes, RSI_PERIOD)
            elif name == "macd":
                result = indicators.macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
                series["macd"] = result.macd_line
                series["macd_signal"] = result.signal_line
                series["macd_histogram"] = result.histogram
                status["macd"] = indicators.confirmed(result.macd_line)
                continue
            elif name == "bb":
                result = indicators.bollinger(closes, BB_PERIOD, BB_STD)
                series["bb_middle"] = result.middle
                series["bb_upper"] = result.upper
                series["bb_lower"] = result.lower
                status["bb"] = indicators.confirmed(result.middle)
                continue
            else:  # pragma: no cover - the Literal type plus _csv() prevent this
                raise ValueError(f"unknown indicator {name!r}")
        except indicators.InsufficientData as exc:
            series[name] = [None] * len(bars)
            status[name] = "insufficient"
            reason[name] = str(exc)
            continue
        series[name] = values
        status[name] = indicators.confirmed(values)

    rows = []
    for i, bar in enumerate(bars):
        row: dict[str, Any] = {"t": _stamp(bar.t), "c": bar.c}
        for name, values in series.items():
            row[name] = values[i] if i < len(values) else None
        rows.append(row)
    return {
        "rows": rows,
        "series": sorted(series),
        "status": status,
        "reason": reason,
        "latest": {name: indicators.latest(values) for name, values in series.items()},
        "parameters": {
            "ema50": EMA_FAST,
            "ema200": EMA_SLOW,
            "rsi": RSI_PERIOD,
            "macd": [MACD_FAST, MACD_SLOW, MACD_SIGNAL],
            "bb": [BB_PERIOD, BB_STD],
        },
        "source": "executor.indicators — the same functions the checklist evaluates",
        "alignment": "aligned to input length; null where the indicator is undefined",
        "bar_count": len(bars),
        "first_t": _stamp(timestamps[0]) if timestamps else None,
    }


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="paper-hunter terminal",
    version="0.1.0",
    description=(
        "Read-only research view of Operation Hunting Blind. GET-only by construction: "
        "every database is opened mode=ro and there are no write endpoints."
    ),
)


def configure(
    settings: UISettings | None = None,
    *,
    context: AppContext | None = None,
) -> AppContext:
    """Install the app's context. Tests call this; production uses the defaults."""
    ctx = context or AppContext(settings=settings or UISettings())
    app.state.context = ctx
    return ctx


configure()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The terminal page (U2 replaced the U1 placeholder in the same file)."""
    return _static_file(INDEX_PAGE, "text/html")


@app.get("/app.js", include_in_schema=False)
def app_script() -> FileResponse:
    """The ES module. An explicit route rather than a StaticFiles mount, on purpose:

    ``tests/test_ui_api.py::test_app_has_zero_non_get_routes`` treats any route with
    no method set — which is what ``app.mount()`` produces — as a write-capable hole
    and fails. A mount of a read-only directory would be harmless, but the assertion
    exists to make "the UI has no door" a mechanical fact, so the static surface stays
    explicit and auditable: three named files, one method, no directory walk.
    """
    return _static_file(APP_SCRIPT, "text/javascript")


#: The U3/U4 ES modules, each served by its own explicit GET route. A loop rather than
#: seven copy-pasted functions: the route list is the security posture, so it is one
#: list next to the ``app_script`` docstring that explains the rule, not seven places
#: to forget. Every entry is a named file with a literal path — no path parameter, no
#: directory walk, and ``methods`` is set, so the zero-non-GET-routes assertion still
#: sees a plain single-method route rather than a mount.
STATIC_MODULES: tuple[tuple[str, Path], ...] = (
    ("/format.js", STATIC_DIR / "format.js"),
    ("/net.js", STATIC_DIR / "net.js"),
    ("/model.js", STATIC_DIR / "model.js"),
    ("/charts.js", STATIC_DIR / "charts.js"),
    ("/markers.js", STATIC_DIR / "markers.js"),
    ("/arms.js", STATIC_DIR / "arms.js"),
    ("/ledger.js", STATIC_DIR / "ledger.js"),
    ("/hunt.js", STATIC_DIR / "hunt.js"),
    ("/calibration.js", STATIC_DIR / "calibration.js"),
)


def _module_endpoint(path: Path) -> Any:
    def endpoint() -> FileResponse:
        return _static_file(path, "text/javascript")

    endpoint.__name__ = f"static_{path.stem.replace('-', '_')}"
    return endpoint


for _route, _module_path in STATIC_MODULES:
    app.add_api_route(
        _route,
        _module_endpoint(_module_path),
        methods=["GET"],
        include_in_schema=False,
    )


@app.get("/style.css", include_in_schema=False)
def app_styles() -> FileResponse:
    return _static_file(APP_STYLES, "text/css")


@app.get("/vendor/lightweight-charts.standalone.production.js", include_in_schema=False)
def vendor_bundle() -> FileResponse:
    """The vendored charting library (Apache-2.0, see ``static/vendor/PROVENANCE.md``).

    Served from the app so the page has zero external references: a LAN tool that
    reaches for a CDN is a LAN tool that breaks when the internet does.
    """
    return _static_file(VENDOR_BUNDLE, "text/javascript")


# -- health ------------------------------------------------------------------


@app.get("/api/health")
def health(ctx: Ctx) -> dict[str, Any]:
    """Liveness plus the provenance the chart badge is allowed to claim."""
    with ctx.journal() as (journal, journal_note), ctx.ivrank() as (ivrank, ivrank_note):
        dbs: dict[str, Any] = {
            "journal": {
                "path": str(ctx.settings.journal_path),
                "present": journal is not None,
                "mode": "ro",
                "decisions": _count(journal, "decisions"),
                "noshots": _count(journal, "noshots"),
                "positions": _count(journal, "positions"),
                "flow_baseline": _count(journal, "flow_baseline"),
            },
            "ivrank": {
                "path": str(ctx.settings.ivrank_path),
                "present": ivrank is not None,
                "mode": "ro",
                "observations": _count(ivrank, "iv_observations"),
            },
        }
    cache = ctx.barcache
    dbs["barcache"] = {**cache.provenance_summary(), "mode": "rw (cache, not a record)"}
    missing = [
        name for name, db in dbs.items() if name != "barcache" and not db.get("present", True)
    ]
    live = ctx.live_barcache() if ctx.settings.allow_fetch else None
    return {
        # "degraded" means a database file is missing. Before the window opens that
        # is the expected state, not a broken server — the per-database notes say
        # which file and why, so the operator is never left guessing.
        "status": "ok" if not missing else "degraded",
        "read_only": True,
        "server_time": _stamp(ctx.now()),
        "notes": {"journal": journal_note, "ivrank": ivrank_note},
        "dbs": dbs,
        "feed_provenance": {
            "requested_feed": cache.feed,
            "live_client": live is not None,
            "cached_series": len(cache.provenance_summary()["series"]),
            "label": _provenance_label(cache.feed, partial=False),
            "note": (
                "On the free tier the newest 15 minutes are IEX-only. Bars are stamped "
                "with the provenance of the fetch that produced them; the cache never "
                "asks Alpaca for a SIP window younger than 15 minutes."
            ),
        },
    }


# -- bars --------------------------------------------------------------------


@app.get("/api/bars")
def bars(
    ctx: Ctx,
    symbol: Annotated[str, Query(min_length=1, max_length=16)] = DEFAULT_SYMBOL,
    tf: Annotated[Timeframe, Query(description="chart timeframe")] = "15m",
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
    fetch: Annotated[bool, Query(description="allow a gap-only live fetch")] = False,
) -> dict[str, Any]:
    """Session-aware OHLCV at ``tf``, with provenance on every bar."""
    query = _validate(
        BarsQuery,
        symbol=symbol.upper(),
        timeframe=tf,
        start=from_,
        end=to,
        fetch=fetch,
    )
    raw, buckets, note = _load_bars(ctx, query)
    start, end = _window_bounds(query.start, query.end, query.timeframe)
    return {
        "symbol": query.symbol,
        "timeframe": query.timeframe,
        "source_timeframe": query.source_timeframe,
        "window": {"from": _stamp(start), "to": _stamp(end)},
        "source": note,
        "raw_bars": len(raw),
        **buckets.as_dict(),
        "feed_provenance": _feed_summary(buckets.bars),
    }


@app.get("/api/indicators")
def indicator_values(
    ctx: Ctx,
    symbol: Annotated[str, Query(min_length=1, max_length=16)] = DEFAULT_SYMBOL,
    tf: Annotated[Timeframe, Query()] = "15m",
    set_: Annotated[str | None, Query(alias="set", description="comma-separated names")] = None,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
) -> dict[str, Any]:
    """Indicators for the aggregated closes, computed by ``executor.indicators``."""
    query = _validate(
        IndicatorsQuery,
        symbol=symbol.upper(),
        timeframe=tf,
        start=from_,
        end=to,
    )
    names = _csv(set_, ALL_INDICATORS, name="set") or list(ALL_INDICATORS)
    _, buckets, note = _load_bars(
        ctx,
        BarsQuery(
            symbol=query.symbol,
            timeframe=query.timeframe,
            start=query.start,
            end=query.end,
        ),
    )
    payload = _indicator_payload(buckets.bars, names)
    return {
        "symbol": query.symbol,
        "timeframe": query.timeframe,
        "requested": names,
        "source": note,
        **payload,
    }


# -- journal reads ------------------------------------------------------------


def _price_for_decision(
    ctx: AppContext,
    symbol: str,
    moment: dt.datetime,
    source_timeframe: str,
) -> tuple[float | None, str | None]:
    """Best available price for a decision timestamp, and where it came from.

    Order: the decision's own recorded price (that is what the executor saw), then the
    cached bar at or before the decision. Returns ``(price, source)``; ``(None, None)``
    is a real answer — "we hold no bar there" — and the chart draws the marker without
    a price rather than inventing one.
    """
    cached = ctx.barcache.stored(
        symbol,
        source_timeframe,
        end=moment,
    )
    if not cached:
        return None, None
    return cached[-1].c, "bar"


#: JSON keys a snapshot may carry the traded price under, most specific first. Shared
#: by decisions and NO-SHOT sightings: both record a price in their own JSON blob and
#: both fall back to the bar cache, and one definition means the marker price and the
#: popup price can never disagree.
SNAPSHOT_PRICE_KEYS = ("spot", "price", "close", "underlying_price", "last_price")


def _snapshot_price(snapshot: Any) -> tuple[float | None, str | None]:
    """The price a JSON snapshot recorded for itself, and which key it came from."""
    if not isinstance(snapshot, dict):
        return None, None
    for key in SNAPSHOT_PRICE_KEYS:
        value = snapshot.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), f"snapshot.{key}"
    return None, None


def _decision_price(
    ctx: AppContext,
    symbol: str,
    entry: Any,
    moment: dt.datetime,
) -> tuple[float | None, str]:
    """Price for one decision row: its own snapshot first, then the nearest bar."""
    price, source = _snapshot_price(_loads(entry["checklist_snapshot"]))
    if price is not None:
        return price, source or "snapshot"
    price, source = _price_for_decision(ctx, symbol, moment, "1Min")
    if price is None:
        daily = ctx.barcache.stored(symbol, "1Day", end=moment)
        if daily:
            return daily[-1].c, "bar(1Day)"
    return price, source or "none"


def _decisions(
    conn: sqlite3.Connection,
    query: TradeQuery,
) -> tuple[list[sqlite3.Row], int]:
    """Filtered, searched, sorted decision rows plus the unpaginated total.

    ``q`` searches the reasoning text, the symbol, the JSON snapshot and the JSON
    checklist state — i.e. everything the operator can see in the Ledger tab. The
    count query runs the same WHERE, so ``total`` is the size of the filtered set and
    not the size of the table.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if query.arm:
        clauses.append("arm = ?")
        params.append(query.arm)
    if query.kind:
        clauses.append("kind = ?")
        params.append(query.kind)
    if query.symbol:
        clauses.append("symbol = ?")
        params.append(query.symbol.upper())
    if query.start:
        clauses.append("substr(ts, 1, 10) >= ?")
        params.append(query.start.isoformat())
    if query.end:
        clauses.append("substr(ts, 1, 10) <= ?")
        params.append(query.end.isoformat())
    if query.q:
        needle = f"%{query.q.lower()}%"
        clauses.append(
            "(lower(reasoning) LIKE ? OR lower(COALESCE(symbol, '')) LIKE ?"
            " OR lower(checklist_snapshot) LIKE ? OR lower(checklist_state) LIKE ?)"
        )
        params.extend([needle, needle, needle, needle])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM decisions{where}", params).fetchone()["n"]
    )
    order = "ASC" if query.order == "asc" else "DESC"
    rows = conn.execute(
        f"SELECT * FROM decisions{where} ORDER BY {query.sort} {order}, id {order}"
        " LIMIT ? OFFSET ?",
        [*params, query.per_page, query.offset],
    ).fetchall()
    return rows, total


def _trade_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "arm": row["arm"],
        "kind": row["kind"],
        "symbol": row["symbol"],
        "reasoning": row["reasoning"],
        "conviction": row["conviction"],
        "strategy_version": row["strategy_version"],
        "references": _loads(row["references"]) or [],
        "checklist_state": _loads(row["checklist_state"]) or {},
        "checklist_snapshot": _loads(row["checklist_snapshot"]) or {},
        "created_at": row["created_at"],
    }


@app.get("/api/signals")
def signals(
    ctx: Ctx,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
    arm: Annotated[Arm | None, Query()] = None,
    kind: Annotated[str | None, Query(description="comma-separated decision kinds")] = None,
    symbol: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=20_000)] = 2000,
) -> dict[str, Any]:
    """Chart markers: journal decisions joined to the nearest cached bar price."""
    query = _validate(
        SignalQuery,
        arm=arm,
        symbol=symbol.upper() if symbol else None,
        start=from_,
        end=to,
        limit=limit,
        kind=_csv(kind, SIGNAL_KINDS, name="kind") or list(SIGNAL_KINDS),
    )
    with ctx.journal() as (conn, note):
        if conn is None:
            return {"rows": [], "count": 0, "note": note, "symbol": symbol or DEFAULT_SYMBOL}
        clauses = [f"kind IN ({','.join('?' * len(query.kind))})"]
        params: list[Any] = list(query.kind)
        if query.arm:
            clauses.append("arm = ?")
            params.append(query.arm)
        if query.symbol:
            clauses.append("symbol = ?")
            params.append(query.symbol)
        if query.start:
            clauses.append("substr(ts, 1, 10) >= ?")
            params.append(query.start.isoformat())
        if query.end:
            clauses.append("substr(ts, 1, 10) <= ?")
            params.append(query.end.isoformat())
        rows = conn.execute(
            f"SELECT * FROM decisions WHERE {' AND '.join(clauses)}"
            " ORDER BY ts ASC, id ASC LIMIT ?",
            [*params, query.limit],
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        moment = dt.datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        price, source = _decision_price(ctx, row["symbol"] or DEFAULT_SYMBOL, row, moment)
        out.append(
            {
                "journal_id": row["id"],
                "t": row["ts"],
                "price": price,
                "price_source": source,
                "arm": row["arm"],
                "kind": row["kind"],
                "symbol": row["symbol"],
                "conviction": row["conviction"],
                "reasoning": row["reasoning"],
            }
        )
    return {
        "rows": out,
        "count": len(out),
        "kinds": query.kind,
        "note": note,
        "price_note": (
            "price is the decision's own recorded spot when it has one, else the "
            "nearest cached bar at or before the decision. Null means we hold no bar "
            "there — the marker still renders, without a price."
        ),
    }


def _arm_mismatches(
    cards: Sequence[dict[str, Any]],
    by_arm: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Integrity flags the card numbers depend on, computed from the journal alone.

    # INTERPRETATION: the spec's Arms page says open positions are "live from Alpaca
    paper + journal cross-check, mismatches flagged loudly". The broker half is not
    available to a read-only LAN server that must also work with no network and no
    Alpaca credentials, and silently reporting zero mismatches would be the worst
    possible answer — a green card claiming a cross-check that never ran. So this
    returns the checks that ARE possible offline, each one a real inconsistency
    between two journal facts, and the UI states the scope next to the flags:

      * ``no_bankroll``      — positions exist for an arm with no bankroll in meta, so
                              deployed/cash/return are undefined and the card says so.
      * ``over_allocated``   — deployed notional exceeds the bankroll, i.e. cash < 0.
      * ``unpriceable``      — an open position with a null entry price or a
                              non-positive qty; its notional is a made-up zero.
      * ``missing_pnl``      — a CLOSED position whose pnl is null. Excluded from the
                              realized sum (rollup does the same) but it means the
                              equity curve has a hole in it.
    """
    flags: list[dict[str, Any]] = []

    def add(arm: str, code: str, severity: str, detail: str) -> None:
        flags.append({"arm": arm, "code": code, "severity": severity, "detail": detail})

    for card in cards:
        arm = str(card["arm"])
        open_positions = card.get("open_positions") or []
        bank = card.get("bankroll")
        if open_positions and bank is None:
            add(
                arm,
                "no_bankroll",
                "bad",
                f"{len(open_positions)} open position(s) but no bankroll in journal meta — "
                "deployed, cash and return on bankroll are undefined for this arm.",
            )
        deployed = card.get("deployed") or 0.0
        if bank is not None and deployed > bank:
            add(
                arm,
                "over_allocated",
                "bad",
                f"deployed ${deployed:,.2f} exceeds bankroll ${bank:,.2f}: cash is "
                f"${(bank - deployed):,.2f}.",
            )
        for position in open_positions:
            if position.get("entry_price") is None or (position.get("qty") or 0) <= 0:
                add(
                    arm,
                    "unpriceable",
                    "bad",
                    f"position #{position.get('id')} ({position.get('symbol')}) has "
                    f"entry_price={position.get('entry_price')} qty={position.get('qty')} — "
                    "its notional is a placeholder zero, not a measurement.",
                )
    # missing_pnl needs the CLOSED rows, which the card summarises but does not carry.
    for card in cards:
        closed = [
            position
            for position in by_arm.get(str(card["arm"]), [])
            if position.get("status") == "CLOSED"
        ]
        missing = [position for position in closed if position.get("pnl") is None]
        if missing:
            add(
                str(card["arm"]),
                "missing_pnl",
                "warn",
                f"{len(missing)} closed position(s) with no recorded pnl "
                f"({', '.join('#' + str(position['id']) for position in missing)}): they "
                "are excluded from realized P&L and the equity curve has a hole there.",
            )
    return flags


@app.get("/api/arms")
def arms(
    ctx: Ctx,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
) -> dict[str, Any]:
    """Per-arm bankroll, positions, realized P&L, equity curve and shadow-roll marks."""
    with ctx.journal() as (conn, note):
        if conn is None:
            return {
                "arms": [_empty_arm(arm) for arm in ("A", "B", "C", "EXCEPTION")],
                "note": note,
                "sufficient": False,
                "mismatches": [],
                "cross_check": {"scope": "none", "broker_checked": False, "note": "no journal"},
            }
        pnl = rollup.arm_pnl(conn, from_, to)
        bankroll = pnl["arms"]
        positions = conn.execute("SELECT * FROM positions ORDER BY arm, entry_ts, id").fetchall()
        legs = conn.execute(
            "SELECT * FROM shadow_roll_legs ORDER BY opened_on, id"
        ).fetchall() if _has_table(conn, "shadow_roll_legs") else []
        marks = conn.execute(
            "SELECT * FROM shadow_roll_marks ORDER BY date, id"
        ).fetchall() if _has_table(conn, "shadow_roll_marks") else []

    by_arm: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ("A", "B", "C", "EXCEPTION")}
    for row in positions:
        by_arm.setdefault(row["arm"], []).append(
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "contract": row["contract"],
                "status": row["status"],
                "qty": row["qty"],
                "entry_ts": row["entry_ts"],
                "entry_price": row["entry_price"],
                "exit_ts": row["exit_ts"],
                "exit_price": row["exit_price"],
                "pnl": row["pnl"],
                "notional": (row["entry_price"] or 0) * (row["qty"] or 0),
            }
        )
    marks_by_leg: dict[int, list[sqlite3.Row]] = {}
    for mark in marks:
        marks_by_leg.setdefault(int(mark["leg_id"]), []).append(mark)

    cards: list[dict[str, Any]] = []
    for arm in ("A", "B", "C", "EXCEPTION"):
        rows = by_arm.get(arm, [])
        open_rows = [p for p in rows if p["status"] == "OPEN"]
        stats = bankroll.get(arm, {})
        bank = stats.get("bankroll")
        deployed = sum(p["notional"] for p in open_rows)
        curve: list[dict[str, Any]] = []
        running = 0.0
        for p in sorted(
            (p for p in rows if p["status"] == "CLOSED"),
            key=lambda r: r["exit_ts"] or "",
        ):
            if p["pnl"] is None:
                continue
            running += float(p["pnl"])
            curve.append({"t": p["exit_ts"], "equity": running})
        card: dict[str, Any] = {
            "arm": arm,
            "bankroll": bank,
            "deployed": deployed,
            "cash": (bank - deployed) if bank is not None else None,
            "realized_pnl": stats.get("realized_pnl", 0.0),
            "vs_control": stats.get("vs_control"),
            "return_on_bankroll": stats.get("return_on_bankroll"),
            "closed_positions": len(rows) - len(open_rows),
            "open_positions": open_rows,
            "equity_curve": curve,
            "shadow_roll": None,
        }
        if arm == "C" and legs:
            card["shadow_roll"] = _shadow_roll(legs, marks_by_leg)
        cards.append(card)
    return {
        "arms": cards,
        "sufficient": bool(pnl.get("sufficient")),
        "reason": pnl.get("reason"),
        "control_arm": pnl.get("control_arm"),
        "window": pnl.get("window"),
        "note": note,
        "mismatches": _arm_mismatches(cards, by_arm),
        "cross_check": {
            "scope": "journal-internal",
            "broker_checked": False,
            "note": (
                "The spec asks for open positions to be cross-checked against the live "
                "Alpaca paper account. This server is read-only, offline-capable and "
                "credential-free by design, so NO broker comparison was performed and "
                "none is implied. What is checked here is journal against journal: the "
                "flags above are real inconsistencies between two recorded facts. A card "
                "with no flags means the journal is internally consistent, NOT that the "
                "broker agrees with it."
            ),
        },
    }


def _empty_arm(arm: str) -> dict[str, Any]:
    """The card shape with nothing in it — an arm with no journal rows yet."""
    return {
        "arm": arm,
        "bankroll": None,
        "deployed": 0.0,
        "cash": None,
        "realized_pnl": 0.0,
        "vs_control": None,
        "return_on_bankroll": None,
        "closed_positions": 0,
        "open_positions": [],
        "equity_curve": [],
        "shadow_roll": None,
    }


def _shadow_roll(
    legs: Sequence[sqlite3.Row],
    marks_by_leg: dict[int, list[sqlite3.Row]],
) -> dict[str, Any]:
    """The costless shadow-roll comparison for arm C's card."""
    out_legs = []
    total = 0.0
    for leg in legs:
        leg_marks = marks_by_leg.get(int(leg["id"]), [])
        pnl = sum(float(m["leg_pnl"]) for m in leg_marks)
        total += pnl
        out_legs.append(
            {
                "id": leg["id"],
                "opened_on": leg["opened_on"],
                "expiry": leg["expiry"],
                "qty": leg["qty"],
                "underlying_close": leg["underlying_close"],
                "supersedes_leg_id": leg["supersedes_leg_id"],
                "marks": len(leg_marks),
                "pnl": pnl,
            }
        )
    return {
        "legs": out_legs,
        "total_pnl": total,
        "note": (
            "Shadow roll is a hypothetical comparison, not a position: no premium was "
            "paid and no order exists. It is compared with arm C on return on capital "
            "deployed, never summed into the A/B/C table."
        ),
    }


@app.get("/api/trades")
def trades(
    ctx: Ctx,
    q: Annotated[str | None, Query(description="free-text over reasoning/symbol/snapshots")] = None,
    arm: Annotated[Arm | None, Query()] = None,
    kind: Annotated[DecisionKind | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
    sort: Annotated[SortField, Query()] = "ts",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    per_page: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Paginated, searchable, sortable ledger rows."""
    query = _validate(
        TradeQuery,
        q=q,
        arm=arm,
        kind=kind,
        symbol=symbol.upper() if symbol else None,
        start=from_,
        end=to,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
    )
    with ctx.journal() as (conn, note):
        if conn is None:
            return {
                "rows": [],
                "total": 0,
                "page": query.page,
                "per_page": query.per_page,
                "pages": 0,
                "sort": query.sort,
                "order": query.order,
                "note": note,
            }
        rows, total = _decisions(conn, query)
    return {
        "rows": [_trade_row(row) for row in rows],
        "total": total,
        "page": query.page,
        "per_page": query.per_page,
        "pages": -(-total // query.per_page) if total else 0,
        "sort": query.sort,
        "order": query.order,
        "note": note,
    }


@app.get("/api/huntplan")
def huntplan(
    ctx: Ctx,
    date: Annotated[dt.date | None, Query()] = None,
    arm: Annotated[Arm | None, Query()] = None,
) -> dict[str, Any]:
    """A day's plan cells, read back out of the journal.

    The plan is not a table: ``executor.hunt_plan.write_hunt_plan`` journals one
    immutable decision row per (arm, direction) cell, and those rows ARE the persisted
    plan. Reading them back is therefore the same data the executor wrote, not a
    reconstruction — and a correction shows up as a second row, which the UI renders
    through ``references``.
    """
    target = date or dt.datetime.now(aggregate.MARKET_TZ).date()
    with ctx.journal() as (conn, note):
        if conn is None:
            return {
                "date": target.isoformat(),
                "cells": [],
                "count": 0,
                "note": note,
                "event_veto": {"active": False, "kinds": [], "available": False, "reason": ""},
            }
        clauses = ["kind IN ('PROPOSAL', 'NO_TRADE')", "substr(ts, 1, 10) = ?"]
        params: list[Any] = [target.isoformat()]
        if arm:
            clauses.append("arm = ?")
            params.append(arm)
        rows = conn.execute(
            f"SELECT * FROM decisions WHERE {' AND '.join(clauses)} ORDER BY arm, id", params
        ).fetchall()
    cells = []
    for row in rows:
        state = _loads(row["checklist_state"]) or {}
        snapshot = _loads(row["checklist_snapshot"]) or {}
        conditions = state.get("conditions")
        if not isinstance(conditions, dict):
            conditions = {}
        cells.append(
            {
                "journal_id": row["id"],
                "arm": row["arm"],
                "direction": snapshot.get("direction"),
                "kind": row["kind"],
                "conviction": row["conviction"],
                "reasoning": row["reasoning"],
                "fire": state.get("fire"),
                "veto_reasons": state.get("veto_reasons") or [],
                "failed_conditions": state.get("failed_conditions") or [],
                "pending_conditions": state.get("pending_conditions") or [],
                "conditions": conditions,
                "trigger_prices": state.get("trigger_prices") or [],
                "watch_levels": state.get("watch_levels") or {},
                "strategy_version": row["strategy_version"],
            }
        )
    # INTERPRETATION: the event veto banner. The plan's ``checklist_state`` records
    # the event veto only as prose inside the T5 condition's ``detail`` string
    # ("earnings day veto", "no event day") — there is no structured ``is_event_day``
    # flag on a plan cell. Rather than have the frontend guess with a regex over that
    # prose, the vocabulary comes from the rulebook's ``event_calendar.veto_kinds`` and
    # the match happens here, and the cell's own reason is returned verbatim next to the
    # match. If the rulebook cannot be loaded, ``active`` is false and ``available`` is
    # false: the page then says it could not determine the event veto rather than
    # drawing a green banner it did not earn.
    event_veto: dict[str, Any] = {"active": False, "kinds": [], "available": False, "reason": ""}
    try:
        rules = load_rules(ctx.settings.rules_path)
        event_calendar = rules.checklist.t5_options_chain.event_calendar
        veto_kinds = [kind.lower() for kind in event_calendar.veto_kinds]
    except (RulesError, OSError, AttributeError):
        veto_kinds = []
    if veto_kinds:
        hits: list[str] = []
        reasons: list[str] = []
        for cell in cells:
            for condition in (cell.get("conditions") or {}).values():
                if not isinstance(condition, dict):
                    continue
                detail = str(condition.get("detail") or "")
                if condition.get("status") == "PASS" or "no event day" in detail.lower():
                    continue
                for kind in veto_kinds:
                    if kind in detail.lower() and kind not in hits:
                        hits.append(kind)
                        reasons.append(detail)
        event_veto = {
            "active": bool(hits),
            "kinds": hits,
            "available": True,
            "reason": "; ".join(dict.fromkeys(reasons)),
        }
    return {
        "date": target.isoformat(),
        "cells": cells,
        "count": len(cells),
        "note": note,
        "event_veto": event_veto,
        # UX PASS 2: the RULEBOOK'S OWN veto kinds, sent whether or not any of them
        # fired. The all-clear claim on the Hunt page is "none of these specific kinds
        # was recorded", and a chip that says only "none" cannot be checked against
        # anything — a reader has no way to know whether the check ran against two kinds
        # or two hundred. A sibling key rather than a new field inside ``event_veto``:
        # that dict is an exact-shape assertion in the tests, and its meaning is "what
        # the day's cells said", whereas this is "what the rulebook would have said".
        "event_veto_kinds": veto_kinds,
        "event_veto_note": (
            "Derived by matching the rulebook's event_calendar.veto_kinds against each "
            "cell's condition detail text, because a plan cell stores the event veto as "
            "prose rather than as a structured flag. The reason is quoted verbatim."
        ),
        "source": "journal decisions (kind PROPOSAL/NO_TRADE) — the plan as written",
    }


@app.get("/api/noshots")
def noshots(
    ctx: Ctx,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
    date: Annotated[dt.date | None, Query(description="single session date")] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> dict[str, Any]:
    """NO-SHOT entries with their counterfactual outcomes, plus the ledger delta."""
    with ctx.journal() as (conn, note):
        if conn is None:
            return {
                "rows": [],
                "count": 0,
                "counterfactual": None,
                "note": note,
            }
        clauses: list[str] = []
        params: list[Any] = []
        if date:
            clauses.append("date = ?")
            params.append(date.isoformat())
        if from_:
            clauses.append("date >= ?")
            params.append(from_.isoformat())
        if to:
            clauses.append("date <= ?")
            params.append(to.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM noshots{where} ORDER BY date DESC, id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        delta = rollup.counterfactual_delta(conn, from_ or date, to or date)
    out = []
    for row in rows:
        outcome = _loads(row["counterfactual_outcome"])
        hypothesis = _loads(row["instrument_hypothesis"]) or {}
        indicators = _loads(row["indicator_values"]) or {}
        moment = dt.datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        # A NO-SHOT marker needs a price to sit on the bar. A sighting records the spot
        # it was taken at in its own JSON blobs; failing that, the nearest cached bar at
        # or before the sighting. Null is a real answer and the marker still renders.
        price, source = _snapshot_price(indicators)
        if price is None:
            price, source = _snapshot_price(hypothesis)
        if price is None:
            symbol = str(hypothesis.get("symbol") or DEFAULT_SYMBOL)
            price, source = _price_for_decision(ctx, symbol, moment, "1Min")
            if price is None:
                daily = ctx.barcache.stored(symbol, "1Day", end=moment)
                price, source = (daily[-1].c, "bar(1Day)") if daily else (None, "none")
        out.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "date": row["date"],
                "instrument_hypothesis": hypothesis,
                "failed_conditions": _loads(row["failed_conditions"]) or {},
                "indicator_values": indicators,
                "counterfactual_entry_ref": row["counterfactual_entry_ref"],
                "counterfactual_outcome": outcome,
                "has_outcome": outcome is not None,
                "price": price,
                "price_source": source,
                "created_at": row["created_at"],
            }
        )
    return {
        "rows": out,
        "count": len(out),
        "counterfactual": delta,
        "note": note,
        "price_note": (
            "same provenance rule as /api/signals: the sighting's own recorded spot, "
            "else the nearest cached bar at or before the sighting. Null means we hold "
            "no bar there — the hollow marker still renders, without a price."
        ),
        "outcome_note": (
            "counterfactual_outcome is the one journal column that may be filled in "
            "after the fact (schema trigger). A null here means the window has not "
            "moved far enough to know, which is not the same as 'no edge'."
        ),
    }


@app.get("/api/histogram")
def histogram(
    ctx: Ctx,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: Annotated[dt.date | None, Query()] = None,
    veto: Annotated[Literal["weekly", "total"], Query()] = "weekly",
) -> dict[str, Any]:
    """Which conditions say no, over time.

    Delegates to ``analysis.rollup.checklist_failure_histogram`` — the same function
    the weekly markdown rollup scores the veto counts with — so the chart and the
    Discord report cannot disagree about how many times T4 said no.
    """
    with ctx.journal() as (conn, note):
        if conn is None:
            return {"mode": veto, "weeks": [], "totals": None, "note": note}
        if veto == "total":
            return {
                "mode": "total",
                "weeks": [],
                "totals": rollup.checklist_failure_histogram(conn, from_, to),
                "note": note,
            }
        first, last = _journal_extent(conn)
        if from_:
            first = min(first, from_) if first else from_
        if to:
            last = max(last, to) if last else to
        if first is None or last is None:
            return {"mode": "weekly", "weeks": [], "totals": None, "note": note}
        weeks = []
        cursor = first - dt.timedelta(days=first.weekday())
        while cursor <= last:
            week_end = cursor + dt.timedelta(days=6)
            result = rollup.checklist_failure_histogram(
                conn, max(cursor, first), min(week_end, last)
            )
            weeks.append(
                {
                    "week_start": cursor.isoformat(),
                    "week_end": min(week_end, last).isoformat(),
                    "by_condition": result["by_condition"],
                    "by_arm": result["by_arm"],
                    "pending_by_condition": result["pending_by_condition"],
                    "sources": result["sources"],
                    "sufficient": result["sufficient"],
                    "reason": result["reason"],
                }
            )
            cursor = week_end + dt.timedelta(days=1)
        return {
            "mode": "weekly",
            "weeks": weeks,
            "totals": rollup.checklist_failure_histogram(conn, first, last),
            "window": {"from": first.isoformat(), "to": last.isoformat()},
            "note": note,
        }


def _journal_extent(conn: sqlite3.Connection) -> tuple[dt.date | None, dt.date | None]:
    """First and last ET session date the journal knows about, or ``(None, None)``."""
    dates: list[str] = []
    for sql in (
        "SELECT MIN(substr(ts, 1, 10)) AS d FROM decisions",
        "SELECT MAX(substr(ts, 1, 10)) AS d FROM decisions",
        "SELECT MIN(date) AS d FROM noshots",
        "SELECT MAX(date) AS d FROM noshots",
    ):
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.Error:  # pragma: no cover - table missing on a partial journal
            continue
        if row and row["d"]:
            dates.append(str(row["d"]))
    if not dates:
        return None, None
    return dt.date.fromisoformat(min(dates)), dt.date.fromisoformat(max(dates))


def _percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (the ``numpy.percentile`` convention).

    Named explicitly because the calibration panel shows a P90 line and the two common
    conventions disagree by a visible amount on a short series: the strict
    "percentage strictly below" rank used by
    :func:`executor.indicators.bandwidth_percentile` is the right rule for *scoring a
    value against a band*, and this interpolated one is the right rule for *drawing a
    line through a series*. Mixing them would move T6's threshold.
    """
    if not values:
        raise ValueError("percentile of an empty series is undefined")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@app.get("/api/calibration")
def calibration(
    ctx: Ctx,
    limit: Annotated[int, Query(ge=1, le=2000)] = 120,
    underlying: Annotated[str, Query()] = "SPY",
) -> dict[str, Any]:
    """The T6 story: flow rows, the proposed N, the volume P90 line, IV history,
    pending calibrations.

    FIX (2026-10-03, full-cycle rehearsal): the panel used to report its "P90" as
    ``deep_otm_total_volume`` and label it as the forming N. T6 evaluates
    ``today_volume >= N * baseline_mean``, so the multiplier must be **dimensionless**;
    a percentile of contract counts is a good chart line and the wrong constant. The
    volume percentile is still here (the chart draws a line through the series with
    it) but it is labelled as volume, and ``n`` carries the ratified ratio proposal.
    """
    flow: list[dict[str, Any]] = []
    pending: list[str] = []
    rulebook_version: str | None = None
    p90: dict[str, Any] = {
        "status": "no_data",
        "value": None,
        "sessions": 0,
        "sessions_needed": 20,
        "method": (
            "linear-interpolation percentile of per-session deep-OTM total volume — a "
            "CHART LINE in contract counts, not the frozen N"
        ),
        "note": "T6's N is a multiplier on the 20-session baseline mean and is read off "
        "the ratio distribution (see the proposed-N block); this line is the volume "
        "distribution the panel draws, and it moves until the baseline is complete.",
    }
    n_block: dict[str, Any] = {
        "status": "no_data",
        "value": None,
        "method": "",
        "note": "",
    }
    with ctx.journal() as (conn, note):
        if conn is not None and _has_table(conn, "flow_baseline"):
            rows = conn.execute(
                "SELECT * FROM flow_baseline ORDER BY date DESC LIMIT ?", (limit,)
            ).fetchall()
            history = list(reversed(rows))
            proposal = flow_calibration(history)
            n_block = proposal.to_dict()
            for row in history:
                flow.append(
                    {
                        "date": row["date"],
                        "underlying": row["underlying"],
                        "feed": row["feed"],
                        "is_delayed": bool(row["is_delayed"]),
                        "session_spot": row["session_spot"],
                        "deep_otm_threshold_pct": row["deep_otm_threshold_pct"],
                        "call_volume": row["deep_otm_call_volume"],
                        "put_volume": row["deep_otm_put_volume"],
                        "total_volume": row["deep_otm_total_volume"],
                        "call_by_distance": _loads(row["call_volume_by_distance"]) or {},
                        "put_by_distance": _loads(row["put_volume_by_distance"]) or {},
                        "baseline_lookback_days": row["baseline_lookback_days"],
                        "baseline_days": row["baseline_days"],
                        "baseline_call_mean": row["baseline_call_mean"],
                        "baseline_put_mean": row["baseline_put_mean"],
                        "ratio_call": row["ratio_call"],
                        "ratio_put": row["ratio_put"],
                    }
                )
            volumes = [float(r["deep_otm_total_volume"]) for r in history]
            needed = max((int(r["baseline_lookback_days"]) for r in history), default=20)
            p90["sessions"] = len(volumes)
            p90["sessions_needed"] = needed
            if volumes:
                p90["value"] = _percentile(volumes, 0.90)
                p90["status"] = "forming" if len(volumes) < needed else "defined"
                p90["call_p90"] = _percentile(
                    [float(r["deep_otm_call_volume"]) for r in history], 0.90
                )
                p90["put_p90"] = _percentile(
                    [float(r["deep_otm_put_volume"]) for r in history], 0.90
                )
        else:
            note = f"{note}; no flow_baseline table yet"

    try:
        rules = load_rules(ctx.settings.rules_path)
        pending = list(rules.pending_calibrations)
        rulebook_version = rules.strategy_version
    except (OSError, RulesError, ValueError) as exc:
        pending = [f"rulebook unreadable: {exc}"]

    iv_series: list[dict[str, Any]] = []
    with ctx.ivrank() as (ivrank, iv_note):
        if ivrank is not None and _has_table(ivrank, "iv_observations"):
            rows = ivrank.execute(
                "SELECT underlying, as_of, tenor_key, iv, source, is_proxy, expiry, right,"
                " strike FROM iv_observations ORDER BY as_of ASC, tenor_key ASC"
            ).fetchall()
            iv_series = [dict(row) for row in rows]
        else:
            iv_note = f"{iv_note}; no iv_observations table yet"

    by_distance: dict[str, dict[str, float]] = {}
    for row in flow:
        for side, key in (("call", "call_by_distance"), ("put", "put_by_distance")):
            bucket = by_distance.setdefault(side, {})
            for pct, value in (row[key] or {}).items():
                bucket[pct] = bucket.get(pct, 0.0) + float(value)

    return {
        "underlying": underlying,
        "flow": flow,
        "flow_sessions": len(flow),
        "p90": p90,
        "n": n_block,
        "distance_histogram": by_distance,
        "ivrank": {
            "series": iv_series,
            "count": len(iv_series),
            "note": "is_proxy distinguishes the VIX proxy series from real chain IV",
            "source_note": iv_note,
        },
        "pending_calibrations": pending,
        "strategy_version": rulebook_version,
        "sufficient": bool(flow),
        "reason": None if flow else "no flow_baseline rows yet — the soak writes one per session",
        "note": note,
    }


@app.get("/api/rollup")
def rollup_view(
    ctx: Ctx,
    week: Annotated[dt.date | None, Query(description="any date inside the week")] = None,
) -> dict[str, Any]:
    """The weekly markdown rollup, for archival browsing.

    The chat is the primary report surface (it posts to Discord); this endpoint exists
    so the UI can show the same text the operator already read, not a second report.
    """
    target = week or dt.datetime.now(aggregate.MARKET_TZ).date()
    with ctx.journal() as (conn, note):
        if conn is None:
            return {
                "week": target.isoformat(),
                "window": None,
                "markdown": "",
                "note": note,
            }
        markdown = rollup.weekly_rollup_text(conn, target)
    start = target - dt.timedelta(days=rollup.WEEK_DAYS - 1)
    return {
        "week": target.isoformat(),
        "window": {"from": start.isoformat(), "to": target.isoformat()},
        "markdown": markdown,
        "note": note,
        "source": "analysis.rollup.weekly_rollup_text — identical text to the Discord post",
    }


# ---------------------------------------------------------------------------
# error shape
# ---------------------------------------------------------------------------


@app.exception_handler(ValueError)
def _value_error(_request: Any, exc: ValueError) -> JSONResponse:
    """Bad enum/parameter values answer 400 with the reason, not a 500 traceback."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


__all__ = [
    "AppContext",
    "BarsQuery",
    "IndicatorsQuery",
    "SignalQuery",
    "TradeQuery",
    "UISettings",
    "app",
    "configure",
    "open_ro",
    "ro_uri",
]
