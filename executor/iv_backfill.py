"""Historical IV backfill for T5's rolling moneyness-keyed series (rulings R8 + R9).

What this does
--------------
For every session in a trailing window, it reconstructs **the contract each arm's IV gate
would have read on that day**, fetches that contract's historical daily bars, inverts the
close into an implied volatility through :mod:`executor.black_scholes`, and writes the
result into the T5 store under the RULED moneyness-keyed tenor with full provenance.

It exists because the warmup floor is real and expensive. ``MIN_OBSERVATIONS = 60`` at one
observation per session is sixty sessions of waiting, and T5's answer to an undefined rank
is PENDING, which *blocks* trades — so an un-warmed T5 does not merely degrade the
experiment, it stops it. Ruling R9 permits a backfilled observation to count toward the
60; this module is the thing that makes that ruling executable.

Why it is scoped to the trailing year and not the whole feed
------------------------------------------------------------
The depth probe established live that bars go back to February 2024
(``docs/reviews/2026-10-03-iv-backfill-depth-probe.md``) — 718 sessions. Backfilling all
of them would be strictly worse than useless: ``iv_rank`` reads a 365-day window, so
anything older than a year is *arithmetically discarded* (the feasibility doc §2b's first
reason). The default window is therefore ``lookback_days + slack`` rather than the feed
floor, and the report states how much history was deliberately left on the table.

The contract selection, stated as an interpretation
--------------------------------------------------
There is no point-in-time option chain (Alpaca serves none — feasibility doc §2a), so a
backfill must *choose* the contract the gate would have read, and that choice is not
recoverable — it is an inference. Two rules make it defensible:

1. **Expiry: the in-band expiry nearest the band's LOWER edge.** ``soak``'s
   ``arm_contract_for_band`` picks the contract nearest spot across the whole band. Every
   expiry in the band offers a strike essentially at spot, so that criterion ties across
   expiries and the real chain breaks the tie by iteration order — and Alpaca returns
   contracts ordered by expiry ascending. So the gate reads the *shortest-dated* in-band
   expiry in practice, and this module reproduces that. The alternative (some fixed offset
   into the band) would put the backfilled series under a DTE bucket the gate never reads,
   which is the R4 failure mode wearing a different hat.
2. **Strike: the enumerated strike nearest spot *that actually has a bar that day*.**
   Reconstruction asks for a $1-wide ladder (SPY's near-ATM spacing) centred on the
   session's own close and takes the nearest survivor. A strike the chain never listed
   has no bar, and its absence is data, not a failure.

Both are reported in the run report — the DTE bucket and moneyness bucket every
observation landed in — so the series is auditable without re-running the job.

BOTH RIGHTS ARE RECORDED. The soak deliberately does not filter by ``right`` (its own
docstring: "recording both sides is what lets a call-side rank warm without a second pass
tomorrow"), and the gate filters by direction at read time. Recording only one side would
leave half the gate's reads in permanent warmup for no benefit.

The honest limit: ARM B CANNOT BE BACKFILLED
--------------------------------------------
Arm B's frozen DTE band is ``0``. Black-Scholes vega is identically zero at expiry, so a
0-DTE close does not determine a volatility — :func:`executor.black_scholes.
implied_volatility` raises rather than invent one. This is a property of the instrument,
not a limitation of this code, and no amount of cleverness changes it: there is no
definition of "the implied volatility of an option expiring today" that is recoverable from
one daily print. So this pipeline warms **arm C** (90-180 DTE) and leaves **arm B** in
warmup, where its live EOD poll is the only instrument that can fill it. That asymmetry is
reported rather than papered over, because an arm silently stuck in warmup is the failure
this whole layer exists to prevent.

Provenance, per ruling R9
--------------------------
Every row written here carries ``source='bars_bs_inversion'`` and ``origin='backfill'``.
R9 counts these toward the 60 while stating that mixing them with live chain readings is
"a methodological splice". The splice is permitted *and marked*: the two can coexist for
the same session and key, so a reader can re-run the rank live-only and see the difference
rather than being told not to look.

Python 3.12+, stdlib only. Network I/O goes through the injected source, so the whole
pipeline is testable offline.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from executor.alpaca_client import (
    MAX_OPTION_SYMBOLS_PER_REQUEST,
    AlpacaAPIError,
    Bar,
    BarSeries,
    OptionBarSeries,
    chunk_option_symbols,
)
from executor.backfill_flow import (
    BACKFILL_ORIGIN,
    BAR_FEED,
    close_by_date,
    session_days,
)
from executor.black_scholes import BsError, IvNotInvertible, invert_bar_close
from executor.iv_rank import (
    BARS_INVERSION_SOURCE,
    DTE_BUCKET_DAYS,
    MONEYNESS_BUCKET_PCT,
    STRIKE_BUCKET_SIZE,
    IvObservation,
    IvRankStore,
    moneyness_bucket,
    moneyness_tenor_key,
    tenor_key,
)
from executor.position_manager import to_et

#: Alpaca's historical option data floor, confirmed live by the depth probe. Used as the
#: hard refusal bound when a caller asks for a window older than the feed.
FEED_FLOOR = dt.date(2024, 2, 1)

#: Extra sessions fetched beyond the lookback window. The lookback is *calendar* days and
#: the window is *sessions*, so 365 calendar days is only ~250 sessions; asking for the
#: lookback plus this slack means the store's own window filter — not this module — is
#: what decides what counts.
DEFAULT_SLACK_DAYS = 45

#: Reconstruction ladder. $1-wide near ATM is SPY's listed spacing; the span is the
#: moneyness bucket plus a margin, so the nearest-to-spot survivor is found even when the
#: close sits between strikes. See ``zero_dte_symbols`` for why the step is a parameter.
DEFAULT_STRIKE_STEP = 1.0
#: How wide a strike ladder to fetch around spot.
#:
#: This started at 3.0% and was cut to 0.75% for a reason that is worth more than the
#: constant: **the gate reads NEAREST-TO-SPOT** inside the DTE band (``soak.
#: arm_contract_for_band``, mirrored from ``hunt_plan._iv_contract_for_arm``). On a $1
#: strike grid nearest-to-spot is at most $0.50 away, which at SPY's price is well under
#: 0.1% — so the tenor key it produces is ``mny0.00`` on EVERY session, verified by
#: enumerating a year of plans, not assumed.
#:
#: A ±3% ladder therefore bought 50 chunks of requests to populate buckets the gate never
#: reads, against a feed that allows roughly ten requests per burst. The ladder is not
#: there to cover the moneyness axis; it is there so that when the nearest strike has no
#: bar there is a NEXT strike to fall back on. Ten ATM strikes is ample for that and is
#: 15 chunks instead of 50. Widen it with ``--range-pct`` if a future gate reads a
#: non-ATM bucket.
DEFAULT_LADDER_RANGE_PCT = 0.75

#: SPY daily closes come back split/dividend-adjusted (``adjustment="all"``). That is the
#: consistent choice against a Black-Scholes model that carries dividends as a yield: an
#: unadjusted close and an adjusted option price are not the same trade.
EQUITY_ADJUSTMENT = "all"

#: SIP bars on the free tier refuse a query whose ``end`` is less than 15 minutes old —
#: "subscription does not permit querying recent SIP data". 20 minutes is the same backoff
#: the live soak uses, and the same reasoning: the module does not silently rewrite the
#: caller's timestamp to make a request succeed, it asks for a window that is actually
#: answerable. Costs the last 20 minutes of today, which no completed daily bar needs.
EQUITY_END_BACKOFF_MINUTES = 20

#: Options bars have the SAME free-tier recency gate, and it is disguised. Alpaca refuses
#: ``/v1beta1/options/bars`` whose ``end`` falls within the last 15 minutes unless the
#: account pays for Algo Trader Plus — and the refusal text is ``OPRA agreement is not
#: signed``, which is not what that means (Alpaca staff, 2026-02-18, forum thread 18445:
#: "you are requesting real time OPRA data and do not have an Algo Trader Plus market data
#: subscription. Real time in this case means the latest 15 minutes."). The route takes no
#: ``feed`` parameter at all, so there is no indicative-feed escape hatch; ``end`` is the
#: only lever.
#:
#: MEASURED A/B, 2026-10-03, same second, same 100 symbols, same credential, ``end`` the
#: only variable: ``end=<today>T23:59:59Z`` → ``403 OPRA agreement is not signed``;
#: ``end=<now-20min>`` → ``200`` with all 100 symbols populated. Because ``end`` was
#: end-of-today, EVERY run 403'd on its first options call, deterministically, on any
#: credential. See ``docs/reviews/2026-10-03-opra-403-investigation.md``.
#:
#: Same 20 minutes as equities, for the same reason, and it is the right answer twice over:
#: the boundary is satisfied AND IV rank is a percentile over CLOSED sessions, so today's
#: partial session was never wanted.
OPTIONS_END_BACKOFF_MINUTES = 20

Right = str


def options_end_iso(end: dt.date, *, now: dt.datetime | None = None) -> str:
    """The ``end`` to send ``/v1beta1/options/bars``, clamped off the recency boundary.

    End-of-day on ``end`` is what the caller means, and it is also — whenever ``end`` is
    today — permanently inside Alpaca's 15-minute real-time gate. Clamping to
    ``min(end-of-day, now - OPTIONS_END_BACKOFF_MINUTES)`` keeps the caller's intent
    whenever ``end`` is in the past (the ordinary case: a 100-day backfill is bounded by
    its own end date, not by the clock) and yields an answerable window when it is today.

    Extracted as a function rather than inlined so the invariant is testable without an
    HTTP call: whatever the clock says, the returned ``end`` is never inside the window.
    """
    if now is None:
        now = dt.datetime.now(dt.UTC)
    end_of_day = dt.datetime.combine(end, dt.time(23, 59, 59), tzinfo=dt.UTC)
    latest_answerable = now - dt.timedelta(minutes=OPTIONS_END_BACKOFF_MINUTES)
    clamped = min(end_of_day, latest_answerable)
    return clamped.isoformat(timespec="seconds").replace("+00:00", "Z")


class IvBackfillError(RuntimeError):
    """The backfill cannot proceed at all (bad window, no credentials, no sessions)."""


class IvSource(Protocol):
    """The two reads this pipeline needs. Live and offline implementations both exist.

    ``page_token`` is part of the protocol rather than an internal detail because
    truncation is the one failure this pipeline cannot detect on its own: a dropped page
    looks exactly like a contract that stopped trading.
    """

    def equity_daily_bars(self, symbol: str, *, start: str, end: str) -> BarSeries: ...

    def option_daily_bars(
        self, symbols: Sequence[str], *, start: str, end: str, page_token: str | None = None
    ) -> OptionBarSeries: ...


#: Bounded retry policy for the options-bars route.
#:
#: **403 is NOT in this set, and that is the correction.** An earlier version of this file
#: (f2614da) read the 403 as a burst throttle in waves — ~10-15 successes, then a blanket
#: 403 for ~2 minutes, then the allowance back — and put 403 in here with a 180s doubling
#: cooldown on the strength of it. That reading was wrong. Root-caused in
#: ``docs/reviews/2026-10-03-opra-403-investigation.md``: the 403 was a 15-minute recency
#: gate on ``end`` wearing the text ``OPRA agreement is not signed``, which is not what
#: that text means. Three measurements refuted the throttle — the rate-limit budget never
#: dipped (199 → 193 across a 15-call burst), the 403s did not CONSUME rate budget, and
#: two calls in the same second with ``end`` as the only variable returned 403 and 200
#: respectively. A throttle cannot do that.
#:
#: So the honest classification is the boring one: a 403 is a refusal of the request as
#: written, and the request does not change on its own. Retrying it spends a 180s sleep
#: ladder in front of an answer that is never going to move — that is how two runs each
#: burned ~9 minutes to arrive at "no". It now fails in one call, which surfaces a genuine
#: entitlement problem immediately instead of nine minutes later.
#:
#: What survives from f2614da is worth keeping, and is kept because it is cheap rather
#: than because it was measured here:
#:
#: 1. **Pace.** Requests are spaced by :data:`DEFAULT_MIN_INTERVAL_SECONDS`. Volunarily
#:    spacing is strictly cheaper than discovering a limit by being throttled.
#: 2. **Bounded retries on genuinely transient statuses.** A 429 or a 5xx really can clear
#:    on its own, so those ramp on the short :data:`DEFAULT_RETRY_BASE_SECONDS`.
#:
#: The bound is the point either way. An unbounded loop is a storm, and a storm is how a
#: transient failure becomes a self-inflicted ban. Retries are counted and reported, so a
#: run that leaned on twenty of them is visibly a worse run than one that needed none.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_SECONDS = 5.0
DEFAULT_MIN_INTERVAL_SECONDS = 1.2


class AlpacaIvSource:
    """Live reads through the read-only client, with a call counter for the report."""

    def __init__(
        self,
        client,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.client = client
        self.calls = 0
        self.retries = 0
        #: Terminal 403s surfaced (not slept through). See :data:`RETRYABLE_STATUS`.
        self.refusals = 0
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.min_interval_seconds = min_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self._last_call = 0.0

    def _pace(self) -> None:
        """Wait out the remainder of the minimum interval since the last options call."""
        if self.min_interval_seconds <= 0:
            return
        elapsed = self._clock() - self._last_call
        if self._last_call and elapsed < self.min_interval_seconds:
            self._sleep(self.min_interval_seconds - elapsed)

    def equity_daily_bars(self, symbol: str, *, start: str, end: str) -> BarSeries:
        self.calls += 1
        return self.client.get_daily_bars(
            symbol,
            feed=BAR_FEED,  # type: ignore[arg-type]
            start=start,
            end=end,
            limit=10000,
            adjustment=EQUITY_ADJUSTMENT,
        )

    def option_daily_bars(
        self, symbols: Sequence[str], *, start: str, end: str, page_token: str | None = None
    ) -> OptionBarSeries:
        kwargs: dict[str, object] = {}
        if page_token is not None:
            kwargs["page_token"] = page_token
        for attempt in range(self.max_retries + 1):
            self._pace()
            self.calls += 1
            self._last_call = self._clock()
            try:
                return self.client.get_option_daily_bars(symbols, start=start, end=end, **kwargs)
            except AlpacaAPIError as exc:
                if exc.status == 403:
                    # Counted, not retried. The count is the operational fact worth
                    # keeping from the throttle era: "the run hit the wall N times" is
                    # still worth knowing. What is NOT worth keeping is the sleep ladder
                    # in front of it.
                    self.refusals += 1
                if exc.status not in RETRYABLE_STATUS or attempt == self.max_retries:
                    raise
                # Plain exponential ramp. Whatever is in RETRYABLE_STATUS is genuinely
                # transient — a 429 or a 5xx — and there is no longer a second, longer
                # ramp for a status we no longer consider recoverable.
                self.retries += 1
                self._sleep(self.retry_base_seconds * (2**attempt))
        raise AssertionError("retry loop exited without returning or raising")


# ---------------------------------------------------------------------------
# expiry enumeration
# ---------------------------------------------------------------------------


def third_friday(year: int, month: int) -> dt.date:
    """The third Friday of a month — SPY's quarterly (LEAPS) expiry.

    Named rather than inlined because getting it wrong silently drops the longest expiries
    from the candidate set, which is exactly where arm C's band lives.
    """
    first = dt.date(year, month, 1)
    offset = (4 - first.weekday()) % 7  # days to the first Friday
    return first + dt.timedelta(days=offset + 14)


def candidate_expiries(session: dt.date, dte_min: int, dte_max: int) -> list[dt.date]:
    """Every SPY expiry between ``dte_min`` and ``dte_max`` days after ``session``.

    SPY lists weeklies (every Friday) plus the quarterly third-Fridays. Both families are
    enumerated; the quarterly ones are already Fridays and are de-duplicated, so the set is
    a sorted list of distinct dates.

    # INTERPRETATION: this is a **reconstruction of a listing calendar, not a record of
    # one.** Alpaca serves no point-in-time chain, so the calendar cannot be read back for
    # a past session. Weeklies plus quarterlies is the rule SPY actually follows, but a
    # session where SPY did not list some weekly simply produces candidates with no bars —
    # which the pipeline treats as absence, not error, and the coverage report counts.
    """
    if dte_min > dte_max:
        raise IvBackfillError(f"empty DTE band: [{dte_min}, {dte_max}]")
    lo = session + dt.timedelta(days=dte_min)
    hi = session + dt.timedelta(days=dte_max)
    found: set[dt.date] = set()

    # Weeklies: every Friday in range.
    cursor = lo + dt.timedelta(days=(4 - lo.weekday()) % 7)
    while cursor <= hi:
        found.add(cursor)
        cursor += dt.timedelta(days=7)

    # Quarterlies: third Friday of every month the band touches.
    year, month = lo.year, lo.month
    while (year, month) <= (hi.year, hi.month):
        candidate = third_friday(year, month)
        if lo <= candidate <= hi:
            found.add(candidate)
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return sorted(found)


def select_expiry(session: dt.date, dte_min: int, dte_max: int) -> dt.date | None:
    """The in-band expiry the gate's nearest-to-spot tie-break would land on.

    See the module docstring: every in-band expiry offers a strike at spot, so the soak's
    nearest-strike criterion ties and the live chain's expiry-ascending order decides.
    Shortest-dated in-band expiry is that decision.
    """
    expiries = candidate_expiries(session, dte_min, dte_max)
    return expiries[0] if expiries else None


def ladder_strikes(
    spot: float,
    *,
    strike_step: float = DEFAULT_STRIKE_STEP,
    range_pct: float = DEFAULT_LADDER_RANGE_PCT,
) -> tuple[float, ...]:
    """The reconstructed strike ladder for one session, centred on that session's close.

    Snapped **up** to a multiple of ``strike_step`` at the low end (``ceil(lo/step)*step``)
    so the span is symmetric to within one step and never asks for a strike outside the
    window. Deliberately the same rule :func:`executor.backfill_flow.zero_dte_symbols`
    uses, and the symbols are then built from *these* values rather than re-derived beside
    them — two ladders computed by two pieces of code is a class of bug where the strike you
    priced is not the strike you requested.

    # INTERPRETATION: $1-wide is SPY's listed near-ATM spacing, and the width is sized to
    # the GATE rather than to caution -- see DEFAULT_LADDER_RANGE_PCT for why that is 0.75%
    # and not 3%. Both are *parameters*, and every run reports requested-vs-returned coverage
    # per session so the size of any reconstruction error is visible instead of assumed.
    """
    if spot <= 0:
        raise IvBackfillError(f"spot must be > 0 to build a strike ladder, got {spot}")
    if strike_step <= 0:
        raise IvBackfillError(f"strike step must be > 0, got {strike_step}")
    if range_pct <= 0:
        raise IvBackfillError(f"range_pct must be > 0, got {range_pct}")
    low = spot * (1.0 - range_pct / 100.0)
    high = spot * (1.0 + range_pct / 100.0)
    strike = math.ceil(low / strike_step) * strike_step
    out: list[float] = []
    while strike <= high + 1e-9:
        out.append(round(strike, 4))
        strike += strike_step
    if not out:
        # A strike step wider than the range produces an empty ladder, and an empty ladder
        # would flow through as a session with zero requested contracts -- reported as "this
        # contract did not trade" when the truth is "we asked for nothing". Misconfiguration
        # must not wear the costume of a market observation.
        raise IvBackfillError(
            f"strike step {strike_step} is wider than the {range_pct}% window around spot "
            f"{spot}; the ladder would be empty. Narrow the step or widen the range."
        )
    return tuple(out)


def occ_symbol(underlying: str, expiry: dt.date, right: Right, strike: float) -> str:
    """One OCC contract symbol: ``SPY`` + ``YYMMDD`` + ``C|P`` + 8-digit strike.

    An 8-digit strike is not cosmetic: a 9-digit one is a hard ``400 invalid symbol`` from
    the route, which the depth probe hit before it learned this.
    """
    scaled = int(round(strike * 1000))
    if scaled <= 0:
        raise IvBackfillError(f"strike must be > 0, got {strike}")
    return f"{underlying.upper()}{expiry.strftime('%y%m%d')}{right[0].upper()}{scaled:08d}"


# ---------------------------------------------------------------------------
# per-session reconstruction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """Everything needed to fetch and invert one session, derived without any I/O."""

    session: dt.date
    spot: float
    expiry: dt.date
    symbols: tuple[str, ...]
    strikes: tuple[float, ...]


@dataclass(slots=True)
class SessionReport:
    """What happened to one session. Kept whole so an audit can read the misses too."""

    session: dt.date
    spot: float | None = None
    expiry: dt.date | None = None
    contracts_requested: int = 0
    contracts_with_bar: int = 0
    observations_written: int = 0
    tenor_keys: list[str] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1


@dataclass(slots=True)
class BackfillReport:
    """Whole-run summary — the honest answer to "did this work and how sure are we"."""

    start: dt.date
    end: dt.date
    sessions_seen: int = 0
    sessions_without_close: int = 0
    sessions_written: int = 0
    observations_written: int = 0
    api_calls: int = 0
    tenor_key_counts: dict[str, int] = field(default_factory=dict)
    origin_counts: dict[str, int] = field(default_factory=dict)
    sessions: list[SessionReport] = field(default_factory=list)
    #: Retries the source needed. Reported because a run that leaned on twenty is a worse
    #: run than one that needed none, even when both produce the same rows.
    retries: int = 0
    #: Terminal 403s that surfaced rather than being slept through. Under the corrected
    #: diagnosis a 403 is a permanent refusal of the request as written, so a non-zero
    #: count is a bug in the request (a recency clamp that regressed, or a real entitlement
    #: change) rather than weather to be waited out.
    refusals: int = 0
    #: ``(arm, dte_min, dte_max, sessions_planned)`` per band, so an empty band is visible
    #: in the report instead of silently contributing nothing.
    bands_seen: list[tuple[str, int, int, int]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"iv backfill {self.start} -> {self.end}",
            f"  sessions seen           : {self.sessions_seen}",
            f"  sessions without close  : {self.sessions_without_close}",
            f"  sessions with >=1 obs   : {self.sessions_written}",
            f"  observations written    : {self.observations_written}",
            f"  api calls               : {self.api_calls}",
            f"  retries (transient 429/5xx): {self.retries}",
            f"  terminal 403 refusals    : {self.refusals}",
            f"  bands (arm, dte, sessions): {self.bands_seen}",
            f"  origins                 : {self.origin_counts}",
            "  tenor key -> observations:",
        ]
        for key, count in sorted(
            self.tenor_key_counts.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"    {key:<34} {count:>5}")
        return "\n".join(lines)


def plan_sessions(
    closes: dict[dt.date, float],
    *,
    underlying: str,
    dte_min: int,
    dte_max: int,
    start: dt.date,
    end: dt.date,
    strike_step: float = DEFAULT_STRIKE_STEP,
    range_pct: float = DEFAULT_LADDER_RANGE_PCT,
) -> list[SessionPlan]:
    """Turn a session->close map into fetch plans. No I/O, so this is unit-testable."""
    plans: list[SessionPlan] = []
    for session in session_days(start, end):
        spot = closes.get(session)
        if spot is None:
            # No SPY bar on this weekday == holiday. Skipping is correct and is not a
            # failure: the same rule the flow backfill uses, and the reason not to guess a
            # holiday calendar.
            continue
        expiry = select_expiry(session, dte_min, dte_max)
        if expiry is None:
            continue
        strikes = ladder_strikes(spot, strike_step=strike_step, range_pct=range_pct)
        symbols = tuple(
            occ_symbol(underlying, expiry, right, strike)
            for strike in strikes
            for right in ("call", "put")
        )
        plans.append(
            SessionPlan(
                session=session,
                spot=spot,
                expiry=expiry,
                symbols=symbols,
                strikes=strikes,
            )
        )
    return plans


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def _bars_index(
    series: OptionBarSeries, plans: Sequence[SessionPlan], *, start: dt.date, end: dt.date
) -> dict[str, dict[dt.date, Bar]]:
    """``symbol -> {session -> bar}`` over the plans' sessions only.

    The route returns bars stamped at 04:00Z; sessions are identified by their **ET** date,
    which is what :func:`executor.backfill_flow.close_by_date` already does for equities
    and what the live soak stamps its rows with. Using UTC here would shift every
    observation onto the previous day for an afternoon-stamped bar, which is the kind of
    one-day skew that a percentile series never notices and always inherits.
    """
    wanted = {plan.session for plan in plans}
    index: dict[str, dict[dt.date, Bar]] = {}
    for symbol, bars in series.bars_by_symbol.items():
        for bar in bars:
            day = to_et(bar.t).date()
            if day in wanted and start <= day <= end:
                index.setdefault(symbol, {})[day] = bar
    return index


def observations_for_session(
    plan: SessionPlan,
    bars: dict[str, dict[dt.date, Bar]],
    *,
    underlying: str,
    dte_bucket_days: int = DTE_BUCKET_DAYS,
    moneyness_pct: float = MONEYNESS_BUCKET_PCT,
    strike_bucket_size: float = STRIKE_BUCKET_SIZE,
) -> tuple[list[IvObservation], SessionReport]:
    """Invert one session's surviving contracts into observations under both tenor keys.

    Mirrors ``soak._observation_for``: every reading is written TWICE — once under the
    expiry-keyed ``$5``-bucketed key (the audit series, which dies with the expiry) and once
    under the RULED moneyness-keyed rolling key (the series T5 reads). Cost is one extra
    row per contract and the benefit is that the approximation stays measurable against the
    exact thing.

    Nearest-to-spot is decided on **liquidity and presence**, not on the enumeration order:
    a contract that has a bar and traded is a better reconstruction of "the contract the
    chain's nearest strike pointed at" than one that was never listed.
    """
    dte = (plan.expiry - plan.session).days
    report = SessionReport(
        session=plan.session, spot=plan.spot, expiry=plan.expiry,
        contracts_requested=len(plan.symbols),
    )
    if dte <= 0:
        # The zero-time refusal, reached through the session loop rather than the solver.
        report.skip("zero_dte_no_invertible_iv")
        return [], report

    present: dict[float, dict[str, Bar]] = {}
    for strike in plan.strikes:
        for right in ("call", "put"):
            symbol = occ_symbol(underlying, plan.expiry, right, strike)
            bar = bars.get(symbol, {}).get(plan.session)
            if bar is not None:
                present.setdefault(strike, {})[right] = bar
    report.contracts_with_bar = sum(len(v) for v in present.values())
    if not present:
        report.skip("no_contract_with_a_bar")
        return [], report

    # Nearest to spot, calls/ties broken by the ladder order (lowest strike first) exactly
    # as OptionChain.atm_contract does on the live side.
    nearest_strike = min(present, key=lambda s: (abs(s - plan.spot), s))
    nearest = present[nearest_strike]
    bucket = moneyness_bucket(nearest_strike, plan.spot, moneyness_pct)

    observations: list[IvObservation] = []
    for right in ("call", "put"):
        bar = nearest.get(right)
        if bar is None:
            report.skip(f"no_{right}_bar_at_nearest_strike")
            continue
        try:
            result = invert_bar_close(
                close=bar.c,
                volume=bar.v,
                spot=plan.spot,
                strike=nearest_strike,
                dte=dte,
                right=right,
            )
        except (IvNotInvertible, BsError) as exc:
            # Refusals are counted and named, never swallowed into silence.
            report.skip(type(exc).__name__)
            continue
        common = {
            "underlying": underlying.upper(),
            "as_of": plan.session.isoformat(),
            "iv": result.iv,
            "source": BARS_INVERSION_SOURCE,
            "origin": BACKFILL_ORIGIN,
            "is_proxy": False,
            "expiry": plan.expiry.strftime("%Y%m%d"),
            "right": right,
            "strike": nearest_strike,
        }
        observations.append(
            IvObservation(
                tenor_key=tenor_key(
                    expiry=plan.expiry.strftime("%Y%m%d"),
                    right=right,
                    strike=nearest_strike,
                    bucket_size=strike_bucket_size,
                ),
                **common,
            )
        )
        observations.append(
            IvObservation(
                tenor_key=moneyness_tenor_key(
                    dte=dte,
                    right=right,
                    moneyness_bucket=bucket,
                    dte_bucket_days=dte_bucket_days,
                ),
                **common,
            )
        )
    report.observations_written = len(observations)
    report.tenor_keys = sorted({o.tenor_key for o in observations})
    return observations, report


def run_backfill(
    source: IvSource,
    store: IvRankStore,
    *,
    underlying: str = "SPY",
    start: dt.date,
    end: dt.date,
    bands: Sequence[tuple[str, int, int]],
    dte_bucket_days: int = DTE_BUCKET_DAYS,
    moneyness_pct: float = MONEYNESS_BUCKET_PCT,
    strike_bucket_size: float = STRIKE_BUCKET_SIZE,
    strike_step: float = DEFAULT_STRIKE_STEP,
    range_pct: float = DEFAULT_LADDER_RANGE_PCT,
    dry_run: bool = False,
) -> BackfillReport:
    """Backfill every arm band in ``bands`` over ``[start, end]`` into ``store``.

    Two passes over the API rather than one per session: the equity closes are read once
    for the whole window (the bar for a date IS the evidence a session happened), and then
    **all** candidate option symbols are de-duplicated across the entire window and fetched
    in ``MAX_OPTION_SYMBOLS_PER_REQUEST`` chunks. A single OCC symbol's bars cover its
    whole life, so asking once for its full history serves every session that used it —
    which turns a few hundred per-session requests into a couple of dozen.

    Pagination is followed explicitly. A 100-symbol chunk across a 400-session window can
    exceed one page of 10,000 bars, and silently truncating that would drop the back half
    of a contract's history while still writing rows for the front half — an undercount
    that looks like a real gap in the series.
    """
    if start > end:
        raise IvBackfillError(f"start {start} is after end {end}")
    if start < FEED_FLOOR:
        raise IvBackfillError(
            f"requested start {start} precedes the confirmed option-data floor {FEED_FLOOR} "
            f"(docs/reviews/2026-10-03-iv-backfill-depth-probe.md). Alpaca answers 200 with "
            f"an EMPTY bars map before that date, so the sessions would be recorded as "
            f"having traded nothing."
        )

    report = BackfillReport(start=start, end=end)
    equity_end = (
        dt.datetime.now(dt.UTC) - dt.timedelta(minutes=EQUITY_END_BACKOFF_MINUTES)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    equities = source.equity_daily_bars(
        underlying, start=start.isoformat(), end=equity_end
    )
    closes = close_by_date(equities)
    report.api_calls += 1

    weekdays = len(session_days(start, end))
    for arm, dte_min, dte_max in bands:
        plans = plan_sessions(
            closes,
            underlying=underlying,
            dte_min=dte_min,
            dte_max=dte_max,
            start=start,
            end=end,
            strike_step=strike_step,
            range_pct=range_pct,
        )
        report.bands_seen.append((arm, dte_min, dte_max, len(plans)))
        report.sessions_seen += len(plans)
        report.sessions_without_close += weekdays - len(plans)
        if not plans:
            continue

        symbols: list[str] = []
        seen: set[str] = set()
        for plan in plans:
            for symbol in plan.symbols:
                if symbol not in seen:
                    seen.add(symbol)
                    symbols.append(symbol)

        index: dict[str, dict[dt.date, Bar]] = {}
        for chunk in chunk_option_symbols(symbols, MAX_OPTION_SYMBOLS_PER_REQUEST):
            merged: dict[str, list[Bar]] = {}
            page_token: str | None = None
            while True:
                series = source.option_daily_bars(
                    chunk,
                    start=start.isoformat(),
                    end=options_end_iso(end),
                    page_token=page_token,
                )
                report.api_calls += 1
                for symbol, bars in series.bars_by_symbol.items():
                    merged.setdefault(symbol, []).extend(bars)
                page_token = series.next_page_token
                if not page_token:
                    break
            index.update(
                _bars_index(
                    OptionBarSeries(bars_by_symbol=merged), plans, start=start, end=end
                )
            )
        report.retries = getattr(source, "retries", 0)
        report.refusals = getattr(source, "refusals", 0)

        for plan in plans:
            observations, session_report = observations_for_session(
                plan,
                index,
                underlying=underlying,
                dte_bucket_days=dte_bucket_days,
                moneyness_pct=moneyness_pct,
                strike_bucket_size=strike_bucket_size,
            )
            report.sessions.append(session_report)
            if not observations:
                continue
            report.sessions_written += 1
            report.observations_written += len(observations)
            report.origin_counts[BACKFILL_ORIGIN] = (
                report.origin_counts.get(BACKFILL_ORIGIN, 0) + len(observations)
            )
            for observation in observations:
                report.tenor_key_counts[observation.tenor_key] = (
                    report.tenor_key_counts.get(observation.tenor_key, 0) + 1
                )
            if not dry_run:
                store.record_many(observations)
    return report


def _page(
    source: IvSource, chunk: Sequence[str], start: dt.date, end: dt.date, page_token: str
) -> OptionBarSeries:
    """Deprecated shim kept out of the run loop; pagination is part of :class:`IvSource`."""
    raise NotImplementedError(
        "pagination is handled through IvSource.option_daily_bars(page_token=...)"
    )


__all__ = [
    "DEFAULT_LADDER_RANGE_PCT",
    "DEFAULT_SLACK_DAYS",
    "DEFAULT_STRIKE_STEP",
    "FEED_FLOOR",
    "AlpacaIvSource",
    "BackfillReport",
    "IvBackfillError",
    "IvSource",
    "SessionPlan",
    "SessionReport",
    "candidate_expiries",
    "ladder_strikes",
    "occ_symbol",
    "observations_for_session",
    "options_end_iso",
    "plan_sessions",
    "run_backfill",
    "select_expiry",
    "third_friday",
]