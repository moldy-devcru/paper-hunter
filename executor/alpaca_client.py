"""Thin Alpaca REST client — market data + paper account reads. NO order placement.

Scope (Phase 3a)
----------------
This module is the *data layer only*. It reads bars, the options chain, the paper
account/positions and the clock. It deliberately cannot place, replace, modify or
cancel an order: there is no such method here, so "accidentally went live" is not a
reachable state from this file even if someone constructs a client at 3am on a
bad night. Order placement is Phase 3b and needs operator keys.

Source: docs/research/2026-10-02-options-data-sources.md
-------------------------------------------------------
Facts this client encodes rather than re-derives:

* Base URLs. Trading (paper): ``https://paper-api.alpaca.markets``. Data:
  ``https://data.alpaca.markets``. Stock bars are v2 and single-symbol
  (``/v2/stocks/{symbol}/bars``); options chain snapshots are v1beta1
  (``/v1beta1/options/snapshots/{underlying}``).
* Free / Basic plan. Equities realtime is **IEX only**; options realtime is the
  **Indicative Pricing Feed** (synthetic quotes, trades delayed 15 min); historical
  SIP bars are queryable as long as ``end`` is at least 15 minutes old. We never
  silently mix feeds — ``feed`` is an explicit required argument on every bar call,
  because T4 relative volume is only meaningful when numerator and baseline come
  from the same feed.
* Rate limit: 200 historical calls/min on Basic. ``RateLimiter`` implements that as
  a sliding window; it is generous and cheap rather than clever.

Authentication
--------------
``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET``. Both are read from the environment
and held only in request headers. This module never logs a header, never puts a key
in an exception message, and never returns one. ``__repr__`` is overridden to say
"configured"/"unset" rather than the value, because a repr ends up in tracebacks and
pytest output.

Offline tests
-------------
``Transport`` is the single seam. ``UrllibTransport`` does real HTTPS; tests inject
``MockTransport`` with canned JSON fixtures shaped exactly like Alpaca's *documented*
responses — flat bar lists, contract-symbol-keyed snapshot dicts, and a query-param
contract enforced per route. See :data:`DOC_VERIFIED_ROUTES`. Nothing in the test
suite touches the network.

Python 3.12+, stdlib only (``urllib``) — consistent with executor/indicators.py's
zero-transitive-risk rule. No ``httpx`` dependency is added.

# INTERPRETATION: ``timeframe`` values are the Alpaca wire strings ("1Min", "5Min",
# "1Day"). Daily and intraday bars use the *same* single-symbol route
# ``GET /v2/stocks/{symbol}/bars`` with different ``timeframe`` values — the symbol is
# a path segment. (The multi-symbol ``/v2/stocks/bars?symbols=...`` route exists and is
# not what this client calls.)

Every route below is doc-verified; ``tests/test_alpaca_routes.py`` pins each path shape
so route drift fails in CI instead of on a live 3am soak run.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

# ---------------------------------------------------------------------------
# endpoints / constants
# ---------------------------------------------------------------------------

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

#: Single-symbol historical stock bars. ``{symbol}`` is substituted into the **path**.
#: https://docs.alpaca.markets/us/reference/stockbarsingle-1
#:
#: The multi-symbol sibling (``/v2/stocks/bars?symbols=AAPL,MSFT``) is a *different*
#: route with a different query contract and a different response envelope. Mixing the
#: two is not a typo you get a free pass on: the multi-symbol route requires the
#: ``symbols`` query param and returns 400 ``"query parameter 'symbols' is required"``
#: if it is missing. See ``tests/test_alpaca_routes.py``.
STOCK_BARS_PATH = "/v2/stocks/{symbol}/bars"

#: Whole-chain option snapshots for one underlying.
#: https://docs.alpaca.markets/us/reference/optionchain
#:
#: Options endpoints are **v1beta1**, not v2beta1 — there is no ``v2beta1`` anywhere in
#: the current Alpaca API reference, and a request to an unknown version 404s.
OPTIONS_SNAPSHOTS_PATH = "/v1beta1/options/snapshots"

ACCOUNT_PATH = "/v2/account"
POSITIONS_PATH = "/v2/positions"
CLOCK_PATH = "/v2/clock"


def stock_bars_path(symbol: str) -> str:
    """Path for one symbol's bars. The symbol is a path segment, not a query param."""
    return STOCK_BARS_PATH.format(symbol=urllib.parse.quote(symbol.upper(), safe=""))


KEY_ENV = "ALPACA_PAPER_KEY"
SECRET_ENV = "ALPACA_PAPER_SECRET"

Timeframe = Literal["1Min", "5Min", "15Min", "1Hour", "1Day"]
Feed = Literal["iex", "sip", "indicative", "opra"]

#: Free-tier historical call budget (Basic plan, per the market-data matrix).
RATE_LIMIT_CALLS = 200
RATE_LIMIT_WINDOW_SECONDS = 60.0

#: Alpaca's documented free-tier floor for SIP history: ``end`` must be at least this
#: old or the request is rejected (42210000). We surface it, we do not hide it.
SIP_HISTORY_MIN_AGE_MINUTES = 15


class AlpacaError(Exception):
    """Base for every error this module raises."""


class AlpacaConfigError(AlpacaError):
    """Missing credentials or a missing transport — a setup bug, not a data problem."""


class AlpacaAPIError(AlpacaError):
    """A non-2xx HTTP response from Alpaca.

    ``status`` and ``message`` only. The request headers are never attached, so an
    API error can never become a secret leak in a log line or a journal entry.
    """

    def __init__(self, status: int, message: str, *, path: str = "") -> None:
        detail = f"{path}: " if path else ""
        super().__init__(f"alpaca HTTP {status} ({detail}{message})")
        self.status = status
        self.message = message
        self.path = path


class RateLimitError(AlpacaError):
    """Raised only when a caller explicitly opts out of waiting (blocking=False)."""


# ---------------------------------------------------------------------------
# transport seam
# ---------------------------------------------------------------------------


class Transport(Protocol):
    """The one injectable seam. Implementations get a full URL and return parsed JSON."""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
    ) -> Any: ...


class UrllibTransport:
    """Real HTTPS via ``urllib``. Stdlib only, no connection pooling.

    We do one short-lived request per call, on purpose: the free-tier budget is 200
    calls/min and the trigger loop polls every few seconds, so pooling would buy
    nothing and hide failures.
    """

    def __init__(self, timeout: float = 10.0, *, user_agent: str = "paper-hunter/0.1") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
    ) -> Any:
        full = url
        if params:
            clean = {k: str(v) for k, v in params.items() if v is not None}
            if clean:
                full = f"{url}?{urllib.parse.urlencode(clean)}"
        request = urllib.request.Request(full, method="GET")
        request.add_header("User-Agent", self.user_agent)
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            raise AlpacaAPIError(exc.code, _api_message(raw), path=url) from None
        except urllib.error.URLError as exc:
            raise AlpacaError(f"alpaca transport failure for {url}: {exc.reason}") from None
        if not body.strip():
            return {}
        return json.loads(body)


def _api_message(raw: str) -> str:
    """Extract Alpaca's ``message`` field; fall back to a truncated raw body."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:300] or "no body"
    if isinstance(parsed, dict) and "message" in parsed:
        return str(parsed["message"])[:300]
    return raw[:300]


@dataclass(frozen=True, slots=True)
class DocRoute:
    """One doc-verified Alpaca route: its path template, its real query params, its URL.

    This is the load-bearing part of the offline test story. The mock used to be a
    permissive lookup table keyed by whatever path the production code happened to
    ask for, which meant it happily mirrored a *wrong* route — 537 green tests and a
    live ``HTTP 400`` on the first real call. Recording the documented contract here
    and validating against it means a drifted path or a renamed query parameter fails
    in the test suite instead of at 3am.
    """

    path: str
    doc_url: str
    query_params: frozenset[str]
    base: str


#: Every route this project talks to, doc-verified. Single source of truth for both the
#: constants above and the offline mock's fidelity check.
DOC_VERIFIED_ROUTES: tuple[DocRoute, ...] = (
    DocRoute(
        path="/v2/stocks/{symbol}/bars",
        doc_url="https://docs.alpaca.markets/us/reference/stockbarsingle-1",
        query_params=frozenset(
            {
                "timeframe",
                "feed",
                "start",
                "end",
                "limit",
                "adjustment",
                "page_token",
                "asof",
                "sort",
                "currency",
            }
        ),
        base=DATA_BASE,
    ),
    DocRoute(
        path="/v1beta1/options/snapshots/{underlying_symbol}",
        doc_url="https://docs.alpaca.markets/us/reference/optionchain",
        query_params=frozenset(
            {
                "feed",
                "limit",
                "updated_since",
                "page_token",
                "type",
                "strike_price_gte",
                "strike_price_lte",
                "expiration_date",
                "expiration_date_gte",
                "expiration_date_lte",
                "root_symbol",
            }
        ),
        base=DATA_BASE,
    ),
    DocRoute(
        path="/v2/account",
        doc_url="https://docs.alpaca.markets/reference/getaccount-1",
        query_params=frozenset(),
        base=PAPER_TRADING_BASE,
    ),
    DocRoute(
        path="/v2/positions",
        doc_url="https://docs.alpaca.markets/reference/getopenpositions-1",
        query_params=frozenset({"symbols", "side"}),
        base=PAPER_TRADING_BASE,
    ),
    DocRoute(
        path="/v2/clock",
        doc_url="https://docs.alpaca.markets/reference/getclock-1",
        query_params=frozenset(),
        base=PAPER_TRADING_BASE,
    ),
)


def match_doc_route(path: str) -> DocRoute | None:
    """Match a concrete request path against the doc-verified route templates."""
    for route in DOC_VERIFIED_ROUTES:
        pattern = "^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(route.path)) + "$"
        if re.match(pattern, path):
            return route
    return None


class MockTransport:
    """Offline transport: canned JSON keyed by request path.

    ``routes`` maps a path to either a payload (returned as-is) or to a callable
    ``(params) -> payload``. A request with no matching route raises
    ``AlpacaAPIError(404)`` — the same shape as the real API, so a wrong path in
    production code fails loudly in tests instead of silently returning nothing.

    Fidelity guarantee
    ------------------
    The routes and envelopes here mirror **doc-verified** paths (see
    :data:`DOC_VERIFIED_ROUTES`), and by default the mock also enforces the real API's
    query-parameter contract: a parameter the documentation does not list for that
    route raises ``AlpacaAPIError(400)``, exactly as Alpaca rejects an invalid format.
    That is the specific check that would have caught the live bug — sending ``symbol``
    as a query param to the bars route is a documented 400 ("query parameter 'symbols'
    is required"), so the mock now refuses to be wrong about it.

    Pass ``strict=False`` only for a deliberately ad-hoc shape.

    This class is import-safe in production but useless without routes; that is
    intentional. Nothing here reads an environment variable.
    """

    def __init__(self, routes: Mapping[str, Any] | None = None, *, strict: bool = True) -> None:
        self.routes: dict[str, Any] = dict(routes or {})
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []
        self.strict = strict

    def add(self, path: str, payload: Any) -> MockTransport:
        self.routes[path] = payload
        return self

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
    ) -> Any:
        self.calls.append((url, dict(params or {})))
        path = urllib.parse.urlsplit(url).path
        if self.strict:
            self._enforce_doc_contract(path, dict(params or {}))
        if path not in self.routes:
            raise AlpacaAPIError(404, f"no mock route for {path}", path=path)
        payload = self.routes[path]
        if callable(payload):
            return payload(dict(params or {}))
        return payload

    def _enforce_doc_contract(self, path: str, params: Mapping[str, str]) -> None:
        route = match_doc_route(path)
        if route is None:
            raise AlpacaAPIError(404, f"{path} is not a doc-verified Alpaca route", path=path)
        unknown = sorted(set(params) - route.query_params)
        if unknown:
            raise AlpacaAPIError(
                400,
                f"invalid parameter(s) for {route.path}: {', '.join(unknown)} "
                f"(see {route.doc_url})",
                path=path,
            )


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window limiter: at most ``max_calls`` in any ``window_seconds``.

    Dead simple on purpose. It tracks call timestamps, drops ones older than the
    window, and sleeps only as long as it must. ``sleep`` and ``clock`` are injected
    so tests run instantly and deterministically.
    """

    def __init__(
        self,
        max_calls: int = RATE_LIMIT_CALLS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        *,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        if max_calls < 1:
            raise ValueError(f"max_calls must be >= 1, got {max_calls}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._sleep = sleep
        self._clock = clock
        self._stamps: list[float] = []

    def acquire(self, *, blocking: bool = True) -> None:
        """Wait until a slot frees, then take it.

        Loops rather than recursing. The recursion would be equivalent with a real
        clock but infinitely recursive with an injected clock that does not advance
        during ``sleep`` (a frozen clock in a test, or a coarse monotonic clock on a
        loaded box) — a data layer that crashes its own trigger loop over a rate limit
        is worse than one that waits.

        # INTERPRETATION: after sleeping, if the window still looks full (the clock
        # did not move), the oldest stamp is dropped and the call proceeds. Waiting
        # forever on a clock that is not advancing is a deadlock, and one extra call
        # past a soft rate limit is a better failure than a hang.
        """
        for _ in range(self.max_calls + 2):
            now = self._clock()
            self._stamps = [t for t in self._stamps if now - t < self.window_seconds]
            if len(self._stamps) < self.max_calls:
                self._stamps.append(now)
                return
            wait = self.window_seconds - (now - self._stamps[0])
            if not blocking:
                raise RateLimitError(
                    f"rate limit reached ({self.max_calls} per {self.window_seconds}s); "
                    f"retry in {wait:.2f}s"
                )
            if wait > 0:
                self._sleep(wait)
            if self._clock() - self._stamps[0] >= self.window_seconds:
                continue  # time really moved; loop and re-evaluate
            self._stamps.pop(0)  # clock did not move — drop the oldest and proceed
        self._stamps.append(self._clock())


# ---------------------------------------------------------------------------
# typed returns
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar. ``t`` is the Alpaca RFC3339 timestamp (bar *start*), kept as a
    datetime so cross-age arithmetic is timezone-correct rather than string math."""

    t: datetime
    o: float
    h: float
    l: float  # noqa: E741 - the Alpaca wire field really is "l"
    c: float
    v: float
    n: int | None = None
    vw: float | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Bar:
        ts = _as_str(raw.get("t"))
        if ts is None:
            raise AlpacaError(f"bar is missing its timestamp: {raw!r}")
        try:
            moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlpacaError(f"unparseable bar timestamp {ts!r}") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return cls(
            t=moment,
            o=_as_float(raw.get("o")) or 0.0,
            h=_as_float(raw.get("h")) or 0.0,
            l=_as_float(raw.get("l")) or 0.0,
            c=_as_float(raw.get("c")) or 0.0,
            v=_as_float(raw.get("v")) or 0.0,
            n=_as_int(raw.get("n")),
            vw=_as_float(raw.get("vw")),
        )


@dataclass(frozen=True, slots=True)
class BarSeries:
    """Bars for one symbol, oldest-first, plus the pagination token if there is more.

    Oldest-first is the orientation ``executor.indicators`` is written against, so
    the client normalises once here rather than every caller remembering to reverse.
    """

    symbol: str
    timeframe: Timeframe
    feed: str
    bars: list[Bar]
    next_page_token: str | None = None

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def closes(self) -> list[float]:
        return [b.c for b in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [b.v for b in self.bars]

    def __iter__(self):
        return iter(self.bars)


@dataclass(frozen=True, slots=True)
class Greeks:
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> Greeks:
        raw = raw or {}
        return cls(
            delta=_as_float(raw.get("delta")),
            gamma=_as_float(raw.get("gamma")),
            theta=_as_float(raw.get("theta")),
            vega=_as_float(raw.get("vega")),
            rho=_as_float(raw.get("rho")),
        )


@dataclass(frozen=True, slots=True)
class OptionQuote:
    bid: float | None = None
    bid_size: int | None = None
    ask: float | None = None
    ask_size: int | None = None
    t: datetime | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> OptionQuote:
        raw = raw or {}
        return cls(
            bid=_as_float(raw.get("bp")),
            bid_size=_as_int(raw.get("bs")),
            ask=_as_float(raw.get("ap")),
            ask_size=_as_int(raw.get("as")),
            t=_parse_ts(raw.get("t")),
        )


@dataclass(frozen=True, slots=True)
class OptionTrade:
    p: float | None = None
    s: int | None = None
    t: datetime | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> OptionTrade:
        raw = raw or {}
        return cls(
            p=_as_float(raw.get("p")),
            s=_as_int(raw.get("s")),
            t=_parse_ts(raw.get("t")),
        )


@dataclass(frozen=True, slots=True)
class OptionContract:
    """One contract in a chain snapshot.

    OCC symbols parse into ``expiry``/``strike``/``right`` here rather than being
    re-parsed by every caller, because the T5 IV-rank tenor key is built from those
    three fields and a mismatch between "what we store" and "what we key on" would
    silently produce an empty history.

    ``symbol``    OCC symbol as Alpaca sent it (e.g. ``SPY251220C00680000``).
    ``expiry``    normalised ``YYYYMMDD``, matching Alpaca's expiry query params.

    ``underlying`` is parsed back out of the OCC root rather than passed in, so a
    chain response can never be mislabelled by a caller.
    """

    symbol: str
    underlying: str
    expiry: str
    strike: float
    right: Literal["call", "put"]
    implied_volatility: float | None
    greeks: Greeks
    latest_quote: OptionQuote
    latest_trade: OptionTrade
    daily_bar: Bar | None = None
    prev_daily_bar: Bar | None = None
    minute_bar: Bar | None = None

    @property
    def dte(self) -> int | None:
        day = self.expiry_date()
        if day is None:
            return None
        return (day - datetime.now(UTC).date()).days

    def expiry_date(self):
        try:
            return datetime.strptime(self.expiry, "%Y%m%d").date()
        except ValueError:
            return None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> OptionContract:
        symbol = _as_str(raw.get("symbol")) or ""
        underlying, expiry, strike, right = parse_occ_symbol(symbol)
        return cls(
            symbol=symbol,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
            implied_volatility=_as_float(raw.get("impliedVolatility")),
            greeks=Greeks.from_json(raw.get("greeks")),
            latest_quote=OptionQuote.from_json(raw.get("latestQuote")),
            latest_trade=OptionTrade.from_json(raw.get("latestTrade")),
            daily_bar=_bar_or_none(raw.get("dailyBar")),
            prev_daily_bar=_bar_or_none(raw.get("prevDailyBar")),
            minute_bar=_bar_or_none(raw.get("minuteBar")),
        )


@dataclass(frozen=True, slots=True)
class OptionChain:
    """A (possibly paged) options chain snapshot for one underlying."""

    underlying: str
    feed: str
    contracts: list[OptionContract]
    next_page_token: str | None = None
    fetched_at: datetime | None = None

    def __len__(self) -> int:
        return len(self.contracts)

    def __iter__(self):
        return iter(self.contracts)

    def calls(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.right == "call"]

    def puts(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.right == "put"]

    def strikes(self) -> list[float]:
        return sorted({c.strike for c in self.contracts})

    def for_expiry(self, expiry: str) -> list[OptionContract]:
        return [c for c in self.contracts if c.expiry == expiry]

    def expiries(self) -> list[str]:
        return sorted({c.expiry for c in self.contracts})

    def nearest_expiry(self, *, on_or_after: datetime | None = None) -> str | None:
        """Earliest expiry not before ``on_or_after`` (default: today, UTC).

        # INTERPRETATION: the reference date is a UTC date. Alpaca expiry timestamps
        # are exchange-local (America/New_York); for an after-hours decision the two
        # agree in every case this executor cares about, and a documented UTC choice
        # is better than an implicit local-time one.
        """
        cutoff = (on_or_after or datetime.now(UTC)).date()
        candidates = []
        for expiry in self.expiries():
            day = datetime.strptime(expiry, "%Y%m%d").date()
            if day >= cutoff:
                candidates.append(day)
        return min(candidates).strftime("%Y%m%d") if candidates else None

    def atm_contract(self, spot: float, expiry: str | None = None) -> OptionContract | None:
        """Strike nearest ``spot`` for ``expiry`` (default: nearest live expiry), calls first.

        # INTERPRETATION: ties break toward calls. Arm C is calls-only and arm B picks
        its own strike, so the only consumer of the ATM contract is the IV-rank tenor
        selection, and a deterministic tie-break keeps that key stable day to day.
        """
        pool = self.for_expiry(expiry) if expiry else self.contracts
        if not pool:
            return None
        best = min(pool, key=lambda c: (abs(c.strike - spot), 0 if c.right == "call" else 1))
        return best

    def extend(self, other: OptionChain) -> OptionChain:
        """Merge another page of the same chain, de-duplicating by contract symbol."""
        seen = {c.symbol for c in self.contracts}
        merged = list(self.contracts)
        merged.extend(c for c in other.contracts if c.symbol not in seen)
        return OptionChain(
            underlying=self.underlying,
            feed=self.feed,
            contracts=merged,
            next_page_token=other.next_page_token,
            fetched_at=other.fetched_at or self.fetched_at,
        )


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    status: str
    currency: str
    cash: float
    portfolio_value: float
    equity: float
    buying_power: float
    pattern_day_trader: bool
    trading_blocked: bool

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Account:
        return cls(
            id=_as_str(raw.get("id")) or "",
            status=_as_str(raw.get("status")) or "",
            currency=_as_str(raw.get("currency")) or "USD",
            cash=_as_float(raw.get("cash")) or 0.0,
            portfolio_value=_as_float(raw.get("portfolio_value")) or 0.0,
            equity=_as_float(raw.get("equity")) or 0.0,
            buying_power=_as_float(raw.get("buying_power")) or 0.0,
            pattern_day_trader=bool(raw.get("pattern_day_trader")),
            trading_blocked=bool(raw.get("trading_blocked")),
        )


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: float
    side: str
    market_value: float | None
    cost_basis: float | None
    avg_entry_price: float | None
    current_price: float | None
    unrealized_pl: float | None
    asset_class: str | None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Position:
        return cls(
            symbol=_as_str(raw.get("symbol")) or "",
            qty=_as_float(raw.get("qty")) or 0.0,
            side=_as_str(raw.get("side")) or "",
            market_value=_as_float(raw.get("market_value")),
            cost_basis=_as_float(raw.get("cost_basis")),
            avg_entry_price=_as_float(raw.get("avg_entry_price")),
            current_price=_as_float(raw.get("current_price")),
            unrealized_pl=_as_float(raw.get("unrealized_pl")),
            asset_class=_as_str(raw.get("asset_class")),
        )


@dataclass(frozen=True, slots=True)
class Clock:
    timestamp: datetime
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Clock:
        ts = _parse_ts(raw.get("timestamp"))
        if ts is None:
            raise AlpacaError("clock response is missing a timestamp")
        return cls(
            timestamp=ts,
            is_open=bool(raw.get("is_open")),
            next_open=_parse_ts(raw.get("next_open")),
            next_close=_parse_ts(raw.get("next_close")),
        )


def _parse_ts(value: Any) -> datetime | None:
    text = _as_str(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _bar_or_none(raw: Mapping[str, Any] | None) -> Bar | None:
    if not raw:
        return None
    try:
        return Bar.from_json(raw)
    except AlpacaError:
        return None


def parse_occ_symbol(symbol: str) -> tuple[str, str, float, Literal["call", "put"]]:
    """``SPY260116C00650000`` -> ``("SPY", "20260116", 650.0, "call")``.

    OCC layout is ``<root><YYMMDD><C|P><strike x1000>`` with the strike always eight
    digits, so the fields are counted from the **right** — which is what makes a
    three-letter root (SPY) and a four-letter root (BRKB) parse identically without
    guessing the root length.

    The two-digit year is expanded to four using the standard 70-year pivot (>= 70 is
    19xx). The rest of the project speaks in ``YYYYMMDD`` — that is the format Alpaca's
    ``expiration_gte``/``expiration_lte`` filters use and the format
    ``iv_rank.tenor_key`` keys on — so expanding here keeps one expiry representation
    everywhere instead of a ``260117`` in one module and ``20260117`` in another.
    """
    text = (symbol or "").strip().upper()
    if len(text) < 15:
        raise AlpacaError(f"not an OCC option symbol: {symbol!r}")
    right: Literal["call", "put"] = "call" if text[-9] == "C" else "put"
    if text[-9] not in ("C", "P"):
        raise AlpacaError(f"not an OCC option symbol (no C/P at position -9): {symbol!r}")
    try:
        yymmdd = text[-15:-9]
        parsed = datetime.strptime(yymmdd, "%y%m%d")
        expiry = f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"
        strike = float(text[-8:]) / 1000.0
    except ValueError as exc:
        raise AlpacaError(f"not an OCC option symbol: {symbol!r}") from exc
    root = text[:-15]
    if not root:
        raise AlpacaError(f"not an OCC option symbol (no underlying root): {symbol!r}")
    return root, expiry, strike, right


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlpacaClient:
    """Read-only Alpaca REST client.

    Construct with explicit credentials or let it read the two documented env vars.
    ``feed`` is a required argument on every bar call so a caller cannot get a SIP
    baseline and an IEX numerator by accident.
    """

    transport: Transport = field(repr=False)
    key: str = field(repr=False, default="")
    secret: str = field(repr=False, default="")
    data_base: str = DATA_BASE
    trading_base: str = PAPER_TRADING_BASE
    rate_limiter: RateLimiter | None = None
    blocking: bool = True

    @classmethod
    def from_env(cls, **kwargs: Any) -> AlpacaClient:
        """Build a client from ``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET``.

        Raises ``AlpacaConfigError`` naming the *variable*, never its value, when one
        is missing — a wrong key is a setup error we want loudly, and the message has
        to be safe to paste into a chat.
        """
        key = os.environ.get(KEY_ENV, "")
        secret = os.environ.get(SECRET_ENV, "")
        missing = [name for name, value in ((KEY_ENV, key), (SECRET_ENV, secret)) if not value]
        if missing:
            raise AlpacaConfigError(
                f"missing Alpaca credential env var(s): {', '.join(missing)} "
                f"(paper keys only — real-money keys never exist on this fleet)"
            )
        return cls(transport=UrllibTransport(), key=key, secret=secret, **kwargs)

    # -- plumbing --------------------------------------------------------------

    @property
    def headers(self) -> dict[str, str]:
        if not self.key or not self.secret:
            raise AlpacaConfigError(
                f"client has no credentials — use AlpacaClient.from_env() with "
                f"{KEY_ENV}/{SECRET_ENV} set, or pass key/secret explicitly"
            )
        return {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
        }

    def _get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        limiter = self.rate_limiter
        if limiter is not None:
            limiter.acquire(blocking=self.blocking)
        return self.transport.get_json(
            f"{base}{path}",
            headers=self.headers,
            params={k: v for k, v in (params or {}).items() if v is not None},
        )

    def __repr__(self) -> str:
        state = "configured" if (self.key and self.secret) else "unconfigured"
        return f"AlpacaClient({state}, data={self.data_base}, trading={self.trading_base})"

    __str__ = __repr__

    # -- market data -----------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        *,
        timeframe: Timeframe,
        feed: Feed,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        adjustment: Literal["raw", "split", "dividend", "all"] | None = None,
        page_token: str | None = None,
        asof: str | None = None,
    ) -> BarSeries:
        """``GET /v2/stocks/{symbol}/bars`` for one symbol.

        ``symbol`` is interpolated into the path (not sent as a query param — that is
        the multi-symbol route's contract, and using it here is what produced the live
        ``400: query parameter 'symbols' is required``).

        ``end`` defaults to now. On the free tier a SIP query needs ``end`` at least
        15 minutes old; :func:`sip_end_is_queryable` is the honest check and this
        method does **not** silently rewrite the caller's ``end`` — quietly moving a
        timestamp to make a request succeed would hide exactly the free-tier
        behaviour the research note flagged as unverified.
        """
        payload = self._get(
            self.data_base,
            stock_bars_path(symbol),
            {
                "timeframe": timeframe,
                "feed": feed,
                "start": start,
                "end": end,
                "limit": limit,
                "adjustment": adjustment,
                "page_token": page_token,
                "asof": asof,
            },
        )
        return _bar_series_from_payload(symbol, timeframe, feed, payload)

    def get_daily_bars(
        self,
        symbol: str,
        *,
        feed: Feed,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        adjustment: str | None = "all",
    ) -> BarSeries:
        return self.get_bars(
            symbol,
            timeframe="1Day",
            feed=feed,
            start=start,
            end=end,
            limit=limit,
            adjustment=adjustment,
        )

    def get_intraday_bars(
        self,
        symbol: str,
        *,
        timeframe: Timeframe,
        feed: Feed,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
    ) -> BarSeries:
        """Intraday bars. Same single-symbol route; ``timeframe`` selects the interval."""
        return self.get_bars(
            symbol,
            timeframe=timeframe,
            feed=feed,
            start=start,
            end=end,
            limit=limit,
            adjustment="raw",
        )

    # -- options ---------------------------------------------------------------

    def get_option_chain_page(
        self,
        underlying: str,
        *,
        feed: Feed = "indicative",
        limit: int | None = 100,
        updated_since: str | None = None,
        page_token: str | None = None,
        right: Literal["call", "put"] | None = None,
        strike_price_gte: float | None = None,
        strike_price_lte: float | None = None,
        expiration_date: str | None = None,
        expiration_date_gte: str | None = None,
        expiration_date_lte: str | None = None,
    ) -> OptionChain:
        """One page of ``GET /v1beta1/options/snapshots/{underlying_symbol}``.

        A full SPY chain is multi-page even at ``limit=1000``; use
        :meth:`get_option_chain` unless you specifically want one page.

        The filter parameters are named exactly as the reference names them
        (``strike_price_gte``, ``expiration_date_gte``, …). The plausible-looking
        ``strike_gte``/``expiration_gte`` pair does not exist on this route; Alpaca
        rejects unknown query parameters rather than ignoring them.
        """
        payload = self._get(
            self.data_base,
            f"{OPTIONS_SNAPSHOTS_PATH}/{urllib.parse.quote(underlying.upper(), safe='')}",
            {
                "feed": feed,
                "limit": limit,
                "updated_since": updated_since,
                "page_token": page_token,
                "type": right,
                "strike_price_gte": strike_price_gte,
                "strike_price_lte": strike_price_lte,
                "expiration_date": expiration_date,
                "expiration_date_gte": expiration_date_gte,
                "expiration_date_lte": expiration_date_lte,
            },
        )
        return _chain_from_payload(underlying, feed, payload)

    def get_option_chain(
        self,
        underlying: str,
        *,
        feed: Feed = "indicative",
        limit: int | None = 1000,
        max_pages: int = 10,
        **filters: Any,
    ) -> OptionChain:
        """All pages of a chain, de-duplicated, stopping at ``max_pages``.

        ``max_pages`` is a real stop, not a suggestion: an unbounded loop against a
        paged API is how a data layer turns into a wallet drain. If the chain is still
        paged when the cap is hit, ``next_page_token`` is left set on the result so
        the caller can record that it has an incomplete chain.
        """
        chain = self.get_option_chain_page(underlying, feed=feed, limit=limit, **filters)
        pages = 1
        token = chain.next_page_token
        while token and pages < max_pages:
            nxt = self.get_option_chain_page(
                underlying, feed=feed, limit=limit, page_token=token, **filters
            )
            chain = chain.extend(nxt)
            token = nxt.next_page_token
            pages += 1
        return chain

    # -- paper account (read-only) --------------------------------------------

    def get_account(self) -> Account:
        return Account.from_json(self._get(self.trading_base, ACCOUNT_PATH))

    def get_positions(self) -> list[Position]:
        payload = self._get(self.trading_base, POSITIONS_PATH)
        if not isinstance(payload, list):
            raise AlpacaError(f"expected a list of positions, got {type(payload).__name__}")
        return [Position.from_json(p) for p in payload]

    def get_clock(self) -> Clock:
        return Clock.from_json(self._get(self.trading_base, CLOCK_PATH))


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _bar_series_from_payload(symbol: str, timeframe: str, feed: str, payload: Any) -> BarSeries:
    """Parse the **single-symbol** bars envelope.

    The documented shape for ``/v2/stocks/{symbol}/bars`` is flat::

        {"bars": [{"t": ..., "o": ...}, ...], "symbol": "AAPL", "next_page_token": null}

    ``bars`` is a *list* here. The symbol-keyed object (``{"bars": {"AAPL": [...]}}``)
    belongs to the multi-symbol route ``/v2/stocks/bars?symbols=...``, which is a
    different route with a different contract. Accepting both is deliberate: reading
    only the documented shape would turn a silent API change into an empty series
    rather than an error, and reading only the keyed shape is what the old mock taught.
    """
    if not isinstance(payload, dict):
        raise AlpacaError(f"unexpected bars payload type {type(payload).__name__}")
    bars_block = payload.get("bars")
    if bars_block is None:
        raise AlpacaError("bars payload has no 'bars' object")
    if isinstance(bars_block, dict):
        raw_bars = bars_block.get(symbol) or bars_block.get(symbol.upper()) or []
    elif isinstance(bars_block, list):
        raw_bars = bars_block
    else:
        raise AlpacaError(f"bars payload has a 'bars' of unusable type {type(bars_block).__name__}")
    bars = [Bar.from_json(b) for b in raw_bars]
    # The API sorts by symbol then timestamp; we sort so we do not *assume* it.
    bars.sort(key=lambda b: b.t)
    return BarSeries(
        symbol=symbol,
        timeframe=timeframe,  # type: ignore[arg-type]
        feed=feed,
        bars=bars,
        next_page_token=_as_str(payload.get("next_page_token")),
    )


def _chain_from_payload(underlying: str, feed: str, payload: Any) -> OptionChain:
    """Parse the documented chain envelope:

    ``{"snapshots": {"SPY251220C00685000": {...snapshot...}}, "next_page_token": null}``

    ``snapshots`` is a **dict keyed by OCC contract symbol**, not a list, and there is
    no inner nesting. The keyed object is also the source of truth for the symbol:
    it is read from the key rather than from a field inside the snapshot, so a chain
    can never be mislabelled.
    """
    if not isinstance(payload, dict):
        raise AlpacaError(f"unexpected chain payload type {type(payload).__name__}")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, dict):
        raise AlpacaError("chain payload has no 'snapshots' object")
    contracts = []
    for occ_symbol, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        enriched = {"symbol": occ_symbol, **snapshot}
        contracts.append(OptionContract.from_json(enriched))
    return OptionChain(
        underlying=underlying.upper(),
        feed=feed,
        contracts=contracts,
        next_page_token=_as_str(payload.get("next_page_token")),
        fetched_at=datetime.now(UTC),
    )


def sip_end_is_queryable(
    end: datetime | None,
    *,
    now: datetime | None = None,
    min_age_minutes: float = SIP_HISTORY_MIN_AGE_MINUTES,
) -> bool:
    """Whether ``end`` is old enough for a free-tier SIP historical query.

    The documented rule: "For historical queries, the ``end`` parameter must be at
    least 15 minutes old to query SIP data without a subscription." A caller wanting
    SIP therefore has to pass an explicit historical ``end`` rather than leaning on
    the default — the behaviour the research note flagged as "documented but
    untested" and one live call still has to confirm.

    # INTERPRETATION: ``end=None`` returns False. Alpaca substitutes "now" for a
    missing end, and "now" sits inside the 15-minute subscription window.
    """
    if end is None:
        return False
    moment = end if end.tzinfo else end.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age = (reference - moment).total_seconds() / 60.0
    return age >= min_age_minutes


__all__ = [
    "ACCOUNT_PATH",
    "AlpacaAPIError",
    "AlpacaClient",
    "AlpacaConfigError",
    "AlpacaError",
    "Account",
    "Bar",
    "BarSeries",
    "CLOCK_PATH",
    "Clock",
    "DATA_BASE",
    "DOC_VERIFIED_ROUTES",
    "DocRoute",
    "Greeks",
    "KEY_ENV",
    "MockTransport",
    "OPTIONS_SNAPSHOTS_PATH",
    "OptionChain",
    "OptionContract",
    "OptionQuote",
    "OptionTrade",
    "PAPER_TRADING_BASE",
    "POSITIONS_PATH",
    "Position",
    "RATE_LIMIT_CALLS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "RateLimitError",
    "RateLimiter",
    "SECRET_ENV",
    "SIP_HISTORY_MIN_AGE_MINUTES",
    "STOCK_BARS_PATH",
    "Transport",
    "UrllibTransport",
    "match_doc_route",
    "parse_occ_symbol",
    "sip_end_is_queryable",
    "stock_bars_path",
]
