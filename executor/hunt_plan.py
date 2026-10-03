"""Hunt plan — the pre-market brain (brief: "Signal cadence" step 1).

At 08:30-09:15 ET the executor recomputes the EOD indicator stack, checks the event
calendar, sets today's watch levels and trigger prices, and writes the result to the
journal. That output is the hunt plan: for every direction (call/put) x every active
armed arm (B/C), the checklist as it stands on yesterday's close, what would have to
change for it to fire, and the price levels worth watching.

Scope discipline (this is Phase 4a): the plan **plans**. It places no order, watches no
trigger, takes no intraday read. The intraday loop (brief step 2) re-verifies the
checklist against live data at trigger time — stale checklist entries are forbidden by
the brief — so everything here is explicitly provisional and labelled as such in the
journal row.

Trigger prices: honest, or null with a reason
---------------------------------------------
A trigger price is "the price level that would flip the marginal failing condition".
Most conditions are **not** flippable by a price level at all: RSI is a 14-close
statistic, relative volume is a volume ratio, IV rank is a percentile of an
imulated-vol distribution, MACD's line/signal relationship is two EMAs. Inventing a
number for those would be the exact failure mode this project exists to measure
around, so ``TriggerPrice.level`` is ``None`` with a stated reason for every condition
that has no honest level. Two conditions do have one and both are computed, not guessed:

* **T1** — when the EMA stack is aligned but the close sits on the wrong side of the
  fast EMA, the fast EMA *is* the trigger. When the EMAs themselves are misaligned no
  price fixes it today; that is a crossover, a time event, and it says so.
* **T3b** — when the signal bar already tagged the band but closed outside it, the
  band edge is the trigger (the close must finish back inside).

# INTERPRETATION: a non-blocking condition can still get a trigger, for T3 only.
The rulebook's ``t3_bollinger.satisfied_if_any_of`` makes T3 an OR group, so when one
T3 arm PASSes the sibling is reported FAIL (or whatever it is) but ``blocking=False``:
it cannot veto. A blind "triggers = every blocking non-PASS condition" filter would
then drop the band-edge level from the plan entirely — even though that level is the
one number an intraday loop would actually watch, and even though reaching it flips
T3b to PASS and satisfies the T3 group through the sibling. So the non-blocking T3
arm keeps its trigger, with its reason prefixed ``NON-BLOCKING`` so the journal never
reads as if it were vetoing anything. PENDING is still excluded regardless of blocking
(an unevaluated condition has no level and no "already satisfied" story to tell), and
this exception is deliberately scoped to the T3 group rather than made general: every
other non-blocking condition in the checklist is SKIPPED-for-this-arm (T6 on arm C),
which is a scope fact, not a near-miss.

Journal vocabulary: PROPOSAL vs NO_TRADE
----------------------------------------
``journal.store.DecisionKind`` offers TRADE / NO_TRADE / ROLL / STOP / PROPOSAL / VETO.
A hunt-plan row is not a trade (no order exists) and not a roll or stop. So:

* checklist **fires** -> ``PROPOSAL``: the plan proposes a *contingent* entry, subject
  to intraday re-verification. No order is implied by this row.
* checklist **does not fire** -> ``NO_TRADE``: at plan time, no trade will be taken
  today. That is a decision, with the vetoing conditions recorded.

The brief reserves ``PROPOSAL`` for catalyst-clause, operator-approved, off-checklist
proposals on arm ``EXCEPTION``. This module only ever writes PROPOSAL on arms B/C with
``checklist_state.decision_kind`` and ``reasoning`` stating the contingency, so the two
uses stay distinguishable in the ledger. ``VETO`` is not used: an event-day veto is a
condition failure inside T5, already visible as T5 FAIL.

# INTERPRETATION: plan-time conviction is a deterministic function of how far the
# checklist is from firing — 10 when it fires, otherwise ``10 - 3*failed - 1*pending``
clipped to 1..10. It is a *ranking* of near-misses (which sighting was closest), not a
calibrated probability; the brief asks for a 1-10 conviction on every decision and
inventing a distribution for it would be dishonest. The formula is recorded here so a
later reader can recompute it from the stored checklist_state.

# INTERPRETATION: "spot" for chain tenor selection defaults to the newest daily close,
because the plan is built pre-market and there is no pre-market option price to use.
With an intraday provider wired (Phase 4b) the caller passes the real spot.

Python 3.12+, stdlib + the already-built data/checklist/snapshot/journal layers.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from config.loader import Rulebook
from data.event_calendar import EventCalendar
from executor.alpaca_client import Bar, BarSeries, OptionChain, OptionContract
from executor.checklist import ChecklistResult, evaluate
from executor.iv_rank import (
    MONEYNESS_BUCKET_PCT,
    STRIKE_BUCKET_SIZE,
    IvRankStore,
    moneyness_bucket,
    moneyness_tenor_key,
    tenor_key,
)
from executor.snapshot_builder import SnapshotResult, build_snapshot

Arm = Literal["B", "C"]
Direction = Literal["call", "put"]

#: Arms that carry a checklist. Arm A is dumb money by design (brief) and never plans.
ACTIVE_ARMS: tuple[Arm, ...] = ("B", "C")
DIRECTIONS: tuple[Direction, ...] = ("call", "put")

ArmKey = tuple[str, str]


class HuntPlanError(ValueError):
    """Raised when the inputs cannot produce an honest plan at all."""


# ---------------------------------------------------------------------------
# data provider (injectable; tests drive the Phase-3a fixtures)
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataProvider(Protocol):
    """What the plan needs from the world. Two methods, no opinions.

    The Alpaca-backed implementation lands with the intraday watch loop; until then
    tests (and offline replays) inject :class:`StaticMarketData`.
    """

    def daily_series(self, symbol: str) -> BarSeries: ...

    def option_chain(self, symbol: str) -> OptionChain | None: ...


@dataclass(frozen=True, slots=True)
class StaticMarketData:
    """Pre-loaded market data. No I/O — everything is handed in at construction."""

    series: BarSeries
    chain: OptionChain | None = None

    def daily_series(self, symbol: str) -> BarSeries:
        if self.series.symbol != symbol:
            raise HuntPlanError(
                f"StaticMarketData holds {self.series.symbol}, asked for {symbol}"
            )
        return self.series

    def option_chain(self, symbol: str) -> OptionChain | None:
        if self.chain is not None and self.chain.underlying != symbol:
            raise HuntPlanError(
                f"chain is for {self.chain.underlying}, asked for {symbol}"
            )
        return self.chain


# ---------------------------------------------------------------------------
# watch levels + trigger prices
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerPrice:
    """The level that would flip one failing condition — or ``None`` plus a reason.

    ``condition`` is the checklist id; ``sense`` is the direction the price has to move
    for the level to matter ("above" / "below" / ``None`` when there is no level).
    """

    condition: str
    level: float | None
    sense: str | None
    status: str
    reason: str

    @property
    def actionable(self) -> bool:
        return self.level is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "level": self.level,
            "sense": self.sense,
            "status": self.status,
            "actionable": self.actionable,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class WatchLevels:
    """Today's watchlist: the numbers an intraday loop would watch."""

    close: float
    ema_fast: float | None = None
    ema_slow: float | None = None
    bollinger_upper: float | None = None
    bollinger_middle: float | None = None
    bollinger_lower: float | None = None
    prior_high: float | None = None
    prior_low: float | None = None
    arm_criteria: dict[str, Any] = field(default_factory=dict)
    arm_b_criteria: dict[str, Any] = field(default_factory=dict)
    strike_projection: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "close": self.close,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "bollinger_upper": self.bollinger_upper,
            "bollinger_middle": self.bollinger_middle,
            "bollinger_lower": self.bollinger_lower,
            "prior_high": self.prior_high,
            "prior_low": self.prior_low,
            "arm_b_criteria": dict(self.arm_b_criteria),
            "arm_c_criteria": dict(self.arm_criteria),
            "strike_projection": self.strike_projection,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RangeProjection:
    """Where the strike rule's "setup-day range projection" sits.

    # INTERPRETATION: the brief's arm-B strike rule is "first OTM strike beyond the
    # setup-day range projection" and gives no formula. Read here as: take the setup
    # day's own high-low range and project it one further range beyond the setup day's
    # extreme in the trade direction — call: ``high + range*factor``, put:
    # ``low - range*factor``, factor defaulting to 1.0 (a full-range extension). The
    # factor is a named parameter, recorded in every payload that uses it, so freezing
    # a different value is a rule change (visible as a strategy-version seam) rather
    # than a silent edit.
    """

    level: float
    sense: Literal["above", "below"]
    setup_high: float
    setup_low: float
    setup_range: float
    factor: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "sense": self.sense,
            "setup_day_high": self.setup_high,
            "setup_day_low": self.setup_low,
            "setup_day_range": self.setup_range,
            "factor": self.factor,
            "formula": "call: setup_high + range*factor; put: setup_low - range*factor",
        }


def project_setup_day_range(
    bar: Bar, direction: Direction, *, factor: float = 1.0
) -> RangeProjection:
    if factor < 0:
        raise HuntPlanError(f"projection factor must be >= 0, got {factor}")
    span = bar.h - bar.l
    if direction == "call":
        return RangeProjection(bar.h + span * factor, "above", bar.h, bar.l, span, factor)
    return RangeProjection(bar.l - span * factor, "below", bar.h, bar.l, span, factor)


# ---------------------------------------------------------------------------
# trigger computation
# ---------------------------------------------------------------------------

_NO_LEVEL = {
    "T2": "RSI is a 14-close statistic — no single price level flips it; it resolves "
          "from the sequence of closes, not from a level",
    "T2b_line": "the MACD line/signal relationship is an EMA-vs-EMA difference — no "
                "single price level flips it",
    "T2b_hist": "MACD histogram expansion is a comparison of the last two computed "
                "histogram values — not reachable as a price level",
    "T2b_cross": "the fresh-cross whipsaw guard is AGE-based: it clears when the most "
                 "recent adverse cross ages past max_age_hours, not at a price",
    "T3a": "squeeze release is a bandwidth-percentile read over a completed session "
           "(bandwidth percentile of 60d vs the squeeze threshold) — the newest bar "
           "cannot be made to satisfy it by a pre-set price level",
    "T4": "relative volume is a volume ratio against the 20d baseline — a price level "
          "does not change the volume that was printed",
    "T5_iv": "IV rank is a percentile of an implied-volatility distribution, not a "
             "price level; it re-scores on its own as the IV store accumulates",
    "T5_event": "event-day is a hard calendar veto — it clears on a non-event session, "
                "not at a price",
    "T5_arm_c": "arm C's IV band is calibration_pending in the frozen rulebook, so "
                "there is no threshold for a level to cross",
    "T6": "T6 is an EOD-confirmed deep-OTM 0DTE flow-volume ratio — no price level, and "
          "its multiplier is calibration_pending in the frozen rulebook",
    "PENDING": "condition is PENDING (missing data or uncalibrated threshold) — "
               "there is no threshold for a price level to cross",
}


def _trigger_for(
    condition_id: str,
    result_status: str,
    detail: str,
    snapshot_result: SnapshotResult,
    rules: Rulebook,
    direction: Direction,
    event_day_veto: bool,
) -> TriggerPrice:
    """One trigger, honest about whether a price level can exist at all."""
    snap = snapshot_result.snapshot

    if result_status == "PENDING":
        return TriggerPrice(condition_id, None, None, "PENDING",
                            f"{_NO_LEVEL['PENDING']} — {detail}")

    # -- T1: the one condition with a genuine price trigger when EMAs align -----
    if condition_id == "T1":
        fast, slow = snap.ema_fast, snap.ema_slow
        if fast is None or slow is None:
            return TriggerPrice("T1", None, None, "FAIL",
                                f"{_NO_LEVEL['PENDING']} — {detail}")
        if direction == "call" and fast <= slow:
            return TriggerPrice(
                "T1", None, None, "FAIL",
                f"EMA{fast_label(rules)} <= EMA{slow_label(rules)}: the fast EMA sits at or "
                f"below the slow one, so NO price satisfies T1 today — this needs an EMA "
                f"crossover (a time event), not a level",
            )
        if direction == "put" and fast >= slow:
            return TriggerPrice(
                "T1", None, None, "FAIL",
                f"EMA{fast_label(rules)} >= EMA{slow_label(rules)}: the fast EMA sits at or "
                f"above the slow one, so NO price satisfies T1 today — this needs an EMA "
                f"crossover (a time event), not a level",
            )
        level = fast
        sense = "above" if direction == "call" else "below"
        return TriggerPrice(
            "T1", level, sense, "FAIL",
            f"EMA{fast_label(rules)}={fast:.4f} vs EMA{slow_label(rules)}={slow:.4f} "
            f"(stack aligned for {direction}); a close {sense} {level:.4f} completes T1 — "
            f"everything else in T1 already holds",
        )

    # -- T3b: a band edge is a real trigger once the bar has tagged it ----------
    if condition_id == "T3b":
        bb = snap.bollinger
        if bb is None:
            return TriggerPrice("T3b", None, None, "FAIL",
                                f"{_NO_LEVEL['PENDING']} — {detail}")
        if direction == "call" and bb.low <= bb.lower and snap.close <= bb.lower:
            return TriggerPrice(
                "T3b", bb.lower, "above", "FAIL",
                f"the signal bar tagged the lower band (low {bb.low:.4f} <= "
                f"{bb.lower:.4f}) but closed outside it (close {snap.close:.4f}); a close "
                f"back above {bb.lower:.4f} completes the rejection",
            )
        if direction == "put" and bb.high >= bb.upper and snap.close >= bb.upper:
            return TriggerPrice(
                "T3b", bb.upper, "below", "FAIL",
                f"the signal bar tagged the upper band (high {bb.high:.4f} >= "
                f"{bb.upper:.4f}) but closed outside it (close {snap.close:.4f}); a close "
                f"back below {bb.upper:.4f} completes the rejection",
            )
        return TriggerPrice(
            "T3b", None, None, "FAIL",
            "band rejection needs the signal bar to tag the band and close back inside — "
            "a candle-shape condition, not a pre-set level (the level only becomes a "
            "trigger once a bar has tagged the band and closed outside it)",
        )

    if condition_id == "T2b":
        if "WHIPSAW GUARD" in detail:
            return TriggerPrice("T2b", None, None, "FAIL", _NO_LEVEL["T2b_cross"])
        if "CONTRACTING" in detail:
            return TriggerPrice("T2b", None, None, "FAIL", _NO_LEVEL["T2b_hist"])
        return TriggerPrice("T2b", None, None, "FAIL", _NO_LEVEL["T2b_line"])

    if condition_id == "T2":
        return TriggerPrice("T2", None, None, "FAIL", _NO_LEVEL["T2"])
    if condition_id == "T3a":
        return TriggerPrice("T3a", None, None, "FAIL", _NO_LEVEL["T3a"])
    if condition_id == "T4":
        return TriggerPrice("T4", None, None, "FAIL", _NO_LEVEL["T4"])
    if condition_id == "T6":
        return TriggerPrice("T6", None, None, "FAIL", _NO_LEVEL["T6"])
    if condition_id == "T5":
        if event_day_veto:
            return TriggerPrice("T5", None, None, "FAIL", _NO_LEVEL["T5_event"])
        if "EVENT DAY" in detail:
            return TriggerPrice("T5", None, None, "FAIL", _NO_LEVEL["T5_event"])
        if "calibration" in detail.lower() or "calibrated band" in detail:
            return TriggerPrice("T5", None, None, "FAIL", _NO_LEVEL["T5_arm_c"])
        return TriggerPrice("T5", None, None, "FAIL", _NO_LEVEL["T5_iv"])
    return TriggerPrice(condition_id, None, None, "FAIL", f"no trigger rule for {condition_id}")


def fast_label(rules: Rulebook) -> int:
    return rules.checklist.t1_ema_alignment.ema_fast


def slow_label(rules: Rulebook) -> int:
    return rules.checklist.t1_ema_alignment.ema_slow


# ---------------------------------------------------------------------------
# arm plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArmPlan:
    """One (arm, direction) cell of today's hunt plan."""

    arm: str
    direction: str
    checklist: ChecklistResult
    triggers: tuple[TriggerPrice, ...]
    watch: WatchLevels
    decision_kind: Literal["PROPOSAL", "NO_TRADE"]
    conviction: int
    reasoning: str
    snapshot_dict: dict[str, Any]

    @property
    def key(self) -> ArmKey:
        return (self.arm, self.direction)

    @property
    def fire(self) -> bool:
        return self.checklist.fire

    def failed(self) -> tuple[str, ...]:
        return tuple(
            cid for cid, c in self.checklist.conditions.items()
            if c.blocking and c.status == "FAIL"
        )

    def pending(self) -> tuple[str, ...]:
        return tuple(
            cid for cid, c in self.checklist.conditions.items()
            if c.blocking and c.status == "PENDING"
        )

    def to_state(self) -> dict[str, Any]:
        """``checklist_state`` payload for the journal row.

        Shape: the checklist's own per-condition outcomes (the schema's example is
        ``{"T4": {"pass": false, "reason": "..."}}``) plus the plan-level facts a
        reader needs to reconstruct the decision: fire, veto reasons, which kind of
        journal row this is, and the plan output (triggers + watch levels).
        """
        return {
            "fire": self.checklist.fire,
            "decision_kind": self.decision_kind,
            "failed_conditions": list(self.failed()),
            "pending_conditions": list(self.pending()),
            "conditions": {k: v.to_dict() for k, v in self.checklist.conditions.items()},
            "veto_reasons": list(self.checklist.veto_reasons),
            "trigger_prices": [t.to_dict() for t in self.triggers],
            "watch_levels": self.watch.to_dict(),
            "conviction": self.conviction,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "direction": self.direction,
            "decision_kind": self.decision_kind,
            "conviction": self.conviction,
            "reasoning": self.reasoning,
            "checklist": self.checklist.to_dict(),
            "triggers": [t.to_dict() for t in self.triggers],
            "watch_levels": self.watch.to_dict(),
            "snapshot": self.snapshot_dict,
        }


@dataclass(frozen=True, slots=True)
class HuntPlan:
    """Today's plan across both directions and both active arms."""

    day: dt.date
    symbol: str
    generated_ts: str
    strategy_version: str
    config_sha256: str
    arms: tuple[ArmPlan, ...]
    event_day_veto: bool
    event_reasons: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def cells(self) -> tuple[ArmKey, ...]:
        return tuple(a.key for a in self.arms)

    def cell(self, arm: str, direction: str) -> ArmPlan:
        for a in self.arms:
            if a.arm == arm and a.direction == direction:
                return a
        raise HuntPlanError(f"no plan cell for arm={arm} direction={direction}")

    def firing(self) -> tuple[ArmKey, ...]:
        return tuple(a.key for a in self.arms if a.fire)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "symbol": self.symbol,
            "generated_ts": self.generated_ts,
            "strategy_version": self.strategy_version,
            "config_sha256": self.config_sha256,
            "event_day_veto": self.event_day_veto,
            "event_reasons": list(self.event_reasons),
            "notes": list(self.notes),
            "arms": [a.to_dict() for a in self.arms],
        }


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def _conviction(result: ChecklistResult) -> int:
    """See module INTERPRETATION: deterministic, ranking-only, not calibrated."""
    if result.fire:
        return 10
    failed = sum(1 for c in result.conditions.values() if c.blocking and c.status == "FAIL")
    pending = sum(
        1 for c in result.conditions.values() if c.blocking and c.status == "PENDING"
    )
    return max(1, min(10, 10 - 3 * failed - 1 * pending))


def _kind(result: ChecklistResult) -> Literal["PROPOSAL", "NO_TRADE"]:
    return "PROPOSAL" if result.fire else "NO_TRADE"


def _reasoning(
    arm: str,
    direction: str,
    result: ChecklistResult,
    triggers: tuple[TriggerPrice, ...],
    event_day_veto: bool,
) -> str:
    if result.fire:
        return (
            f"{arm}/{direction}: checklist fires on the prior close. PROPOSAL only — a "
            f"contingent plan, no order placed; the intraday loop must re-verify every "
            f"condition with live data at trigger time (brief: stale checklist entries "
            f"are forbidden)."
        )
    parts = [f"{arm}/{direction}: NO TRADE at plan time."]
    if event_day_veto:
        parts.append("Event-day hard veto (T5) suppresses the whole session.")
    failed = [cid for cid, c in result.conditions.items() if c.blocking and c.status == "FAIL"]
    pending = [
        cid for cid, c in result.conditions.items() if c.blocking and c.status == "PENDING"
    ]
    if failed:
        parts.append("Failed: " + ", ".join(failed) + ".")
    if pending:
        parts.append("Pending (blocks, does not pass): " + ", ".join(pending) + ".")
    actionable = [t for t in triggers if t.actionable]
    if actionable:
        parts.append(
            "Price triggers that would move this cell: "
            + "; ".join(f"{t.condition} {t.sense} {t.level:.4f}" for t in actionable)
            + "."
        )
    else:
        parts.append("No failing condition here is flippable by a price level.")
    return " ".join(parts)


def _watch_levels(
    rules: Rulebook,
    arm: str,
    direction: str,
    snapshot_result: SnapshotResult,
    prior_bar: Bar | None,
    projection_factor: float,
    extra_notes: Sequence[str] = (),
) -> WatchLevels:
    snap = snapshot_result.snapshot
    bb = snap.bollinger
    arm_criteria: dict[str, Any] = {}
    arm_b_criteria: dict[str, Any] = {}
    projection: dict[str, Any] | None = None
    notes: list[str] = list(extra_notes)

    if arm == "B":
        entry = rules.arms.B.entry
        exits = rules.arms.B.exits
        arm_b_criteria = {
            "entry_window_et": {
                "start": entry.window_et.start,
                "end": entry.window_et.end,
            },
            "dte": entry.dte,
            "right": entry.right,
            "moneyness": entry.moneyness,
            "strike_rule": entry.strike_rule,
            "premium_cap_usd": min(
                entry.premium_pct_of_bankroll_max * rules.arms.B.bankroll_usd,
                entry.premium_usd_hard_cap,
            ),
            "hard_close_et": exits.hard_close_et,
            "time_stop_minutes": exits.time_stop.after_entry_minutes,
            "time_stop_gain_pct": exits.time_stop.if_under_gain_pct,
            "profit_take_close_pct_at_100pct": exits.profit_take.close_pct_at_100pct_gain,
            "max_concurrent_positions": rules.arms.B.limits.max_concurrent_positions,
            "max_entries_per_week": rules.arms.B.limits.max_entries_per_week,
        }
        if prior_bar is not None:
            proj = project_setup_day_range(prior_bar, direction, factor=projection_factor)
            projection = proj.to_dict()
            projection["provisional"] = True
            projection["basis"] = (
                f"prior completed session bar ({prior_bar.t.date().isoformat()}); the "
                f"real setup-day range is only known at EOD"
            )
    elif arm == "C":
        entry = rules.arms.C.entry
        arm_criteria = {
            # RULED 2026-10-03 (operator): arm C declares an entry window, so the plan
            # carries it the same way arm B's does. The watch loop's arming path and
            # the entry governor both read the rulebook's own window; this copy is what
            # lands on the journalled cell, so a row can explain its own timing later.
            "entry_window_et": {
                "start": entry.window_et.start,
                "end": entry.window_et.end,
            },
            "right": entry.right,
            "dte_min": entry.dte.min,
            "dte_max": entry.dte.max,
            "delta_min": entry.delta_min,
            # FIX (2026-10-03, full-cycle rehearsal): the watch loop's live arm C
            # selection reads the bankroll from here to apply the premium cap. The
            # dict did not carry it, so ``_arm_c_contract`` computed a max premium of
            # 0 and could never select a contract.
            "bankroll_usd": rules.arms.C.bankroll_usd,
            "premium_pct_of_bankroll_max": entry.premium_pct_of_bankroll_max,
            "roll_delta_below": rules.arms.C.exits.roll.trigger.delta_below,
            "roll_dte_below": rules.arms.C.exits.roll.trigger.dte_below,
            "max_concurrent_positions": rules.arms.C.limits.max_concurrent_positions,
        }

    return WatchLevels(
        close=snap.close,
        ema_fast=snap.ema_fast,
        ema_slow=snap.ema_slow,
        bollinger_upper=None if bb is None else bb.upper,
        bollinger_middle=None if bb is None else bb.middle,
        bollinger_lower=None if bb is None else bb.lower,
        prior_high=None if prior_bar is None else prior_bar.h,
        prior_low=None if prior_bar is None else prior_bar.l,
        arm_criteria=arm_criteria,
        arm_b_criteria=arm_b_criteria,
        strike_projection=projection,
        notes=tuple(notes),
    )


def _dte_of(contract: OptionContract, day: dt.date) -> int | None:
    """Calendar days from ``day`` to the contract's expiry, or ``None`` if unparseable.

    Same reading as ``noshot._dte``: the session date, never ``datetime.now()``.
    """
    if not contract.expiry:
        return None
    try:
        return max((dt.datetime.strptime(contract.expiry, "%Y%m%d").date() - day).days, 0)
    except ValueError:
        return None


def _iv_contract_for_arm(
    chain: OptionChain,
    spot: float,
    right: str,
    day: dt.date,
    *,
    dte_min: int,
    dte_max: int,
) -> OptionContract | None:
    """The contract arm ``X``'s IV gate should read: nearest-to-spot INSIDE its DTE band.

    FIX 2026-10-03 (outsider review 4.2). This replaces a whole-chain, no-DTE-filter
    nearest-strike search (review finding F13), which made both arms read front-month
    regardless of what they were about to trade.

    Why it matters for arm C specifically. ``docs/brief.md`` T5 says the IV gate for
    arm C is "IV on the **chosen strike** within normal band" — the chosen strike being
    the 90-180 DTE, delta>=0.80 deep-ITM call from ``arms.C.entry.dte``. The code
    instead picked the strike nearest spot across every expiry in the chain, which for a
    real SPY chain is a front-month contract a quarter of a year away from the one arm C
    would buy. So arm C's "IV is normal" evidence was being read off an instrument it
    was not trading: a 130-DTE contract's IV regime is not a 0DTE's, and the gate
    exists precisely to refuse hostile IV *for the position being taken*.

    The band is the rulebook's own ``arms.X.entry.dte`` (arm B: 0; arm C: 90-180),
    passed in rather than hard-coded here, so the plan cannot drift from the rulebook
    or from the live selectors in ``watch_loop``. Within the band the nearest-to-spot
    contract is chosen, matching the selection the EOD soak accumulates
    (``soak.py`` records one ATM contract per expiry) — so the gate reads a series that
    can actually warm up, instead of a deep-ITM strike bucket nothing has ever written.

    ``None`` means the chain has no contract of that right inside the band, and T5
    reports ``no_source`` rather than falling back to a tenor the arm does not trade.
    Falling back is the bug this fixes.
    """
    pool: list[OptionContract] = []
    for contract in chain.contracts:
        if contract.right != right:
            continue
        dte = _dte_of(contract, day)
        if dte is None or not (dte_min <= dte <= dte_max):
            continue
        pool.append(contract)
    if not pool:
        return None
    return min(pool, key=lambda c: abs(c.strike - spot))


#: Strike-bucket width is the single constant in ``executor.iv_rank`` (FIX
#: 2026-10-03, R4), so the plan cannot drift from the soak that writes the series.
TENOR_STRIKE_BUCKET = STRIKE_BUCKET_SIZE

#: RULED 2026-10-03 (operator): the ROLLING tenor key's strike axis is
#: moneyness-relative at this width, not absolute dollars. Same single-constant
#: discipline as ``TENOR_STRIKE_BUCKET`` above — the plan reads the width from
#: ``executor.iv_rank`` so it cannot drift from the soak that writes the series.
TENOR_MONEYNESS_BUCKET_PCT = MONEYNESS_BUCKET_PCT


def _tenor_key_for(
    contract: OptionContract,
    day: dt.date,
    *,
    mode: str,
    dte_bucket_days: int,
    spot: float | None = None,
) -> str:
    """The IV-rank series key T5 reads, per the rulebook's ``tenor_key_mode``.

    RULED 2026-10-03 (operator, R4): ``rolling_dte``. The rolling key needs a DTE,
    which means a calendar the caller has to supply — this derives it from the
    contract's own expiry rather than from the chain's far expiry, because a chain
    with a March LEAPS leg and a Friday weekly would otherwise put arm B's 0DTE
    reading under a 150-day key and never warm up.

    RULED 2026-10-03 (operator, key schema): ``spot`` is REQUIRED for the rolling mode,
    because the strike axis is moneyness-relative (``strike/spot - 1``, bucketed at 2%
    — ``MONEYNESS_BUCKET_PCT``). The dollar axis it replaced rotated with spot, so the
    key of an ATM contract walked off its own series every time SPY moved a few percent
    and no tenor could reach ``MIN_OBSERVATIONS``. A missing spot is refused rather than
    defaulted: falling back to the dollar axis would write the plan under a key schema
    the soak no longer writes, and T5 would sit in warmup forever with no error.
    """
    if mode == "expiry":
        return tenor_key(
            expiry=contract.expiry, right=contract.right, strike=contract.strike
        )
    try:
        expiry_day = dt.datetime.strptime(contract.expiry, "%Y%m%d").date()
    except ValueError:
        # A contract with an unparseable expiry cannot be placed on the DTE axis.
        # Falling back to the expiry key is the conservative move: it yields a series
        # that will not warm up (T5 stays PENDING, no trade) rather than one that
        # quietly borrows another expiry's history (T5 fires on the wrong tenor).
        return tenor_key(
            expiry=contract.expiry, right=contract.right, strike=contract.strike
        )
    if spot is None:
        raise ValueError(
            "spot is required for the rolling (moneyness) tenor key: the strike axis is "
            "bucketed relative to spot, so without it the key cannot be built at all"
        )
    dte = max((expiry_day - day).days, 0)
    return moneyness_tenor_key(
        dte=dte,
        right=contract.right,
        moneyness_bucket=moneyness_bucket(
            contract.strike, spot, TENOR_MONEYNESS_BUCKET_PCT
        ),
        dte_bucket_days=dte_bucket_days,
    )


def _iv_rank_for_direction(
    chain: OptionChain | None,
    store: IvRankStore | None,
    spot: float | None,
    direction: Direction,
    day: dt.date,
    *,
    dte_min: int,
    dte_max: int,
    tenor_key_mode: str = "rolling_dte",
    dte_bucket_days: int = 7,
) -> tuple[float | None, dict[str, Any]]:
    """IV rank for THIS ARM's traded tenor, via the injected store.

    Returns ``(rank_or_None, provenance)``. The provenance is kept even when the rank
    is undefined: "warmup" with 12 observations and "no history" are different facts
    and the journal should show which one blocked T5.

    ``dte_min``/``dte_max`` are the calling arm's rulebook DTE band (FIX 2026-10-03,
    outsider review 4.2): arm B reads its 0DTE contract, arm C reads a contract inside
    its 90-180 DTE band. They are required keywords rather than defaulted, so a new
    call site has to say which tenor it means instead of inheriting "whichever expiry
    the API listed first".
    """
    if chain is None or store is None:
        reason = (
            "no options chain supplied" if chain is None else "no IV-rank store supplied"
        )
        return None, {"status": "no_source", "reason": reason}
    if spot is None:
        return None, {"status": "no_source", "reason": "no spot for ATM tenor selection"}
    contract = _iv_contract_for_arm(
        chain, spot, direction, day, dte_min=dte_min, dte_max=dte_max
    )
    band = f"{dte_min}-{dte_max}"
    if contract is None:
        return None, {
            "status": "no_source",
            "reason": (
                f"chain carries no {direction} contracts inside the {band} DTE band — "
                "the IV gate reads the arm's traded tenor only, so it reports no source "
                "rather than scoring a tenor this arm does not buy"
            ),
            "dte_band": band,
        }
    if contract.implied_volatility is None:
        return None, {
            "status": "no_source",
            "reason": f"{contract.symbol} carries no impliedVolatility to score",
            "contract": contract.symbol,
        }
    key = _tenor_key_for(
        contract,
        day,
        mode=tenor_key_mode,
        dte_bucket_days=dte_bucket_days,
        spot=spot,
    )
    result = store.iv_rank(
        contract.implied_volatility,
        contract.underlying,
        key,
        as_of=day,
    )
    payload = result.to_dict()
    payload["contract"] = contract.symbol
    payload["current_iv"] = contract.implied_volatility
    # FIX 2026-10-03 (outsider review 4.2): record WHICH tenor answered — the expiry and
    # the arm's band, not just the symbol. A rank of 31 for arm C is only interpretable
    # next to "read off a 121-DTE call", because before this fix the same code could
    # return that number off a 7-DTE call and nothing in the journal would say so.
    payload["iv_dte_band"] = band
    payload["iv_expiry"] = contract.expiry
    payload["iv_dte"] = _dte_of(contract, day)
    # Recorded so the journal says WHICH series answered, not just what it returned.
    # Under R4 the two modes are a live switch, so a rank of 31 is only interpretable
    # next to the key that produced it.
    payload["tenor_key"] = key
    payload["tenor_key_mode"] = tenor_key_mode
    return result.rank, payload


def compute_triggers(
    result: ChecklistResult,
    snapshot_result: SnapshotResult,
    rules: Rulebook,
    direction: Direction,
    event_day_veto: bool = False,
) -> tuple[TriggerPrice, ...]:
    """One :class:`TriggerPrice` per blocking condition that is not PASS.

    Plus, per the module INTERPRETATION above, the non-blocking arm of a T3 OR group
    when it has actually failed — it cannot veto, but its price level is the one the
    plan is for, and its reason is prefixed ``NON-BLOCKING`` so nothing downstream
    mistakes it for a veto.

    Separate from :func:`build_hunt_plan` so the trigger logic can be exercised
    directly against a hand-built snapshot (which is the only way to reach, say, "T1
    fails purely because the close is under EMA50 while the EMA stack is aligned").
    """
    triggers = [
        _trigger_for(
            cid, cond.status, cond.detail, snapshot_result, rules, direction, event_day_veto
        )
        for cid, cond in result.conditions.items()
        if cond.blocking and cond.status != "PASS"
    ]
    for cid in ("T3a", "T3b"):
        cond = result.conditions.get(cid)
        if cond is None or cond.blocking or cond.status in ("PASS", "PENDING"):
            continue
        trigger = _trigger_for(
            cid, cond.status, cond.detail, snapshot_result, rules, direction, event_day_veto
        )
        triggers.append(
            dataclasses.replace(
                trigger,
                reason=(
                    f"NON-BLOCKING: T3 is satisfied_if_any_of and the sibling PASSes, so "
                    f"{cid} cannot veto; this level is a watch level, not a blocker — and "
                    f"if {cid} does flip to PASS the T3 group is satisfied through it too. "
                    f"{trigger.reason}"
                ),
            )
        )
    return tuple(triggers)


def make_arm_plan(
    *,
    arm: str,
    direction: str,
    snapshot_result: SnapshotResult,
    rules: Rulebook,
    prior_bar: Bar | None = None,
    event_day_veto: bool = False,
    projection_factor: float = 1.0,
    extra_snapshot: dict[str, Any] | None = None,
    extra_notes: Sequence[str] = (),
) -> ArmPlan:
    """Evaluate the checklist for one (arm, direction) cell and package the plan.

    The single place a cell is built, so the journal row and the in-memory plan can
    never disagree about what was evaluated.
    """
    if arm not in ACTIVE_ARMS:
        raise HuntPlanError(f"unknown arm {arm!r}; active arms are {ACTIVE_ARMS}")
    if direction not in DIRECTIONS:
        raise HuntPlanError(f"unknown direction {direction!r}; directions are {DIRECTIONS}")

    result = evaluate(snapshot_result.snapshot, rules, direction, arm)  # type: ignore[arg-type]
    triggers = compute_triggers(result, snapshot_result, rules, direction, event_day_veto)  # type: ignore[arg-type]
    watch = _watch_levels(
        rules,
        arm,
        direction,
        snapshot_result,
        prior_bar,
        projection_factor,
        extra_notes=extra_notes,
    )
    snapshot_dict = snapshot_result.to_dict(direction=direction, arm=arm)
    if extra_snapshot:
        snapshot_dict.update(extra_snapshot)
    # R2: recorded here, where the evaluation actually happened, so the watch loop can
    # tell "green at 08:30" from "green right now" without re-deriving it — and it is
    # set AFTER the caller's extras so it cannot be overwritten by a stale value.
    snapshot_dict["plan_green"] = bool(result.fire)
    return ArmPlan(
        arm=arm,
        direction=direction,
        checklist=result,
        triggers=triggers,
        watch=watch,
        decision_kind=_kind(result),
        conviction=_conviction(result),
        reasoning=_reasoning(arm, direction, result, triggers, event_day_veto),
        snapshot_dict=snapshot_dict,
    )


def build_hunt_plan(
    *,
    data: MarketDataProvider,
    rules: Rulebook,
    day: dt.date,
    calendar: EventCalendar | None = None,
    iv_store: IvRankStore | None = None,
    chain: OptionChain | None = None,
    spot: float | None = None,
    flow_gate: Any | None = None,
    arms: Sequence[str] = ACTIVE_ARMS,
    directions: Sequence[str] = DIRECTIONS,
    projection_factor: float = 1.0,
    generated_ts: str | None = None,
    baseline_series: BarSeries | None = None,
) -> HuntPlan:
    """Build today's hunt plan.

    ``day`` is the **session date the plan is for**. The newest bar in the series is the
    signal bar (normally the prior close, since the plan runs pre-market) — that is the
    snapshot builder's own convention and it is not re-derived here.
    """
    symbol = rules.strategy.symbol
    series = data.daily_series(symbol)
    if not series.bars:
        raise HuntPlanError("daily series has no bars — cannot build a hunt plan")
    if chain is None:
        chain = data.option_chain(symbol)

    prior_bar = series.bars[-1]
    notes: list[str] = []
    if spot is None:
        spot = series.bars[-1].c
        notes.append(
            f"spot defaulted to the newest close {spot:.4f} (pre-market: no live spot); "
            f"IV-rank tenor selection is keyed on it"
        )

    veto_kinds = rules.checklist.t5_options_chain.event_calendar.veto_kinds
    event_reasons: tuple[str, ...] = ()
    event_day_veto = False
    if calendar is not None:
        vetoes = calendar.veto_events_on(day, veto_kinds=veto_kinds)
        event_day_veto = bool(vetoes)
        event_reasons = tuple(calendar.veto_reasons(day, veto_kinds=veto_kinds))

    cells: list[ArmPlan] = []
    for arm in arms:
        for direction in directions:
            if arm not in ACTIVE_ARMS:
                raise HuntPlanError(f"unknown arm {arm!r}; active arms are {ACTIVE_ARMS}")
            if direction not in DIRECTIONS:
                raise HuntPlanError(
                    f"unknown direction {direction!r}; directions are {DIRECTIONS}"
                )

            extra_notes: list[str] = []
            # FIX 2026-10-03 (outsider review 4.2): the IV gate reads each arm's OWN
            # traded tenor, taken from the rulebook's entry DTE band (arm B: 0, arm C:
            # 90-180) so the plan cannot drift from the rulebook. Before this, both arms
            # read the nearest-strike contract across the whole chain — front-month for
            # arm C, whose actual trade is a 90-180 DTE contract.
            if arm == "B":
                iv_dte_min = iv_dte_max = rules.arms.B.entry.dte
            else:
                iv_dte_min = rules.arms.C.entry.dte.min
                iv_dte_max = rules.arms.C.entry.dte.max
            # Both arms now read an IV rank. Arm C used to be skipped here because its
            # T5 gate was calibration_pending and scoring a rank would imply a threshold
            # that did not exist; the 2026-10-02 ruling gave it a real ceiling (< 50, same
            # as arm B), so not scoring it would leave the gate unevaluable.
            iv_rank, iv_provenance = _iv_rank_for_direction(
                chain,
                iv_store,
                spot,
                direction,
                day,
                dte_min=iv_dte_min,
                dte_max=iv_dte_max,
                tenor_key_mode=rules.checklist.t5_options_chain.tenor_key_mode,
                dte_bucket_days=rules.checklist.t5_options_chain.dte_bucket_days,
            )
            extra_notes.append(f"IV-rank source: {iv_provenance}")

            snapshot_result = build_snapshot(
                daily=series,
                chain=chain,
                calendar=calendar,
                veto_kinds=veto_kinds,
                as_of=day,
                spot=spot,
                iv_rank=iv_rank,
                flow_gate=flow_gate,
                baseline_series=baseline_series,
                ema_fast=rules.checklist.t1_ema_alignment.ema_fast,
                ema_slow=rules.checklist.t1_ema_alignment.ema_slow,
                rsi_period=rules.checklist.t2_rsi.period,
                macd_fast=rules.checklist.t2b_macd.fast,
                macd_slow=rules.checklist.t2b_macd.slow,
                macd_signal=rules.checklist.t2b_macd.signal,
                bb_lookback_days=rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release.lookback_days,
                bb_squeeze_percentile=rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release.bandwidth_below_percentile,
                relvol_lookback_days=rules.checklist.t4_volume.lookback_days,
                cross_guard_hours=rules.checklist.t2b_macd.fresh_cross_guard.max_age_hours,
            )

            cell = make_arm_plan(
                arm=arm,
                direction=direction,
                snapshot_result=snapshot_result,
                rules=rules,
                prior_bar=prior_bar,
                event_day_veto=event_day_veto,
                projection_factor=projection_factor,
                extra_snapshot={
                    "iv_rank_provenance": iv_provenance,
                    # R4: the tenor key is lifted to a top-level field because the
                    # watch loop must be able to confirm it is re-verifying the SAME
                    # series the plan read. A rank without its key is not auditable
                    # under a ruling that keeps both keying modes reachable.
                    "iv_rank_tenor_key": iv_provenance.get("tenor_key"),
                    "iv_rank_tenor_mode": iv_provenance.get("tenor_key_mode"),
                    "watch_notes": list(extra_notes),
                },
                extra_notes=extra_notes,
            )
            cells.append(cell)

    if event_day_veto:
        notes.append(
            f"event-day hard veto on {day.isoformat()}: " + "; ".join(event_reasons)
        )

    return HuntPlan(
        day=day,
        symbol=symbol,
        generated_ts=generated_ts or dt.datetime.now(dt.UTC).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        strategy_version=rules.strategy_version,
        config_sha256=rules.config_sha256,
        arms=tuple(cells),
        event_day_veto=event_day_veto,
        event_reasons=event_reasons,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# journaling
# ---------------------------------------------------------------------------


def write_hunt_plan(
    conn: Any,
    plan: HuntPlan,
    *,
    ts: str | None = None,
) -> dict[ArmKey, int]:
    """Journal the plan: one immutable decision row per (arm, direction) cell.

    Returns ``{(arm, direction): decision_id}`` — the ids are what
    ``executor.noshot`` links each NO-SHOT row to via ``counterfactual_entry_ref``.

    Inserts only. Nothing here updates or deletes, so the append-only triggers on
    ``decisions`` are never exercised; a plan row can only be corrected by writing
    another plan (a new row), which is exactly what the brief demands.
    """
    from journal.store import DecisionEntry, append_decision

    stamp = ts or plan.generated_ts
    ids: dict[ArmKey, int] = {}
    for cell in plan.arms:
        entry = DecisionEntry(
            ts=stamp,
            arm=cell.arm,  # type: ignore[arg-type]
            kind=cell.decision_kind,
            checklist_snapshot=cell.snapshot_dict,
            checklist_state=cell.to_state(),
            reasoning=cell.reasoning,
            strategy_version=plan.strategy_version,
            symbol=plan.symbol,
            conviction=cell.conviction,
            references=[],
        )
        ids[cell.key] = append_decision(conn, entry)
    return ids


def summarise(plan: HuntPlan) -> str:
    """One-screen human summary of a plan (operator-facing, pre-market read)."""
    lines = [f"Hunt plan {plan.day.isoformat()} {plan.symbol} v{plan.strategy_version}"]
    if plan.event_day_veto:
        lines.append(f"  EVENT-DAY HARD VETO: {'; '.join(plan.event_reasons)}")
    for cell in plan.arms:
        flags = ",".join(
            # A non-blocking FAIL (the losing arm of the T3 OR group) is shown as such,
            # so the flag list never reads as a veto the checklist did not raise.
            f"{cid}:{c.status}{'' if c.blocking else '(non-blocking)'}"
            for cid, c in cell.checklist.conditions.items()
            if c.status != "PASS"
        )
        actionable = [t for t in cell.triggers if t.actionable]
        triggers = ", ".join(
            f"{t.condition} {t.sense} {t.level:.2f}" for t in actionable
        ) or "no price-level trigger"
        lines.append(
            f"  {cell.arm}/{cell.direction}: {cell.decision_kind} "
            f"conviction={cell.conviction} [{flags or 'all pass'}] triggers: {triggers}"
        )
    return "\n".join(lines)


__all__ = [
    "ACTIVE_ARMS",
    "DIRECTIONS",
    "ArmKey",
    "ArmPlan",
    "HuntPlan",
    "HuntPlanError",
    "MarketDataProvider",
    "RangeProjection",
    "StaticMarketData",
    "TriggerPrice",
    "WatchLevels",
    "build_hunt_plan",
    "compute_triggers",
    "make_arm_plan",
    "project_setup_day_range",
    "summarise",
    "write_hunt_plan",
]