"""Snapshot builder — raw market data in, ``IndicatorSnapshot`` out.

This is the seam between the I/O layer (``alpaca_client``) and the pure evaluation
layer (``checklist``). It does arithmetic and nothing else: no network, no journal
writes, no order anything. Feed it bars, a chain and a calendar; it hands
``checklist.evaluate`` exactly the fields that evaluator reads.

The whole point of this module is that **warmup is reported honestly**. EMA200 needs
200 closes, MACD needs 34, RSI needs 15, the bandwidth percentile needs 61 bandwidth
values, the relvol baseline needs 20 volumes. A 150-day series is not an error and it
is not a partially-correct snapshot — it is a snapshot with PENDING-shaped holes, and
the checklist turns each hole into a PENDING that blocks. So ``None`` propagates; no
default is ever invented. (Indicators raise ``InsufficientData`` rather than padding,
so the builder catches it per-indicator and records the gap.)

Feed consistency — the T4 correctness requirement
--------------------------------------------------
The research note's sharpest warning: on the free tier, realtime equity volume is
**IEX only** while historical can be SIP. "Relative volume ≥ 1.5x 20-day average"
is meaningless if the numerator is IEX volume and the baseline is SIP volume — IEX is
a fraction of consolidated volume, so mixing feeds makes T4 fail or pass on the ratio
between two different universes, and the checklist would be lying quietly.

So: the builder refuses to compute relvol across feeds. ``BarSeries.feed`` on the
signal series and on the baseline must match or relvol is ``None`` (T4 PENDING,
blocks) and the reason is recorded in the return object. The builder never silently
re-queries a different feed to "fix" it.

MACD cross age
--------------
``macd_cross_direction`` / ``macd_cross_age_hours`` are derived from the aligned
macd/signal series: find the most recent bar where ``macd - signal`` changes sign,
take its direction from the sign of the newest side, and its age in hours from the
timestamp of that bar to the timestamp of the newest bar. The whipsaw guard in T2b
only fires inside 24h, so "hours" is the honest unit — "bars" would depend on whether
we happened to be on daily or 5-minute data, and a 3-bar-old cross would read as 72h on
daily and 15 minutes on 5m.

# INTERPRETATION: age is measured from the crossover bar's timestamp to the newest
bar's timestamp, both from the data (never ``now``). An EOD snapshot therefore reports
age as of the close, not as of the wall clock — otherwise a snapshot recomputed at
16:15 would show a 68-hour-old cross that was really 44 hours old at the close, and
the journal's numbers would not be reproducible from the same bars.

Python 3.12+, stdlib + ``executor.indicators``/``executor.checklist``.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from data.event_calendar import EventCalendar
from executor.alpaca_client import BarSeries, OptionChain
from executor.checklist import (
    BollingerState,
    FlowGate,
    IndicatorSnapshot,
    snapshot_indicators,
)
from executor.indicators import (
    InsufficientData,
    bandwidth_percentile,
    bollinger,
    ema,
    latest,
    latest_pair,
    macd,
    relative_volume,
    rsi,
)
from executor.position_manager import to_et

Direction = Literal["call", "put"]

#: Bars needed before each indicator is defined. Mirrors the maths in
#: ``executor.indicators`` rather than duplicating it — these are documentation of the
#: honest minimums, and the code below still catches ``InsufficientData`` rather than
#: trusting them.
NEEDS_EMA_SLOW = 200
NEEDS_MACD = 26 + 9 - 1
NEEDS_RSI = 14 + 1


class SnapshotInputError(ValueError):
    """Raised when inputs are structurally unusable (empty series, no close, ...)."""


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    """The snapshot plus the provenance of how it was built.

    ``pending_reasons`` is the honest audit trail: every field the checklist will read
    as PENDING, with the arithmetic reason. The journal writes this next to the
    snapshot so a NO-SHOT entry explains *why* the system didn't know something,
    rather than leaving a reader to guess from a null.

    ``relvol_feed`` records which feed the volume series came from — the single field
    that makes the T4 same-feed rule checkable after the fact.
    """

    snapshot: IndicatorSnapshot
    pending_reasons: tuple[str, ...] = ()
    bars_used: int = 0
    relvol_feed: str | None = None
    feed_notes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        return bool(self.pending_reasons)

    def to_dict(self, *, direction: str = "call", arm: str = "B") -> dict:
        """Journal-ready provenance. The indicator dict itself comes from
        ``checklist.snapshot_indicators`` (that module owns the snapshot shape), so
        this stays about *how* the snapshot was built, not what it contains."""
        return {
            "indicators": snapshot_indicators(self.snapshot, direction, arm),
            "bars_used": self.bars_used,
            "relvol_feed": self.relvol_feed,
            "pending_reasons": list(self.pending_reasons),
            "feed_notes": list(self.feed_notes),
            "notes": list(self.notes),
        }


def _closes(series: Sequence[float] | BarSeries) -> list[float]:
    if isinstance(series, BarSeries):
        return list(series.closes)
    return list(series)


def _volumes(series: Sequence[float] | BarSeries) -> list[float]:
    if isinstance(series, BarSeries):
        return list(series.volumes)
    return list(series)


# ---------------------------------------------------------------------------
# MACD cross derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MacdCross:
    """Most recent MACD line/signal crossover found in a series."""

    direction: Direction
    index: int
    bar_time: dt.datetime | None
    age_hours: float | None


def find_macd_cross(
    closes: Sequence[float],
    bar_times: Sequence[dt.datetime],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MacdCross | None:
    """Most recent macd/signal sign change, newest-first scan.

    A "bullish" cross (macd crossing **above** signal) is ``direction="call"`` and a
    bearish cross is ``"put"`` — the direction names the *cross*, which is what T2b's
    guard compares against ("no entries against a fresh MACD cross"). T2b inverts it
    against the trade direction itself, so this module never has to know the trade.

    Returns ``None`` when there is no cross in the defined history (a monotone
    series, or not enough bars for MACD at all). ``None`` is honest: the guard only
    applies to a cross that exists, and T2b checks ``macd_cross_direction is not None``.
    """
    if len(closes) != len(bar_times):
        raise SnapshotInputError(
            f"closes/times length mismatch: {len(closes)} vs {len(bar_times)}"
        )
    try:
        result = macd(closes, fast, slow, signal)
    except InsufficientData:
        return None

    diff = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(result.macd_line, result.signal_line, strict=True)
    ]

    last_index: int | None = None
    last_direction: Direction | None = None
    for i in range(len(diff) - 1, 0, -1):
        current, prior = diff[i], diff[i - 1]
        if current is None or prior is None or current == prior or current == 0 or prior == 0:
            continue
        last_index = i
        last_direction = "call" if current > prior else "put"
        break

    if last_index is None or last_direction is None:
        return None

    bar_time = bar_times[last_index]
    newest_time = bar_times[-1]
    age_hours = (newest_time - bar_time).total_seconds() / 3600.0
    return MacdCross(
        direction=last_direction,
        index=last_index,
        bar_time=bar_time,
        age_hours=age_hours,
    )


# ---------------------------------------------------------------------------
# bollinger state
# ---------------------------------------------------------------------------


def build_bollinger_state(
    daily: BarSeries,
    *,
    lookback_days: int = 60,
    squeeze_percentile: float = 20.0,
) -> tuple[BollingerState | None, str | None]:
    """Bollinger(20, 2) state for the newest daily bar.

    Returns ``(state, pending_reason)``. ``None`` state with a reason means the
    snapshot carries a hole T3 will read as PENDING.

    ``squeeze`` is evaluated here (bandwidth percentile < the rulebook's threshold
    over the rulebook's 60-day window) rather than in the checklist, because the
    percentile needs the bandwidth history that only this layer has. The threshold
    itself is passed in from the rulebook — no literal threshold lives here.

    # INTERPRETATION: the bandwidth percentile is computed over the ``lookback_days``
    *preceding* bandwidths, excluding the current bar (matching
    ``indicators.bandwidth_percentile``'s own exclusion of the newest value). A
    squeeze reading that includes itself would always be at or below its own median.
    """
    closes = _closes(daily)
    if len(closes) < 20:
        return None, f"bollinger needs 20 closes, got {len(closes)}"
    bb = bollinger(closes, 20, 2.0)

    newest = len(closes) - 1
    current_bw = bb.bandwidth[newest]
    prior_bw = bb.bandwidth[newest - 1]
    current_mid = bb.middle[newest]
    prior_mid = bb.middle[newest - 1]
    if None in (current_bw, prior_bw, current_mid, prior_mid, bb.upper[newest], bb.lower[newest]):
        return None, "bollinger undefined on the newest bar (unexpected)"

    # Percentile needs `lookback_days` preceding bandwidths plus the current one.
    available = len(closes) - 19  # number of defined bandwidths
    if available < lookback_days + 1:
        return None, (
            f"bandwidth percentile needs {lookback_days + 1} defined bandwidths "
            f"(1 current + {lookback_days} trailing), got {available}"
        )
    try:
        pct = bandwidth_percentile(
            bb.bandwidth[newest - lookback_days : newest + 1],  # type: ignore[arg-type]
            window=lookback_days,
            value=current_bw,
        )
    except InsufficientData as exc:
        return None, f"bandwidth percentile unavailable: {exc}"

    last = daily.bars[newest]
    return (
        BollingerState(
            upper=bb.upper[newest],  # type: ignore[arg-type]
            middle=current_mid,  # type: ignore[arg-type]
            lower=bb.lower[newest],  # type: ignore[arg-type]
            low=last.l,
            high=last.h,
            bandwidth=current_bw,  # type: ignore[arg-type]
            bandwidth_percentile=pct,
            bandwidth_expanding=current_bw > prior_bw,  # type: ignore[operator]
            middle_rising=current_mid > prior_mid,  # type: ignore[operator]
            squeeze=pct < squeeze_percentile,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# relvol
# ---------------------------------------------------------------------------


def build_relvol(
    daily: BarSeries,
    *,
    lookback_days: int = 20,
    baseline_series: BarSeries | None = None,
) -> tuple[float | None, str | None, str]:
    """T4 relative volume with the same-feed rule enforced.

    Returns ``(relvol, pending_reason, feed)``. ``None`` relvol means T4 PENDING.

    ``baseline_series`` lets a caller supply the 20-day baseline from a different bar
    series (e.g. the same instrument pulled with a different limit). When supplied,
    its ``feed`` must match the signal series or we refuse — same rule as below.

    The current bar is **excluded** from its own baseline: "≥ 1.5x 20-day average on
    the signal candle" means the signal candle divided by the average of the 20 days
    before it, not including itself.
    """
    volumes = _volumes(daily)
    if len(volumes) < lookback_days + 1:
        return (
            None,
            f"relative volume needs {lookback_days + 1} bars "
            f"(signal + {lookback_days}d baseline), got {len(volumes)}",
            daily.feed,
        )

    signal_feed = daily.feed
    if baseline_series is not None and baseline_series.feed != signal_feed:
        return (
            None,
            f"relvol baseline feed mismatch: signal series is feed={signal_feed}, "
            f"baseline is feed={baseline_series.feed}. T4 requires one feed for both "
            f"or the ratio compares two different universes (IEX vs SIP) and the "
            f"checklist silently lies (research note §1).",
            signal_feed,
        )

    current = volumes[-1]
    baseline = volumes[-1 - lookback_days : -1]
    try:
        value = relative_volume(current, baseline)
    except InsufficientData as exc:
        return None, f"relative volume unavailable: {exc}", signal_feed
    return value, None, signal_feed


# ---------------------------------------------------------------------------
# intraday run rate (T4's second, frozen branch)
# ---------------------------------------------------------------------------

#: US equity regular session, ET wall-clock. Bars outside it never enter the run rate:
#: the daily bar it is compared against is a regular-session bar, so a pre-market
#: block glued onto either side would compare two different universes. Same rule and
#: same reasoning as ``ui.aggregate``'s pre/post exclusion.
SESSION_OPEN_ET = dt.time(9, 30)
SESSION_CLOSE_ET = dt.time(16, 0)

#: Timeframes that cannot express "volume so far today". A daily bar carries one
#: volume number for the whole session stamped at the open, so every session would
#: report the same time-of-day and the ratio would collapse back into ``build_relvol``.
CALENDAR_TIMEFRAMES: frozenset[str] = frozenset(
    {"1Day", "1Week", "1Month"}
)


def build_intraday_run_rate(
    intraday: BarSeries | None,
    *,
    lookback_days: int = 20,
    session: dt.date | None = None,
) -> tuple[float | None, str | None]:
    """T4's intraday run-rate branch: today's volume so far vs the same clock yesterday.

    FIX 2026-10-03 (outsider review 4.3). ``checklist.t4_volume.intraday_run_rate_min``
    is a frozen rulebook value that no code read: the field existed in the loader, in a
    test, and inside a *log string* in ``checklist._t4``, while T4 evaluated
    ``relvol >= relative_volume_min`` and nothing else.

    Definition (the rulebook's own comment: *"or intraday run-rate >= 1.5x for intraday
    triggers"*): with ``t`` the clock time of the newest intraday bar,

        run_rate = today's cumulative volume through t
                   / mean over the prior ``lookback_days`` sessions of that session's
                     cumulative volume through the SAME time of day

    Same-time-of-day, not full-day: the baseline must be the volume a normal session
    had accumulated by the time we are being asked, or the number measures elapsed time
    rather than urgency (at 10:00 a normal day is ``~15%`` done, and dividing by a
    completed-day average makes the ratio ~15x optimistic in one direction and
    structurally unsatisfiable in the other).

    INTERPRETATION: the clock is the newest bar's, not ``now()``. A run rate computed
    against the wall clock keeps growing between bars while no new volume arrives, so the
    same snapshot read twice would produce two different numbers — and the number would
    partly measure how long the loop has been waiting rather than how fast volume came in.

    INTERPRETATION: every insufficient input yields ``None`` rather than a number computed
    on what happens to be available. A shrunk baseline, a dropped session, and a zero
    window all still produce a ratio; none of them produce the one the frozen rule names.

    Returns ``(run_rate, pending_reason)``. ``None`` run rate means T4 falls back to
    its relative-volume branch — see ``checklist._t4``, which scores the two branches
    as the OR the brief wrote ("...or intraday run-rate >= 1.5x for intraday triggers").

    Strictness choices, all toward "report nothing rather than report a wrong number":

    - The full ``lookback_days`` prior sessions must be present. A partial baseline is
      a baseline measured over a different window than the frozen rule names.
    - A prior session with no bar at or before ``t`` is not silently dropped; it makes
      the value unavailable. Its cumulative-through-``t`` is not measurable, and
      averaging over the sessions that happen to have data would quietly shrink the
      window.
    - A zero baseline mean returns ``None``, not ``inf``: an infinite ratio would score
      as the strongest possible volume confirmation off a window that measured nothing.
    - ``session`` overrides which date counts as "today". It exists so a caller can
      score a fixed session in tests; production leaves it ``None`` and the newest
      session in the series is today by construction.
    """
    if intraday is None or not intraday.bars:
        return None, "no intraday series supplied — T4 scored on the relative-volume branch only"
    if intraday.timeframe in CALENDAR_TIMEFRAMES:
        return (
            None,
            f"intraday series is timeframe={intraday.timeframe!r}, which cannot express "
            "volume-so-far-today; T4 scored on the relative-volume branch only",
        )

    # (ET date -> [(ET time, volume)], oldest session last)
    by_session: dict[dt.date, list[tuple[dt.time, float]]] = {}
    for bar in intraday.bars:
        moment = to_et(bar.t)
        clock = moment.time()
        if clock < SESSION_OPEN_ET or clock > SESSION_CLOSE_ET:
            continue
        by_session.setdefault(moment.date(), []).append((clock, bar.v))
    if not by_session:
        return (
            None,
            "intraday series contains no regular-session bars (09:30-16:00 ET); "
            "T4 scored on the relative-volume branch only",
        )

    dates = sorted(by_session)
    current_date = session if session is not None else dates[-1]
    if current_date not in by_session:
        return (
            None,
            f"intraday series has no bars for session {current_date.isoformat()}; "
            "T4 scored on the relative-volume branch only",
        )
    # The clock is the newest bar of TODAY — not now(). A run rate computed against the
    # wall clock would keep growing between bars while no new volume arrives.
    cutoff = max(clock for clock, _ in by_session[current_date])
    current_volume = math.fsum(v for _, v in by_session[current_date])

    prior = [d for d in dates if d < current_date][-lookback_days:]
    if len(prior) < lookback_days:
        return (
            None,
            f"intraday run-rate needs {lookback_days} prior sessions with intraday bars, "
            f"got {len(prior)} (latest session {current_date.isoformat()})",
        )
    baselines: list[float] = []
    for date in prior:
        session_volume = math.fsum(v for clock, v in by_session[date] if clock <= cutoff)
        if not any(clock <= cutoff for clock, _ in by_session[date]):
            return (
                None,
                f"session {date.isoformat()} has no intraday bar at or before the "
                f"current clock {cutoff.strftime('%H:%M')} ET, so its cumulative volume at "
                f"that time is not measurable; T4 scored on the relative-volume branch only",
            )
        baselines.append(session_volume)
    mean_baseline = math.fsum(baselines) / len(baselines)
    if mean_baseline <= 0:
        return (
            None,
            f"intraday run-rate baseline is {mean_baseline:.3f} shares over "
            f"{len(baselines)} sessions through {cutoff.strftime('%H:%M')} ET — a "
            "zero baseline measures nothing, so T4 scored on the relative-volume "
            "branch only",
        )
    return current_volume / mean_baseline, None


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def build_snapshot(
    *,
    daily: BarSeries,
    chain: OptionChain | None = None,
    calendar: EventCalendar | None = None,
    veto_kinds: Sequence[str] | None = None,
    as_of: dt.date | None = None,
    spot: float | None = None,
    iv_rank: float | None = None,
    flow_gate: FlowGate | None = None,
    baseline_series: BarSeries | None = None,
    ema_fast: int = 50,
    ema_slow: int = 200,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_lookback_days: int = 60,
    bb_squeeze_percentile: float = 20.0,
    relvol_lookback_days: int = 20,
    intraday: BarSeries | None = None,
    intraday_lookback_days: int = 20,
    cross_guard_hours: int = 24,
) -> SnapshotResult:
    """Build the ``IndicatorSnapshot`` the checklist evaluates.

    Every threshold parameter defaults to the frozen rulebook value. The intended
    call site passes ``rules.checklist`` values explicitly — defaults exist so a test
    can build a snapshot without loading a rulebook, not so production can forget to
    (a silent default change would be a mid-window rule change, which the brief
    forbids outside monthly review).

    ``iv_rank`` is passed in rather than computed here: the rank needs the persisted
    IV store (which has its own connection and warmup rules), and pretending a
    snapshot builder can know the answer would hide that dependency. Same for
    ``spot``/``chain``, which exist so the provenance of a T5 tenor selection can be
    recorded by the caller.

    The newest daily bar is the signal bar. There is no daily/intraday merge here: the
    two live in separate ``BarSeries`` inputs and never mix in one indicator. The
    intraday run-rate (``build_intraday_run_rate``) is its own field, scored against
    its own same-time-of-day baseline — see its docstring for why it cannot be derived
    from the daily series.
    """
    if not daily.bars:
        raise SnapshotInputError("daily series has no bars — cannot build a snapshot")

    pending: list[str] = []
    closes = _closes(daily)
    times = [b.t for b in daily.bars]
    close = daily.bars[-1].c

    # -- EMAs ----------------------------------------------------------------
    ema_fast_value: float | None = None
    ema_slow_value: float | None = None
    if len(closes) >= ema_fast:
        ema_fast_value = latest(ema(closes, ema_fast))
    else:
        pending.append(f"EMA{ema_fast} needs {ema_fast} closes, got {len(closes)}")
    if len(closes) >= ema_slow:
        ema_slow_value = latest(ema(closes, ema_slow))
    else:
        pending.append(
            f"EMA{ema_slow} needs {ema_slow} closes, got {len(closes)} — "
            f"T1 PENDING until the 200-day warmup completes"
        )

    # -- RSI -----------------------------------------------------------------
    rsi_value: float | None = None
    needs_rsi = rsi_period + 1
    if len(closes) >= needs_rsi:
        rsi_value = latest(rsi(closes, rsi_period))
    else:
        pending.append(f"RSI({rsi_period}) needs {needs_rsi} closes, got {len(closes)}")

    # -- MACD ----------------------------------------------------------------
    macd_line: float | None = None
    macd_signal_value: float | None = None
    macd_hist: float | None = None
    macd_hist_prev: float | None = None
    if len(closes) >= NEEDS_MACD:
        m = macd(closes, macd_fast, macd_slow, macd_signal)
        macd_line = latest(m.macd_line)
        macd_signal_value = latest(m.signal_line)
        macd_hist = latest(m.histogram)
        try:
            _, macd_hist_prev = latest_pair(m.histogram)
        except InsufficientData:
            pending.append("MACD histogram has only one defined value; no prior bar to compare")
    else:
        pending.append(
            f"MACD({macd_fast},{macd_slow},{macd_signal}) needs {NEEDS_MACD} closes, "
            f"got {len(closes)}"
        )

    # -- MACD cross ----------------------------------------------------------
    cross = find_macd_cross(closes, times, fast=macd_fast, slow=macd_slow, signal=macd_signal)
    cross_direction = cross.direction if cross else None
    cross_age = cross.age_hours if cross else None
    if cross and cross_age is not None and cross_age < cross_guard_hours:
        pending.append(
            f"NOTE: most recent MACD cross is {cross_age:.2f}h old "
            f"(< {cross_guard_hours}h whipsaw guard) — T2b will veto entries against it"
        )
    if cross is None:
        pending.append("no MACD crossover in the defined history — fresh-cross guard inactive")

    # -- Bollinger -----------------------------------------------------------
    bb_state, bb_reason = build_bollinger_state(
        daily, lookback_days=bb_lookback_days, squeeze_percentile=bb_squeeze_percentile
    )
    if bb_reason:
        pending.append(bb_reason)

    # -- relvol --------------------------------------------------------------
    relvol_value, relvol_reason, relvol_feed = build_relvol(
        daily, lookback_days=relvol_lookback_days, baseline_series=baseline_series
    )
    if relvol_reason:
        pending.append(relvol_reason)

    # -- intraday run rate (T4's second branch) ------------------------------
    # FIX 2026-10-03 (review 4.3): the rulebook's `intraday_run_rate_min` had no reader.
    # Its unavailability is a NOTE, not a pending reason: the brief scores T4 as an OR
    # ("relative volume ... or intraday run-rate"), so a missing run rate leaves T4
    # decidable on the relative-volume branch and must not block an entry the frozen
    # rules allow. The note is kept because a journal reader still needs to know which
    # branch actually ran.
    run_rate_value, run_rate_reason = build_intraday_run_rate(
        intraday, lookback_days=intraday_lookback_days, session=as_of
    )

    # -- IV rank / chain provenance -----------------------------------------
    notes: list[str] = []
    if run_rate_reason is not None:
        notes.append(f"intraday run-rate unavailable: {run_rate_reason}")
    elif run_rate_value is not None:
        notes.append(
            f"intraday run-rate computed on {intraday.timeframe}/{intraday.feed} "
            f"(feed note only — the run-rate baseline comes from the same series, so "
            f"there is no cross-feed ratio to make)"
        )
    if chain is not None:
        notes.append(
            f"options chain: {len(chain)} contracts over {len(chain.expiries())} expiry/expiries, "
            f"feed={chain.feed}"
        )
        if spot is None:
            notes.append("no spot supplied — chain present but no ATM tenor selection recorded")
    elif iv_rank is None:
        pending.append("no options chain supplied and no iv_rank given — T5 PENDING")

    # -- event calendar ------------------------------------------------------
    is_event_day = False
    event_kinds: tuple[str, ...] = ()
    if calendar is not None:
        day = as_of or daily.bars[-1].t.date()
        kinds = list(veto_kinds) if veto_kinds else None
        vetoes = calendar.veto_events_on(day, veto_kinds=kinds)
        is_event_day = bool(vetoes)
        event_kinds = tuple(sorted({v.kind for v in vetoes}))
        if is_event_day:
            reasons = "; ".join(calendar.veto_reasons(day, veto_kinds=kinds))
            notes.append(f"event-day hard veto on {day.isoformat()}: {reasons}")

    snapshot = IndicatorSnapshot(
        close=close,
        ema_fast=ema_fast_value,
        ema_slow=ema_slow_value,
        rsi=rsi_value,
        macd_line=macd_line,
        macd_signal=macd_signal_value,
        macd_hist=macd_hist,
        macd_hist_prev=macd_hist_prev,
        macd_cross_direction=cross_direction,
        macd_cross_age_hours=cross_age,
        bollinger=bb_state,
        relvol=relvol_value,
        intraday_run_rate=run_rate_value,
        iv_rank=iv_rank,
        is_event_day=is_event_day,
        event_kinds=event_kinds,
        flow_gate=flow_gate if flow_gate is not None else FlowGate(status="unknown"),
    )
    snapshot.validate()

    return SnapshotResult(
        snapshot=snapshot,
        pending_reasons=tuple(pending),
        bars_used=len(daily.bars),
        relvol_feed=relvol_feed,
        feed_notes=(f"relvol computed on feed={relvol_feed}",) if relvol_value is not None else (),
        notes=tuple(notes),
    )


def build_from_bars(
    daily_bars_json: dict,
    *,
    symbol: str = "SPY",
    **kwargs,
) -> SnapshotResult:
    """Convenience wrapper for tests/ad-hoc use: raw v2 bars payload -> snapshot.

    Kept because it means a canned JSON fixture (exactly the shape
    ``alpaca_client`` parses) can drive the whole builder in a test without the test
    having to know how bars are constructed.

    Parsing goes through ``alpaca_client._bar_series_from_payload`` on purpose. This
    used to hand-roll ``bars[symbol]`` — the *multi-symbol* envelope — which silently
    produced an empty series for every documented single-symbol payload.
    """
    from executor.alpaca_client import _bar_series_from_payload

    series = _bar_series_from_payload(
        symbol, "1Day", str(daily_bars_json.get("feed", "sip")), daily_bars_json
    )
    return build_snapshot(daily=series, **kwargs)


__all__ = [
    "MacdCross",
    "NEEDS_EMA_SLOW",
    "NEEDS_MACD",
    "NEEDS_RSI",
    "SnapshotInputError",
    "SnapshotResult",
    "build_bollinger_state",
    "build_from_bars",
    "build_relvol",
    "build_snapshot",
    "find_macd_cross",
]