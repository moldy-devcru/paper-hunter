"""Base-rate measurement for the frozen checklist — how often does T1..T6 actually fire?

Why this module exists
----------------------
The outsider review (``docs/reviews/2026-10-03-algo-outsider-review.md`` §2, §5.2,
§6.2) argues from standalone daily pass rates that the six ANDed blocking conditions
multiply out to roughly one qualifying day per few thousand sessions, and recommends
measuring the real joint rate before the window opens. That claim is arithmetic done on
*guesses* about the standalone rates. This module replaces the guesses with a replay of
the frozen evaluator over real bars, so the number that decides whether an empty Monday
is news or arithmetic is a measurement rather than an estimate.

What is measured, and how honestly
-----------------------------------
The measurement replays :func:`executor.checklist.evaluate` — the *actual* frozen
evaluator, not a reimplementation of it. Every threshold comes from the loaded
``Rulebook``; nothing here duplicates a number that lives in the rulebook. If the
rulebook changes, this script's numbers change with it, which is the only way a
base-rate table stays honest between strategy versions.

Causality: why one pass over the history is exact, not an approximation
-------------------------------------------------------------------------
Every indicator the checklist reads is **causal** and **prefix-stable**: the value at
bar ``i`` depends only on bars ``0..i``, and does not depend on how many bars come
after it. That holds because each one is seeded from the *start* of the series
(``indicators.ema`` seeds with the SMA of its own first ``period`` values,
``indicators.rsi`` seeds from the first ``period`` deltas, ``indicators.macd`` seeds
its signal from the defined tail of the line, ``indicators.bollinger`` is a trailing
window, ``snapshot_builder.build_relvol`` and ``indicators.bandwidth_percentile`` both
read trailing windows that exclude the current bar, and
``snapshot_builder.find_macd_cross`` scans newest-first and so reports the most recent
cross at or before ``i``).

So computing each aligned indicator series **once** over the whole history and reading
position ``i`` yields bit-identical values to calling ``build_snapshot`` on the prefix
``bars[:i+1]`` — at O(n) instead of O(n²). That equivalence is not assumed:
:func:`verify_against_builder` re-derives a sample of indices through the real
``build_snapshot`` on truncated series and asserts the checklist statuses match. The
CLI runs it on every execution and prints the result, so the fast path cannot silently
drift away from production.

The three honest limitations
----------------------------
1. **T5's IV rank is a VIX proxy.** No free source has point-in-time historical SPY
   option IV, which is exactly why ``executor/iv_rank.py`` ships a VIX proxy seeder.
   This module ranks the Cboe VIX close over the same 365-day window and the same
   strictly-below percentile convention ``IvRankStore.iv_rank`` uses. VIX and SPY ATM
   option IV rank are strongly but not perfectly correlated, so the T5 pass rate here is
   an *estimate of a gate*, not a measurement of the gate. The proxy flag is carried in
   the output so no reader can mistake one for the other.
2. **T5's event-day leg is not replayed.** The repo's event calendar covers 2026-Q4
   only (``data/events/2026-Q4.yaml``), so there is no historical FOMC/CPI calendar to
   veto against. The event leg is reported as a separate session-count haircut derived
   from the calendar's own declared annual cadence, never folded silently into the
   measured rate.
3. **T6 cannot be measured at all.** There is no historical deep-OTM 0DTE volume series
   in existence for this project — ``data/README.md`` says so outright — and the frozen
   rulebook still has ``t6_flow.multiplier.calibration_pending: true``. A PENDING T6
   blocks. So arm B's joint rate as the rulebook stands *today* is structurally zero,
   no matter what the market did, and that is a fact about the frozen rules rather than
   a measurement. This module reports it as such instead of inventing a flow history.

Daily resolution also means the T2b fresh-cross guard (24h) is inert — see
:data:`CROSS_GUARD_HOURS_NOTE`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from config.loader import Rulebook
from executor.checklist import (
    BollingerState,
    ChecklistResult,
    FlowGate,
    IndicatorSnapshot,
    evaluate,
)
from executor.indicators import (
    InsufficientData,
    bollinger,
    ema,
    macd,
    relative_volume,
    rsi,
)

Basis = Literal["plan", "eod"]

#: Which bar the snapshot is built on.
#:
#: ``plan``  — the signal bar is the **prior** close (``bars[i-1]``), which is what
#:             ``hunt_plan.build_hunt_plan`` actually evaluates: the plan is built
#:             pre-market, so for session ``i`` it sees every bar up to and including
#:             session ``i-1``. This is the basis that governs "will Monday's plan go
#:             green", so it is the default and the headline number.
#: ``eod``   — the signal bar is session ``i``'s own completed close. Reported as a
#:             secondary series because it is the more common backtest convention and
#:             the outsider review's numbers are closer to it; showing both keeps the
#:             comparison to the review honest instead of comparing unlike things.
BASES: tuple[Basis, ...] = ("plan", "eod")

#: The checklist condition ids this module reports individually, in rulebook order.
#: ``T3a``/``T3b`` are the two arms of the OR group the evaluator resolves (see the FIX
#: note in ``checklist.evaluate`` — T3 was previously ANDed, making it unsatisfiable).
#: ``T3`` is the *group*, which is not an id the evaluator emits; it is derived per
#: session as "T3a PASS or T3b PASS", which is exactly the ``satisfied_if_any_of``
#: semantics. See :func:`group_status`.
CONDITIONS: tuple[str, ...] = ("T1", "T2", "T2b", "T3a", "T3b", "T3", "T4", "T5", "T6")

#: The conditions that form the T3 OR group, in rulebook order.
T3_GROUP: tuple[str, ...] = ("T3a", "T3b")

#: The checklist at *rulebook* granularity — the seven entries ``checklist.for_arm``
#: returns — which is what a marginal-contribution analysis has to work at. T3a and T3b
#: are collapsed into T3 first; treating them as two independent gates would count the
#: same OR group twice and make T3 look like the dominant constraint for a reason that
#: is an artefact of the reporting granularity.
RULEBOOK_CONDITIONS: tuple[str, ...] = ("T1", "T2", "T2b", "T3", "T4", "T5", "T6")

#: Non-blocks for these arms, per the rulebook's ``applies_to``.
SKIPPED_BY_ARM: dict[str, frozenset[str]] = {
    "B": frozenset(),
    "C": frozenset({"T6"}),
}

#: Bollinger parameters. Structural to the frozen T3 wording ("2 sigma", 20 periods)
#: rather than rulebook fields, so they are module constants — and the same ones
#: ``snapshot_builder.build_snapshot`` passes to ``indicators.bollinger`` by default.
BOLLINGER_PERIOD = 20
BOLLINGER_NUM_STD = 2.0

CROSS_GUARD_HOURS_NOTE = (
    "on daily bars consecutive sessions are 24h apart (72h across a weekend), and the "
    "T2b fresh-cross guard vetoes only when the adverse cross is strictly younger than "
    "24h — so a cross on the immediately preceding bar sits at exactly 24.0h and does "
    "not fire, and anything older is further from the window still. The guard is "
    "therefore effectively inert at daily resolution and its measured veto rate is ~0. "
    "That is a property of measuring on daily bars, not evidence that the guard is "
    "harmless intraday, where the bars are minutes apart."
)


# ---------------------------------------------------------------------------
# input rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarRow:
    """One daily bar. Mirrors ``executor.alpaca_client.Bar`` minus the fields the
    checklist never reads, so the analysis input stays a plain record."""

    t: dt.datetime
    o: float
    h: float
    l: float  # noqa: E741 - mirrors ``alpaca_client.Bar``, where the wire field is "l"
    c: float
    v: float


@dataclass(frozen=True, slots=True)
class VixRow:
    """One daily VIX close, used only as T5's IV-rank proxy input."""

    day: dt.date
    close: float


# ---------------------------------------------------------------------------
# indicator pre-pass (causal, prefix-stable — see the module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndicatorStack:
    """Every aligned indicator series the checklist reads, computed once.

    Aligned and oldest-first, matching ``executor.indicators``' own contract:
    ``series[i]`` is ``None`` where the indicator is not yet defined at bar ``i``.
    """

    closes: list[float]
    highs: list[float]
    lows: list[float]
    volumes: list[float]
    times: list[dt.datetime]
    ema_fast: list[float | None]
    ema_slow: list[float | None]
    rsi_values: list[float | None]
    macd_line: list[float | None]
    macd_signal: list[float | None]
    macd_hist: list[float | None]
    bb_middle: list[float | None]
    bb_upper: list[float | None]
    bb_lower: list[float | None]
    bb_bandwidth: list[float | None]
    #: Index of the most recent MACD line/signal sign change at or before each bar,
    #: or ``None``. Precomputed by one forward scan instead of calling
    #: ``snapshot_builder.find_macd_cross`` per session, which would recompute the
    #: whole MACD over the whole prefix on every bar (O(n^2)). Equivalent by
    #: construction — ``find_macd_cross`` on a prefix of length ``i+1`` scans
    #: ``i, i-1, ... 1`` newest-first and stops at the first valid bar, which is exactly
    #: "the most recent sign change at or before i" — and
    #: :func:`verify_against_builder` checks that claim against the real function on
    #: every run rather than trusting it.
    macd_cross_index: list[int | None]
    macd_cross_direction: list[str | None]

    def __len__(self) -> int:
        return len(self.closes)


def _macd_cross_scan(m: Any) -> tuple[list[int | None], list[str | None]]:
    """Most-recent MACD crossover at or before each bar, in one forward pass.

    Mirrors ``snapshot_builder.find_macd_cross``'s skip rule: a bar qualifies only when
    both ``macd - signal`` values are defined, non-zero, and differ.
    """
    n = len(m.macd_line)
    index_at: list[int | None] = [None] * n
    direction_at: list[str | None] = [None] * n
    last_index: int | None = None
    last_direction: str | None = None
    for i in range(n):
        if last_index is not None:
            index_at[i] = last_index
            direction_at[i] = last_direction
        current = (
            m.macd_line[i] - m.signal_line[i]
            if m.macd_line[i] is not None and m.signal_line[i] is not None
            else None
        )
        if current is None or current == 0 or i == 0:
            continue
        prior = (
            m.macd_line[i - 1] - m.signal_line[i - 1]
            if m.macd_line[i - 1] is not None and m.signal_line[i - 1] is not None
            else None
        )
        if prior is None or prior == 0 or current == prior:
            continue
        last_index = i
        last_direction = "call" if current > prior else "put"
    return index_at, direction_at


def build_stack(bars: Sequence[BarRow], rules: Rulebook) -> IndicatorStack:
    """Compute every aligned indicator series once over the whole history."""
    closes = [b.c for b in bars]
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    volumes = [b.v for b in bars]
    times = [b.t for b in bars]

    t1 = rules.checklist.t1_ema_alignment
    t2 = rules.checklist.t2_rsi
    t2b = rules.checklist.t2b_macd

    fast = ema(closes, t1.ema_fast)
    slow = ema(closes, t1.ema_slow)
    rsi_values = rsi(closes, t2.period)
    m = macd(closes, t2b.fast, t2b.slow, t2b.signal)
    # Bollinger period/std are structural to the frozen T3 wording ("2 sigma", 20
    # periods) rather than rulebook fields, so they are the module constants that
    # ``snapshot_builder.build_snapshot`` also uses verbatim.
    bb = bollinger(closes, BOLLINGER_PERIOD, BOLLINGER_NUM_STD)
    cross_index, cross_direction = _macd_cross_scan(m)
    return IndicatorStack(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        times=times,
        ema_fast=fast,
        ema_slow=slow,
        rsi_values=rsi_values,
        macd_line=m.macd_line,
        macd_signal=m.signal_line,
        macd_hist=m.histogram,
        bb_middle=bb.middle,
        bb_upper=bb.upper,
        bb_lower=bb.lower,
        bb_bandwidth=bb.bandwidth,
        macd_cross_index=cross_index,
        macd_cross_direction=cross_direction,
    )


# ---------------------------------------------------------------------------
# VIX proxy rank (T5)
# ---------------------------------------------------------------------------


class VixRankProxy:
    """T5's ``iv_rank`` over the Cboe VIX close, in ``IvRankStore``'s own convention.

    Convention copied from ``executor/iv_rank.IvRankStore.iv_rank``: percentile rank is
    the percentage of window observations **strictly below** the current reading, over a
    trailing ``lookback_days``-**calendar**-day window ending at (and including) the
    scoring session. ``min_observations`` is ``iv_rank.MIN_OBSERVATIONS`` — below the
    floor the real store returns ``warmup`` and the checklist turns that into PENDING,
    which blocks. The proxy reproduces that rather than scoring a short window and
    pretending it is warm.
    """

    def __init__(
        self,
        rows: Sequence[VixRow],
        *,
        lookback_days: int = 365,
        min_observations: int = 60,
    ) -> None:
        self._rows = sorted(rows, key=lambda r: r.day)
        self._closes = [r.close for r in self._rows]
        self._days = [r.day for r in self._rows]
        self.lookback_days = lookback_days
        self.min_observations = min_observations

    def rank(self, on: dt.date) -> float | None:
        """IV-rank proxy for session ``on``, or ``None`` while below the warmup floor."""
        index = None
        for i in range(len(self._days) - 1, -1, -1):
            if self._days[i] <= on:
                index = i
                break
        if index is None:
            return None
        current = self._closes[index]
        cutoff = self._days[index] - dt.timedelta(days=self.lookback_days)
        window = [
            value
            for day, value in zip(
                self._days[: index + 1], self._closes[: index + 1], strict=True
            )
            if day >= cutoff
        ]
        if len(window) < self.min_observations:
            return None
        below = sum(1 for value in window if value < current)
        return 100.0 * below / len(window)

    @property
    def coverage(self) -> tuple[dt.date | None, dt.date | None]:
        if not self._days:
            return None, None
        return self._days[0], self._days[-1]


# ---------------------------------------------------------------------------
# per-session snapshot construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """One (session, arm, direction) evaluation and its per-condition statuses."""

    session: dt.date
    signal_bar: dt.date
    arm: str
    direction: str
    fire: bool
    statuses: dict[str, str]
    blocking: dict[str, bool]

    def passed(self, condition: str) -> bool:
        return _resolved_statuses(self).get(condition) == "PASS"


@dataclass(frozen=True, slots=True)
class BasisResult:
    """Aggregate over every session for one basis."""

    basis: Basis
    sessions: tuple[dt.date, ...]
    outcomes: tuple[SessionOutcome, ...]

    @property
    def n_sessions(self) -> int:
        return len(self.sessions)

    def for_arm(self, arm: str, direction: str | None = None) -> tuple[SessionOutcome, ...]:
        return tuple(
            o
            for o in self.outcomes
            if o.arm == arm and (direction is None or o.direction == direction)
        )


def _bandwidth_percentile(stack: IndicatorStack, i: int, lookback_days: int) -> float | None:
    """``indicators.bandwidth_percentile`` over the trailing window ending at ``i``.

    Reproduces ``snapshot_builder.build_bollinger_state``'s slice exactly: it passes
    ``bb.bandwidth[newest - lookback_days : newest + 1]`` (a ``lookback + 1``-element
    slice) together with the current bandwidth as ``value``, and
    ``indicators.bandwidth_percentile`` then takes the last ``window`` elements of that
    slice — which excludes the current bar — and scores the current value against them.
    So the comparison set is indices ``i - lookback_days + 1 .. i - 1``, and the
    percentile is the share of those strictly below the current bandwidth.

    Returns ``None`` when the window is not yet full, which is the same PENDING the
    builder reports. The builder's own floor is "at least ``lookback + 1`` defined
    bandwidths", and Bollinger is defined from index 19, so the earliest usable index is
    ``19 + lookback``.
    """
    if i < BOLLINGER_PERIOD - 1 + lookback_days:
        return None
    tail: list[float] = []
    for j in range(i - lookback_days + 1, i):
        value = stack.bb_bandwidth[j]
        if value is None:
            return None
        tail.append(value)
    current = stack.bb_bandwidth[i]
    if current is None or not tail:
        return None
    below = sum(1 for value in tail if value < current)
    return 100.0 * below / len(tail)


def snapshot_for_index(
    stack: IndicatorStack,
    i: int,
    rules: Rulebook,
    *,
    iv_rank: float | None,
) -> IndicatorSnapshot:
    """Build the ``IndicatorSnapshot`` the evaluator reads, for signal bar ``i``.

    Field-for-field what ``snapshot_builder.build_snapshot`` produces for the prefix
    ``bars[:i+1]``, including the ``None``-propagation that makes a warmup hole a
    PENDING rather than a defaulted value. ``flow_gate`` is left at its default
    ``unknown``, which is the honest state: there is no flow series, and ``unknown`` is
    explicitly not a pass.
    """
    t3 = rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release
    t4 = rules.checklist.t4_volume
    bar_high, bar_low = stack.highs[i], stack.lows[i]

    bb_state: BollingerState | None = None
    pct = _bandwidth_percentile(stack, i, t3.lookback_days)
    (mid, upper, lower, bw, prior_bw, prior_mid) = (
        stack.bb_middle[i],
        stack.bb_upper[i],
        stack.bb_lower[i],
        stack.bb_bandwidth[i],
        stack.bb_bandwidth[i - 1] if i >= 1 else None,
        stack.bb_middle[i - 1] if i >= 1 else None,
    )
    if None not in (mid, upper, lower, bw, prior_bw, prior_mid) and pct is not None:
        bb_state = BollingerState(
            upper=upper,  # type: ignore[arg-type]
            middle=mid,  # type: ignore[arg-type]
            lower=lower,  # type: ignore[arg-type]
            low=bar_low,
            high=bar_high,
            bandwidth=bw,  # type: ignore[arg-type]
            bandwidth_percentile=pct,
            bandwidth_expanding=bw > prior_bw,  # type: ignore[operator]
            middle_rising=mid > prior_mid,  # type: ignore[operator]
            squeeze=pct < t3.bandwidth_below_percentile,
        )

    relvol: float | None = None
    if i >= t4.lookback_days:
        baseline = stack.volumes[i - t4.lookback_days : i]
        try:
            relvol = relative_volume(stack.volumes[i], baseline)
        except InsufficientData:
            relvol = None

    hist_prev = stack.macd_hist[i - 1] if i >= 1 else None
    cross_index = stack.macd_cross_index[i]
    cross_direction = stack.macd_cross_direction[i]
    cross_age = (
        (stack.times[i] - stack.times[cross_index]).total_seconds() / 3600.0
        if cross_index is not None
        else None
    )

    snap = IndicatorSnapshot(
        close=stack.closes[i],
        ema_fast=stack.ema_fast[i],
        ema_slow=stack.ema_slow[i],
        rsi=stack.rsi_values[i],
        macd_line=stack.macd_line[i],
        macd_signal=stack.macd_signal[i],
        macd_hist=stack.macd_hist[i],
        macd_hist_prev=hist_prev,
        macd_cross_direction=cross_direction,
        macd_cross_age_hours=cross_age,
        bollinger=bb_state,
        relvol=relvol,
        iv_rank=iv_rank,
        is_event_day=False,
        event_kinds=(),
        flow_gate=FlowGate(status="unknown"),
    )
    snap.validate()
    return snap





# ---------------------------------------------------------------------------
# event-leg haircut
# ---------------------------------------------------------------------------


def event_session_haircut(
    rules: Rulebook,
    *,
    fomc_days_per_year: int = 16,
    cpi_days_per_year: int = 12,
    sessions_per_year: int = 252,
) -> dict[str, float]:
    """T5's event leg, as a session-count haircut rather than a replay.

    The repo's calendar (``data/events/2026-Q4.yaml``) declares eight FOMC meetings a
    year at two veto days each and roughly twelve monthly CPI prints — both kinds are
    in the rulebook's ``veto_kinds``. This returns the fraction of a normal session year
    the hard veto would remove, plus the surviving fraction, so the joint rate can be
    reported with the event leg either included as a haircut or excluded as
    unmeasured. Neither number is presented as a replay of a historical calendar.
    """
    veto_days = fomc_days_per_year + cpi_days_per_year
    return {
        "veto_kinds": ",".join(rules.checklist.t5_options_chain.event_calendar.veto_kinds),
        "estimated_veto_sessions_per_year": float(veto_days),
        "sessions_per_year": float(sessions_per_year),
        "haircut_fraction": veto_days / sessions_per_year,
        "surviving_fraction": 1.0 - veto_days / sessions_per_year,
    }


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measurement:
    """Full measurement output for one rulebook + bar history."""

    results: dict[Basis, BasisResult]
    first_session: dt.date
    last_session: dt.date
    rulebook_version: str
    bar_count: int
    warmup_bars: int
    vix_coverage: tuple[dt.date | None, dt.date | None]
    iv_rank_proxy: bool
    event_haircut: dict[str, float]
    notes: tuple[str, ...] = field(default_factory=tuple)


def measure(
    bars: Sequence[BarRow],
    *,
    rules: Rulebook,
    vix: VixRankProxy | None = None,
    arms: Sequence[str] = ("B", "C"),
    directions: Sequence[str] = ("call", "put"),
    bases: Sequence[Basis] = BASES,
) -> Measurement:
    """Replay the frozen checklist over every session in ``bars``.

    For each basis and each (arm, direction) pair, evaluates the real
    ``checklist.evaluate`` on the snapshot the real builder would have produced, and
    records the per-condition status plus whether that condition blocks. Nothing about
    the pass/fail decision is reimplemented here.

    ``T6`` is reported exactly as the evaluator reports it — PENDING, because the frozen
    multiplier is uncalibrated and there is no flow series. That is the honest state and
    it means arm B's measured ``fire`` rate is zero by construction; see the module
    docstring. The T6 column in the results table is therefore a statement about the
    rulebook, not about the market.
    """
    if not bars:
        raise ValueError("no bars to measure")
    stack = build_stack(bars, rules)
    proxy = vix if vix is not None else VixRankProxy([])

    # The first session at which every blocking indicator is defined. Bars before this
    # would evaluate as PENDING on arithmetic grounds, not market grounds, and counting
    # them would understate every rate. Derived from the longest warmup in the stack
    # rather than hardcoded, so a threshold change moves it automatically.
    warmup = _warmup_bars(rules)
    first_signal = warmup
    if first_signal >= len(bars):
        raise ValueError(
            f"history is shorter than the {warmup}-bar warmup the frozen thresholds need; "
            "no session can be evaluated"
        )

    results: dict[Basis, BasisResult] = {}
    notes: list[str] = []
    for basis in bases:
        offset = 1 if basis == "plan" else 0
        outcomes: list[SessionOutcome] = []
        sessions: list[dt.date] = []
        for i in range(first_signal, len(bars)):
            signal_i = i - offset
            if signal_i < first_signal:
                continue
            session_day = bars[i].t.date()
            signal_day = bars[signal_i].t.date()
            sessions.append(session_day)
            snap = snapshot_for_index(stack, signal_i, rules, iv_rank=proxy.rank(session_day))
            for arm in arms:
                for direction in directions:
                    result = evaluate(snap, rules, direction, arm)  # type: ignore[arg-type]
                    outcomes.append(_outcome(session_day, signal_day, arm, direction, result))
        results[basis] = BasisResult(
            basis=basis, sessions=tuple(sessions), outcomes=tuple(outcomes)
        )
    if "T6" in rules.checklist.for_arm("B"):
        notes.append(
            "T6 is calibration_pending in the frozen rulebook and has no historical flow "
            "series to replay, so it is PENDING and blocking: arm B's measured fire rate "
            "is zero by construction, not by market behaviour."
        )
    notes.append(CROSS_GUARD_HOURS_NOTE)
    notes.append(
        "T5's iv_rank is a Cboe VIX percentile-rank proxy, not SPY option IV; no free "
        "source of point-in-time historical SPY option IV exists."
    )
    notes.append(
        "T5's event-day leg is not replayed — the repo's calendar covers 2026-Q4 only — "
        "and is reported as a separate session-count haircut."
    )
    return Measurement(
        results=results,
        first_session=bars[first_signal].t.date(),
        last_session=bars[-1].t.date(),
        rulebook_version=rules.strategy_version,
        bar_count=len(bars),
        warmup_bars=warmup,
        vix_coverage=proxy.coverage,
        iv_rank_proxy=True,
        event_haircut=event_session_haircut(rules),
        notes=tuple(notes),
    )


def _warmup_bars(rules: Rulebook) -> int:
    """Bars of history the frozen thresholds need before any gate can be non-PENDING.

    The binding minimum is the Bollinger bandwidth percentile: 20 bars for the bands
    plus the ``lookback_days`` trailing bandwidths, which the builder requires as
    ``lookback + 1`` defined bandwidths starting at index 19 — i.e. index
    ``19 + lookback``. EMA200 needs index 199, relvol needs 20, MACD needs 33. The max
    of those, plus one so the "prior bar" comparisons (histogram expansion, band
    expansion direction) have a bar to compare against.
    """
    squeeze = rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release
    t3 = 19 + squeeze.lookback_days + 1
    t1 = rules.checklist.t1_ema_alignment.ema_slow
    t2b = rules.checklist.t2b_macd.slow + rules.checklist.t2b_macd.signal - 1
    t4 = rules.checklist.t4_volume.lookback_days
    return max(t1, t3, t2b, t4) + 1


def _outcome(
    session: dt.date,
    signal_day: dt.date,
    arm: str,
    direction: str,
    result: ChecklistResult,
) -> SessionOutcome:
    return SessionOutcome(
        session=session,
        signal_bar=signal_day,
        arm=arm,
        direction=direction,
        fire=result.fire,
        statuses={cid: cond.status for cid, cond in result.conditions.items()},
        blocking={cid: cond.blocking for cid, cond in result.conditions.items()},
    )


# ---------------------------------------------------------------------------
# per-condition rates
# ---------------------------------------------------------------------------


def group_status(statuses: dict[str, str], group: Sequence[str] = T3_GROUP) -> str:
    """Resolve an OR group the way ``checklist.evaluate`` does.

    The evaluator folds the T3 group into a single veto carried by T3a when neither arm
    passes, and marks the passing sibling non-blocking. So the group's status for a
    session is: ``PASS`` if either member passed, else the first member's status. That
    is not cosmetic bookkeeping — reporting T3 as "always FAIL" because the id is not
    emitted would understate the real gate rate by the entire T3b contribution.
    """
    member_statuses = [statuses.get(cid, "PENDING") for cid in group]
    if any(s == "PASS" for s in member_statuses):
        return "PASS"
    # A group is PENDING (not FAIL) when no member could be evaluated at all; if some
    # member evaluated and vetoed, the group vetoed. Note a member the arm does not carry
    # is reported SKIPPED by the evaluator, so it does not read as a veto here.
    if any(s == "FAIL" for s in member_statuses):
        return "FAIL"
    if all(s == "SKIPPED" for s in member_statuses):
        return "SKIPPED"
    return "PENDING"


def _resolved_statuses(outcome: SessionOutcome) -> dict[str, str]:
    """Per-condition statuses plus the derived T3 group, keyed as ``CONDITIONS``."""
    statuses = dict(outcome.statuses)
    statuses["T3"] = group_status(outcome.statuses)
    return statuses


def effective_blocking(outcome: SessionOutcome, drop: Sequence[str] = ()) -> list[str]:
    """The rulebook-level blocking conditions for one session, T3 collapsed.

    Three things happen here, and the second and third are both load-bearing correctness
    fixes that the tests in ``tests/test_gate_baserate.py`` exist to catch.

    1. The T3 OR group becomes a single entry, because ``checklist.evaluate`` treats it
       as one veto. Splitting it would double-count the group in a marginal analysis.
    2. The group is only blocking when the arm actually carries at least one of its
       members. Without this an arm that does not run T3 at all would report T3 as
       blocking, because the group's status for an uncarried gate is PENDING rather
       than SKIPPED — and a spuriously-blocking gate poisons every downstream number
       that asks "did the other gates pass today?"
    3. The group is dropped as a unit when ``drop`` names it, so a leave-one-out that
       removes T3 removes *both* of its arms rather than leaving one behind to veto.
    """
    statuses = _resolved_statuses(outcome)
    dropped = set(drop)
    carries_t3 = any(outcome.blocking.get(member) for member in T3_GROUP)
    out: list[str] = []
    for cid in RULEBOOK_CONDITIONS:
        if cid in dropped:
            continue
        if cid == "T3":
            if not carries_t3:
                continue
            # The group's blocking flag lives on T3a when it vetoes and on neither arm
            # when a sibling passed. Either way the group gates this arm, so the group is
            # blocking unless it resolved to PASS.
            if statuses.get("T3") != "PASS":
                out.append(cid)
            continue
        if not outcome.blocking.get(cid) or statuses.get(cid) == "SKIPPED":
            continue
        out.append(cid)
    return out


def condition_rates(outcomes: Sequence[SessionOutcome], condition: str) -> dict[str, float]:
    """Pass / veto / pending / skipped counts and rates for one condition.

    A condition that is ``SKIPPED`` is reported on its own rather than folded into
    "passed" or "failed" — the checklist treats SKIPPED as neither, and a base-rate table
    that silently counted it would overstate a gate's coverage.
    """
    counts = {"PASS": 0, "FAIL": 0, "PENDING": 0, "SKIPPED": 0}
    total = len(outcomes)
    for outcome in outcomes:
        counts[_resolved_statuses(outcome).get(condition, "SKIPPED")] += 1
    denominator = total - counts["SKIPPED"]
    return {
        "sessions": float(total),
        "evaluated": float(denominator),
        "pass": float(counts["PASS"]),
        "fail": float(counts["FAIL"]),
        "pending": float(counts["PENDING"]),
        "skipped": float(counts["SKIPPED"]),
        "pass_rate": counts["PASS"] / denominator if denominator else 0.0,
        "veto_rate": counts["FAIL"] / denominator if denominator else 0.0,
    }


def joint_rate(outcomes: Sequence[SessionOutcome]) -> dict[str, float]:
    """Fraction of sessions where every blocking condition PASSes."""
    total = len(outcomes)
    fires = sum(1 for o in outcomes if o.fire)
    return {
        "sessions": float(total),
        "fires": float(fires),
        "joint_rate": fires / total if total else 0.0,
    }


def joint_rate_excluding(
    outcomes: Sequence[SessionOutcome], exclude: Sequence[str]
) -> dict[str, float]:
    """Joint rate with named conditions removed from the requirement.

    Needed for exactly one situation, and labelled as a counterfactual wherever it is
    reported: T6 is PENDING on every session while its multiplier is uncalibrated, so
    arm B's true joint rate is zero and its per-gate marginal analysis is degenerate.
    Dropping T6 from the AND does not predict what arm B will do — T6 is designed to
    pass ~10% of days — it answers the structural question the review is really asking,
    namely how the *technical* gates T1-T5 combine. Arm C's checklist is T1-T5 exactly,
    so arm C's measurement is the non-counterfactual version of the same number.
    """
    drop = set(exclude)
    total = len(outcomes)
    fires = 0
    for outcome in outcomes:
        statuses = _resolved_statuses(outcome)
        required = effective_blocking(outcome, drop)
        if all(statuses.get(cid) == "PASS" for cid in required):
            fires += 1
    return {
        "sessions": float(total),
        "fires": float(fires),
        "joint_rate": fires / total if total else 0.0,
        "excluded": ",".join(sorted(drop)),
    }


def marginal_contribution(
    outcomes: Sequence[SessionOutcome],
    conditions: Sequence[str],
    *,
    exclude: Sequence[str] = (),
) -> dict[str, dict[str, float]]:
    """Which gate is actually doing the filtering, measured three ways.

    "Binding" is ambiguous and a table that reports only one reading of it hides half
    the answer, so each condition gets three numbers:

    * ``sole_veto_sessions`` — sessions where this condition was the **only** blocker:
      every other blocking gate passed and this one vetoed. That is the count of days
      this gate is the sole reason nothing fired, and it is the sharpest single measure
      of bindingness.
    * ``pass_given_others_pass`` — of the sessions where every *other* gate passed, the
      share where this one passed too. Low means the gate vetoes independently of what
      the rest of the checklist thinks; near 1.0 means it is redundant.
    * ``fires_if_removed`` — absolute count of sessions that would have fired with this
      condition dropped from the AND. Unlike a ratio-based "lift" this stays readable
      when the joint rate is a handful of sessions out of thousands, which is exactly
      the regime this checklist operates in.

    Everything is re-derived from the recorded per-condition statuses, which is exactly
    the evaluator's own ``fire`` definition (no blocking condition is anything other
    than PASS), so no gate is re-decided here.

    Two guards keep the numbers honest. A condition the arm does not carry — T6 on arm
    C, reported SKIPPED — is excluded rather than counted as a veto, because "arm C is
    blocked by T6" is a category error. And a condition that is PENDING on every
    evaluated session is flagged ``always_pending``: it is blocking because it has no
    data, not because it evaluated and said no, and reporting that as a tight constraint
    would invert the finding. (T6 is the live example: uncalibrated multiplier, so it
    constrains arm B's entire fire rate to zero without filtering a single session.)
    """
    drop = set(exclude)
    out: dict[str, dict[str, float]] = {}
    total = len(outcomes)
    for condition in conditions:
        applicable = 0
        sole = 0
        without = 0
        pending_sessions = 0
        evaluated = 0
        for outcome in outcomes:
            statuses = _resolved_statuses(outcome)
            blocking = effective_blocking(outcome, drop)
            if condition not in blocking:
                # Not carried by this arm, or already dropped from the counterfactual.
                continue
            applicable += 1
            others = [cid for cid in blocking if cid != condition]
            if not all(statuses.get(cid) == "PASS" for cid in others):
                continue
            evaluated += 1
            status = statuses.get(condition)
            if status == "PASS":
                without += 1
            else:
                sole += 1
                if status == "PENDING":
                    pending_sessions += 1
        without_rate = without / evaluated if evaluated else 0.0
        out[condition] = {
            "applicable_sessions": float(applicable),
            "evaluated_on_others_pass": float(evaluated),
            "sole_veto_sessions": float(sole),
            "sole_veto_share_of_sessions": sole / total if total else 0.0,
            "pass_given_others_pass": without_rate,
            "fires_if_removed": float(without),
            "pending_sole_veto_sessions": float(pending_sessions),
            "always_pending": 1.0 if evaluated and pending_sessions == evaluated else 0.0,
            "not_applicable": 1.0 if applicable == 0 else 0.0,
        }
    return out


def expected_per_year(
    joint: float, *, sessions_per_year: int = 252, target_sessions: int | None = None,
    target_months: int | None = None,
) -> dict[str, float]:
    """Projected qualifying days per year and per planned window.

    A projection, labelled as one: the per-year figure is the measured joint rate times
    a 252-session year. The per-window figure uses the rulebook's own
    ``target_sessions`` when present, because that is the number the pre-registration
    committed to.
    """
    per_year = joint * sessions_per_year
    window_sessions = target_sessions or sessions_per_year / 12.0 * (target_months or 3)
    per_window = joint * window_sessions
    out = {
        "joint_rate": joint,
        "sessions_per_year": float(sessions_per_year),
        "expected_days_per_year": per_year,
        "expected_days_per_window": per_window,
        "target_sessions": float(target_sessions or window_sessions),
        "target_months": float(target_months or 0),
    }
    out["expected_trades_per_window"] = per_window
    # An "empty window" is the operator's actual question, so answer it directly rather
    # than making them invert the expectation themselves. Under the standard assumption
    # that qualifying sessions are independent across days, the chance a window of N
    # sessions contains none is (1 - p) ** N. Independence is an approximation here --
    # the gates are regime-dependent, so real runs will cluster -- and it is stated as
    # such. The figure is dominated by p being small, so the approximation is generous.
    out["p_zero_in_window"] = (
        (1.0 - joint) ** window_sessions if 0.0 <= joint < 1.0 else (1.0 if joint <= 0 else 0.0)
    )
    out["p_at_least_one_in_window"] = 1.0 - out["p_zero_in_window"]
    return out


# ---------------------------------------------------------------------------
# equivalence verification
# ---------------------------------------------------------------------------


def verify_against_builder(
    bars: Sequence[BarRow],
    *,
    rules: Rulebook,
    vix: VixRankProxy | None = None,
    samples: int = 12,
    stride: int = 97,
) -> dict[str, Any]:
    """Prove the O(n) fast path equals ``build_snapshot`` on truncated prefixes.

    The whole measurement rests on the claim in the module docstring that the aligned
    one-pass indicators are bit-identical to per-prefix construction. That claim is the
    kind of thing that rots silently: a future edit to the snapshot builder changes what
    production computes and this script keeps reporting last quarter's arithmetic.

    So this re-derives ``samples`` sessions the slow way — truncating the bar list and
    calling the real ``executor.snapshot_builder.build_snapshot`` with the rulebook's own
    thresholds — and compares every checklist condition's status against the fast path.
    Returns a report; ``mismatches`` empty means the fast path is faithful.
    """
    from executor.alpaca_client import BarSeries
    from executor.snapshot_builder import build_snapshot

    proxy = vix if vix is not None else VixRankProxy([])
    stack = build_stack(bars, rules)
    warmup = _warmup_bars(rules)
    n = len(bars)
    if n <= warmup:
        return {"samples": 0, "mismatches": [], "ok": False, "reason": "history too short"}

    squeeze = rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release
    indices = [warmup + stride * k for k in range(samples)]
    indices = [i for i in indices if i < n]
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for i in indices:
        prefix = bars[: i + 1]
        series = BarSeries(
            symbol=rules.strategy.symbol,
            timeframe="1Day",
            feed="analysis",
            bars=[_to_alpaca_bar(b) for b in prefix],
        )
        session_day = bars[i].t.date()
        rank = proxy.rank(session_day)
        slow = build_snapshot(
            daily=series,
            as_of=session_day,
            iv_rank=rank,
            ema_fast=rules.checklist.t1_ema_alignment.ema_fast,
            ema_slow=rules.checklist.t1_ema_alignment.ema_slow,
            rsi_period=rules.checklist.t2_rsi.period,
            macd_fast=rules.checklist.t2b_macd.fast,
            macd_slow=rules.checklist.t2b_macd.slow,
            macd_signal=rules.checklist.t2b_macd.signal,
            bb_lookback_days=squeeze.lookback_days,
            bb_squeeze_percentile=squeeze.bandwidth_below_percentile,
            relvol_lookback_days=rules.checklist.t4_volume.lookback_days,
            cross_guard_hours=rules.checklist.t2b_macd.fresh_cross_guard.max_age_hours,
        )
        fast_snap = snapshot_for_index(stack, i, rules, iv_rank=rank)
        for arm in ("B", "C"):
            for direction in ("call", "put"):
                a = evaluate(slow.snapshot, rules, direction, arm)  # type: ignore[arg-type]
                b = evaluate(fast_snap, rules, direction, arm)  # type: ignore[arg-type]
                checked += 1
                if a.fire != b.fire:
                    mismatches.append(
                        {"index": i, "session": session_day.isoformat(), "arm": arm,
                         "direction": direction, "field": "fire",
                         "slow": a.fire, "fast": b.fire}
                    )
                for cid in a.conditions:
                    sa = a.conditions[cid].status
                    sb = b.conditions.get(cid).status if b.conditions.get(cid) else None
                    if sa != sb:
                        mismatches.append(
                            {"index": i, "session": session_day.isoformat(), "arm": arm,
                             "direction": direction, "field": cid, "slow": sa, "fast": sb}
                        )
    return {
        "samples": len(indices),
        "evaluations_compared": checked,
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
        "ok": not mismatches,
        "stride": stride,
    }


def _to_alpaca_bar(row: BarRow):
    from executor.alpaca_client import Bar

    return Bar(t=row.t, o=row.o, h=row.h, l=row.l, c=row.c, v=row.v)


__all__ = [
    "BASES",
    "BOLLINGER_NUM_STD",
    "BOLLINGER_PERIOD",
    "CONDITIONS",
    "CROSS_GUARD_HOURS_NOTE",
    "RULEBOOK_CONDITIONS",
    "T3_GROUP",
    "BarRow",
    "Basis",
    "BasisResult",
    "IndicatorStack",
    "Measurement",
    "SessionOutcome",
    "VixRankProxy",
    "VixRow",
    "build_stack",
    "condition_rates",
    "event_session_haircut",
    "expected_per_year",
    "effective_blocking",
    "group_status",
    "joint_rate",
    "joint_rate_excluding",
    "marginal_contribution",
    "measure",
    "snapshot_for_index",
    "verify_against_builder",
]