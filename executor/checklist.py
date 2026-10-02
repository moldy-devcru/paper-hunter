"""Frozen-checklist evaluator — T1..T6 as booleans over a pre-computed snapshot.

This module is the deterministic half of the executor: given an indicator snapshot
and the frozen rulebook it answers exactly one question per condition, plus ``fire``.
No I/O, no network, no LLM. The brief's rationale applies verbatim — "a script does
boolean perfectly and an LLM does it expensively."

Three statuses, not two
-----------------------
``PASS`` / ``FAIL`` / ``PENDING``. PENDING is not a soft pass and not a fail: it is
the state of a condition whose threshold the rulebook has **not** calibrated yet
(T6's flow multiplier, arm C's IV band) or whose input history is not yet long
enough. Treating PENDING as pass would let an uncalibrated gate wave trades through;
treating it as fail would let an uncalibrated gate silently block every entry and
quietly change the experiment into a different one. So PENDING is recorded as itself,
``fire`` is False, and the reason string says exactly which calibration is missing.

Arm scoping
-----------
``T6`` applies to arm B only (``applies_to: [B]``). For arm C it is reported as
``SKIPPED`` — it neither passes nor blocks, and the ``ChecklistResult`` records that
so the journal snapshot shows the full seven-condition grid with the arm's real
scope visible rather than a six-item list with a silent gap.

Everything here reads thresholds from the loaded ``Rulebook``; no threshold is
duplicated as a literal in this file. That is what lets the rulebook stay the single
frozen source of truth for a veto.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

from config.loader import Rulebook

Direction = Literal["call", "put"]
Arm = Literal["B", "C"]
Status = Literal["PASS", "FAIL", "PENDING", "SKIPPED"]
FlowStatus = Literal["pass", "fail", "pending", "unknown"]


class ChecklistInputError(ValueError):
    """Raised when a snapshot is internally inconsistent (bad direction, NaN, ...)."""


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BollingerState:
    """Bollinger inputs the T3 conditions need, plus the squeeze-release inputs.

    ``squeeze`` is ``bandwidth_percentile < rulebook threshold`` evaluated by the
    caller at data-plumbing time; we accept it as a fact rather than recomputing
    percentiles here, because the percentile needs the 60-day bandwidth history that
    lives in the indicator stack, not in the snapshot.
    """

    upper: float
    middle: float
    lower: float
    low: float                 # signal-bar low  (band rejection touches)
    high: float                # signal-bar high (band rejection touches, put side)
    bandwidth: float
    bandwidth_percentile: float
    bandwidth_expanding: bool  # bandwidth > previous bar's bandwidth
    middle_rising: bool        # middle band up vs previous bar (expansion direction)
    squeeze: bool              # bandwidth below the 20th percentile of the 60d window


@dataclass(frozen=True, slots=True)
class FlowGate:
    """T6 inputs. ``status`` is the data layer's own read; the evaluator arbitrates.

    ``eod_confirmed`` reflects the rulebook's ``t6_flow.evaluation: EOD_only``: intraday
    flow numbers are not comparable to a 20-day daily baseline, so a mid-session read
    cannot satisfy T6 no matter what it says.
    """

    status: FlowStatus
    ratio: float | None = None      # deep-OTM 0DTE volume / 20-day baseline
    eod_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    """Every checklist value at decision time — the journal's required snapshot.

    The brief: "indicator snapshot (ALL checklist values, not just the deciding ones)".
    Nothing here is optional-by-computation: a condition that cannot be evaluated
    carries the missing piece as ``None`` and evaluates to PENDING, so the snapshot
    is honest about what the system actually knew when it decided.
    """

    close: float
    ema_fast: float | None
    ema_slow: float | None
    rsi: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    macd_cross_direction: Direction | None = None  # direction of the most recent cross
    macd_cross_age_hours: float | None = None      # age of that cross
    bollinger: BollingerState | None = None
    relvol: float | None = None
    iv_rank: float | None = None
    is_event_day: bool = False
    event_kinds: tuple[str, ...] = ()
    flow_gate: FlowGate = field(default_factory=lambda: FlowGate(status="unknown"))

    def validate(self) -> None:
        if not _finite(self.close):
            raise ChecklistInputError("close must be a finite number")


def _finite(value: float | None) -> bool:
    if value is None:
        return False
    return value == value and value not in (float("inf"), float("-inf"))


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConditionResult:
    id: str
    status: Status
    detail: str
    blocking: bool = True

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "detail": self.detail,
                "blocking": self.blocking}


@dataclass(frozen=True, slots=True)
class ChecklistResult:
    arm: Arm
    direction: Direction
    conditions: dict[str, ConditionResult]
    veto_reasons: tuple[str, ...]
    indicators: dict[str, Any]
    fire: bool

    def status(self, condition_id: str) -> Status:
        return self.conditions[condition_id].status

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "direction": self.direction,
            "fire": self.fire,
            "conditions": {k: v.to_dict() for k, v in self.conditions.items()},
            "veto_reasons": list(self.veto_reasons),
            "indicators": self.indicators,
        }


# ---------------------------------------------------------------------------
# individual conditions
# ---------------------------------------------------------------------------


def _t1(snap: IndicatorSnapshot, rules: Rulebook, direction: Direction) -> ConditionResult:
    cfg = rules.checklist.t1_ema_alignment
    fast_label, slow_label = f"EMA{cfg.ema_fast}", f"EMA{cfg.ema_slow}"
    if snap.ema_fast is None or snap.ema_slow is None:
        need = max(cfg.ema_fast, cfg.ema_slow)
        return ConditionResult("T1", "PENDING",
                              f"needs {fast_label}/{slow_label} (>= {need} closes); "
                              f"ema_fast={snap.ema_fast}, ema_slow={snap.ema_slow}")
    if direction == "call":
        ok = snap.close > snap.ema_fast > snap.ema_slow
        want = f"close > {fast_label} > {slow_label}"
    else:
        ok = snap.close < snap.ema_fast < snap.ema_slow
        want = f"close < {fast_label} < {slow_label}"
    return ConditionResult(
        "T1",
        "PASS" if ok else "FAIL",
        f"{'OK' if ok else 'NOT'}: {want} | close={snap.close:.4f}, "
        f"{fast_label}={snap.ema_fast:.4f}, {slow_label}={snap.ema_slow:.4f}",
    )


def _t2(snap: IndicatorSnapshot, rules: Rulebook, direction: Direction) -> ConditionResult:
    cfg = rules.checklist.t2_rsi
    if snap.rsi is None:
        return ConditionResult("T2", "PENDING",
                              f"RSI({cfg.period}) not yet defined (insufficient closes)")
    rsi = snap.rsi
    ex = cfg.exhaustion
    # Brief: "> 75 / < 25 = no new entries, wait for reset" — strict inequalities.
    if rsi > ex.high:
        return ConditionResult("T2", "FAIL",
                              f"EXHAUSTED: rsi={rsi:.2f} > {ex.high} "
                              f"({ex.action}) — wait for reset",
                              blocking=True)
    if rsi < ex.low:
        return ConditionResult("T2", "FAIL",
                              f"EXHAUSTED: rsi={rsi:.2f} < {ex.low} "
                              f"({ex.action}) — wait for reset",
                              blocking=True)
    ok = rsi > cfg.call_bias_min if direction == "call" else rsi < cfg.put_bias_max
    bound = cfg.call_bias_min if direction == "call" else cfg.put_bias_max
    comparator = ">" if direction == "call" else "<"
    return ConditionResult(
        "T2",
        "PASS" if ok else "FAIL",
        f"{'OK' if ok else 'NOT'}: {direction} bias needs rsi {comparator} {bound} "
        f"| rsi={rsi:.2f} (exhaustion band {ex.low}-{ex.high})",
    )


def _t2b(snap: IndicatorSnapshot, rules: Rulebook, direction: Direction) -> ConditionResult:
    cfg = rules.checklist.t2b_macd
    if snap.macd_line is None or snap.macd_signal is None:
        need = cfg.slow + cfg.signal - 1
        return ConditionResult("T2b", "PENDING",
                              f"MACD({cfg.fast},{cfg.slow},{cfg.signal}) needs >= {need} closes")

    opposite: Direction = "put" if direction == "call" else "call"
    above = snap.macd_line > snap.macd_signal
    aligned = above if direction == "call" else not above
    if not aligned:
        return ConditionResult(
            "T2b", "FAIL",
            f"line vs signal: macd={snap.macd_line:.6f} "
            f"{'below' if above else 'above'} signal={snap.macd_signal:.6f} "
            f"— against {direction} bias",
        )

    # Histogram: expanding is "preferred", contracting is a veto. See INTERPRETATION.
    hist_note = "histogram unavailable (no prior bar)"
    hist_status: Status | None = None
    if snap.macd_hist is None or snap.macd_hist_prev is None:
        hist_note = f"UNAVAILABLE: macd_hist={snap.macd_hist}, prev={snap.macd_hist_prev}"
    else:
        # INTERPRETATION: a flat histogram (bitwise unchanged vs the prior bar) is
        # neither expanding nor contracting, so it is not vetoed — the rulebook names
        # only "contracting" as the failing momentum state, "expanding" as preferred.
        contracting = (
            snap.macd_hist < snap.macd_hist_prev if direction == "call"
            else snap.macd_hist > snap.macd_hist_prev
        )
        expanding = not contracting and snap.macd_hist != snap.macd_hist_prev
        label = ("CONTRACTING (momentum fading)" if contracting
                 else "FLAT (neither expanding nor contracting)" if not expanding
                 else "expanding in trade direction")
        hist_note = (
            f"histogram {snap.macd_hist:.6f} vs prev {snap.macd_hist_prev:.6f}: {label}"
        )
        hist_status = "FAIL" if contracting else "PASS"
    if hist_status == "FAIL":
        return ConditionResult("T2b", "FAIL", hist_note + " — veto per t2b.histogram.contracting")

    # Fresh-cross whipsaw guard.
    guard = cfg.fresh_cross_guard
    # INTERPRETATION: the guard fires when the MOST RECENT cross (cross_direction +
    # cross age, supplied by the data layer) was AGAINST the trade direction and is
    # younger than max_age_hours. "No entries against a fresh MACD cross" is read this
    # way rather than "no entries within N hours of any cross": a fresh cross IN the
    # trade direction is exactly the setup we want. The exception is T3a squeeze-release
    # firing, as the rulebook states.
    squeeze_release = bool(snap.bollinger and snap.bollinger.squeeze
                           and snap.bollinger.bandwidth_expanding
                           and (snap.bollinger.middle_rising if direction == "call"
                                else not snap.bollinger.middle_rising))
    age = snap.macd_cross_age_hours
    if (snap.macd_cross_direction == opposite and age is not None and age < guard.max_age_hours
            and not squeeze_release):
        return ConditionResult(
            "T2b", "FAIL",
            f"FRESH CROSS WHIPSAW GUARD: cross {opposite} {age:.2f}h ago "
            f"(< {guard.max_age_hours}h) against a {direction} entry, and T3a squeeze-release "
            f"did not fire (exception_if={guard.exception_if})",
        )

    detail = f"macd={snap.macd_line:.6f} {'above' if above else 'below'} " \
             f"signal={snap.macd_signal:.6f}; {hist_note}"
    if hist_status is None:
        return ConditionResult("T2b", "PENDING", detail)
    return ConditionResult("T2b", "PASS", detail)


def _t3a(snap: IndicatorSnapshot, rules: Rulebook, direction: Direction) -> ConditionResult:
    cfg = rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release
    bb = snap.bollinger
    if bb is None:
        return ConditionResult("T3a", "PENDING", "bollinger state unavailable")
    # INTERPRETATION: "expanding in trade direction" is read as *directional*
    # expansion, not just a widening band: bandwidth must be rising AND the middle
    # band must be moving with the trade (rising for calls, falling for puts). A
    # widening band with a falling middle band is volatility expansion against us.
    expanding_dir = bb.middle_rising if direction == "call" else not bb.middle_rising
    ok = bb.squeeze and bb.bandwidth_expanding and expanding_dir
    return ConditionResult(
        "T3a",
        "PASS" if ok else "FAIL",
        f"squeeze={bb.squeeze} (bandwidth percentile {bb.bandwidth_percentile:.2f} vs "
        f"< {cfg.bandwidth_below_percentile} over {cfg.lookback_days}d), "
        f"bandwidth_expanding={bb.bandwidth_expanding}, "
        f"middle_{'rising' if bb.middle_rising else 'falling'}"
        f"{'' if expanding_dir else ' (against trade direction)'}",
    )


def _t3b(snap: IndicatorSnapshot, rules: Rulebook, direction: Direction) -> ConditionResult:
    cfg = rules.checklist.t3_bollinger.satisfied_if_any_of.band_rejection
    bb = snap.bollinger
    if bb is None:
        return ConditionResult("T3b", "PENDING", "bollinger state unavailable")
    # INTERPRETATION: "close tags lower band and closes back inside" = the signal
    # bar's LOW touched or undercut the lower band AND the close finished back above
    # it (inverted for puts: high tags the upper band, close back below). Touching
    # and closing back inside are both required; a close that ends outside the band
    # is a break, not a rejection.
    if direction == "call":
        tagged = bb.low <= bb.lower
        back_inside = snap.close > bb.lower
        ok = tagged and back_inside
        rule_text = cfg.calls
    else:
        tagged = bb.high >= bb.upper
        back_inside = snap.close < bb.upper
        ok = tagged and back_inside
        rule_text = cfg.puts
    return ConditionResult(
        "T3b",
        "PASS" if ok else "FAIL",
        f"{rule_text}: tagged={tagged}, back_inside={back_inside} | "
        f"close={snap.close:.4f}, lower={bb.lower:.4f}, upper={bb.upper:.4f}, "
        f"low={bb.low:.4f}, high={bb.high:.4f}",
    )


def _t4(snap: IndicatorSnapshot, rules: Rulebook) -> ConditionResult:
    cfg = rules.checklist.t4_volume
    if snap.relvol is None:
        return ConditionResult("T4", "PENDING",
                              f"relative volume unavailable "
                              f"(needs {cfg.lookback_days}d baseline)")
    relvol = snap.relvol
    # A non-finite ratio means the 20-day baseline was zero/unusable: volume did not
    # confirm, so this is a FAIL, not an exception and not a pass.
    if not _finite(relvol):
        return ConditionResult("T4", "FAIL",
                              f"relative volume is not finite ({relvol}) — baseline window "
                              f"unusable, volume cannot confirm")
    ok = relvol >= cfg.relative_volume_min
    return ConditionResult(
        "T4",
        "PASS" if ok else "FAIL",
        f"relative volume {relvol:.3f}x "
        f"{'>=' if ok else '<'} {cfg.relative_volume_min}x "
        f"({cfg.lookback_days}d baseline; intraday run-rate {cfg.intraday_run_rate_min}x)",
    )


def _t5(snap: IndicatorSnapshot, rules: Rulebook, arm: Arm) -> ConditionResult:
    cfg = rules.checklist.t5_options_chain
    # INTERPRETATION: an event day is checked FIRST and short-circuits T5. The brief
    # calls the event calendar a hard veto ("no earnings/FOMC day entries"), so it
    # outranks a healthy IV rank rather than being one AND-condition among several.
    # Earnings veto is not yet implemented (earnings_veto.implemented: false), so
    # is_event_day is expected to carry only fomc/cpi kinds until that is built.
    if snap.is_event_day:
        kinds = ",".join(snap.event_kinds) or "unspecified"
        return ConditionResult("T5", "FAIL",
                              f"EVENT DAY HARD VETO: {kinds} "
                              f"(veto_kinds={cfg.event_calendar.veto_kinds}, "
                              f"hard_veto={cfg.event_calendar.hard_veto})")
    if arm == "B":
        if snap.iv_rank is None:
            return ConditionResult("T5", "PENDING",
                                  f"iv_rank unavailable (needs {cfg.arm_b.iv_rank_lookback} "
                                  f"of IV history for the chosen strike)")
        ok = snap.iv_rank < cfg.arm_b.iv_rank_max
        return ConditionResult(
            "T5",
            "PASS" if ok else "FAIL",
            f"iv_rank {snap.iv_rank:.2f} "
            f"{'<' if ok else '>='} {cfg.arm_b.iv_rank_max} "
            f"(premium-buying, {cfg.arm_b.iv_rank_lookback}); no event day",
        )
    arm_c = cfg.arm_c
    if arm_c.calibration_pending:
        return ConditionResult("T5", "PENDING",
                              f"arm C IV band not calibrated ({arm_c.requirement}); "
                              f"calibration_pending=true, value=null — a pending gate "
                              f"cannot pass")
    return ConditionResult("T5", "PENDING",
                          "arm C calibrated band present but no strike-IV evaluation supplied")


def _t6(snap: IndicatorSnapshot, rules: Rulebook, arm: Arm) -> ConditionResult:
    cfg = rules.checklist.t6_flow
    if arm not in cfg.applies_to:
        return ConditionResult("T6", "SKIPPED",
                              f"T6 applies to {cfg.applies_to} only "
                              f"(brief: 'Arm B only, flow gate')",
                              blocking=False)
    mult = cfg.multiplier
    if mult.calibration_pending:
        return ConditionResult("T6", "PENDING",
                              f"flow multiplier not calibrated ({mult.reason}) — "
                              f"calibration_pending=true, value=null; T6 cannot pass "
                              f"before it is frozen before first trade")
    gate = snap.flow_gate
    if not gate.eod_confirmed:
        return ConditionResult("T6", "PENDING",
                              f"t6_flow.evaluation={cfg.evaluation}: this read is not an "
                              f"EOD-confirmed flow snapshot (status={gate.status})")
    if gate.status == "pending" or gate.ratio is None:
        return ConditionResult("T6", "PENDING",
                              f"flow data pending (status={gate.status}, "
                              f"{cfg.baseline_lookback_days}d baseline not ready)")
    if gate.status == "unknown":
        return ConditionResult("T6", "PENDING",
                              "flow data status=unknown — an unknown flow gate is not a pass")
    ratio = gate.ratio
    ok = gate.status == "pass" and ratio >= float(mult.value)
    return ConditionResult(
        "T6",
        "PASS" if ok else "FAIL",
        f"deep-OTM 0DTE {cfg.side} volume {ratio:.3f}x baseline "
        f"{'>=' if ok else '<'} {mult.value}x ({cfg.baseline_lookback_days}d), "
        f"status={gate.status}",
    )


# ---------------------------------------------------------------------------
# snapshot serialisation
# ---------------------------------------------------------------------------


def snapshot_indicators(snap: IndicatorSnapshot, direction: Direction, arm: Arm) -> dict:
    """Flatten the snapshot to a JSON-safe dict — the journal's indicator record.

    The brief requires ALL checklist values in the snapshot, so this deliberately
    includes the derived booleans (squeeze, expansion direction, cross age) and the
    flow-gate status, not just the numeric indicators.
    """
    bb = snap.bollinger
    out: dict[str, Any] = {
        "direction": direction,
        "arm": arm,
        "close": snap.close,
        "ema_fast": snap.ema_fast,
        "ema_slow": snap.ema_slow,
        "rsi": snap.rsi,
        "macd_line": snap.macd_line,
        "macd_signal": snap.macd_signal,
        "macd_hist": snap.macd_hist,
        "macd_hist_prev": snap.macd_hist_prev,
        "macd_cross_direction": snap.macd_cross_direction,
        "macd_cross_age_hours": snap.macd_cross_age_hours,
        "relvol": None if snap.relvol is None or not _finite(snap.relvol) else snap.relvol,
        "iv_rank": snap.iv_rank,
        "is_event_day": snap.is_event_day,
        "event_kinds": list(snap.event_kinds),
        "flow_gate": {
            "status": snap.flow_gate.status,
            "ratio": snap.flow_gate.ratio,
            "eod_confirmed": snap.flow_gate.eod_confirmed,
        },
        "bollinger": None
        if bb is None
        else {
            "upper": bb.upper,
            "middle": bb.middle,
            "lower": bb.lower,
            "low": bb.low,
            "high": bb.high,
            "bandwidth": bb.bandwidth,
            "bandwidth_percentile": bb.bandwidth_percentile,
            "bandwidth_expanding": bb.bandwidth_expanding,
            "middle_rising": bb.middle_rising,
            "squeeze": bb.squeeze,
        },
    }
    return out


# ---------------------------------------------------------------------------
# evaluator
# ---------------------------------------------------------------------------


def evaluate(
    snapshot: IndicatorSnapshot,
    rules: Rulebook,
    direction: Direction,
    arm: Arm,
) -> ChecklistResult:
    """Evaluate the arm's full checklist. ``fire`` is True only if every blocking
    condition PASSes.

    Order of evaluation is rulebook order (T1, T2, T2b, T3a, T3b, T4, T5, T6) so the
    first veto reason in the journal is the first thing that failed, which is what
    makes the NO-SHOT failure histogram readable.
    """
    if direction not in ("call", "put"):
        raise ChecklistInputError(f"direction must be 'call' or 'put', got {direction!r}")
    if arm not in ("B", "C"):
        raise ChecklistInputError(f"arm must be 'B' or 'C', got {arm!r}")
    snapshot.validate()

    conditions: dict[str, ConditionResult] = {
        "T1": _t1(snapshot, rules, direction),
        "T2": _t2(snapshot, rules, direction),
        "T2b": _t2b(snapshot, rules, direction),
        "T3a": _t3a(snapshot, rules, direction),
        "T3b": _t3b(snapshot, rules, direction),
        "T4": _t4(snapshot, rules),
        "T5": _t5(snapshot, rules, arm),
        "T6": _t6(snapshot, rules, arm),
    }

    # T3 group semantics: the rulebook (and the brief: "T3: Bollinger condition —
    # one of: (a) squeeze release, (b) band rejection") declares
    # ``satisfied_if_any_of: [squeeze_release, band_rejection]``. Evaluate T3a/T3b
    # as an OR group: if either PASSes the sibling becomes non-blocking context;
    # if neither PASSes the group produces ONE veto (carried by T3a, with T3b's
    # detail folded in) so the failure histogram counts T3 once, not twice.
    # FIX (2026-10-02, Moldy review of Phase 4a flag): previously both were
    # independently blocking, making T3 an AND — no series could ever fire.
    t3a, t3b = conditions["T3a"], conditions["T3b"]
    if t3a.status == "PASS" or t3b.status == "PASS":
        satisfied_by = "T3a squeeze-release" if t3a.status == "PASS" else "T3b band-rejection"
        if t3a.status != "PASS":
            conditions["T3a"] = dataclasses.replace(
                t3a, blocking=False,
                detail=f"{t3a.detail} | T3 group satisfied by {satisfied_by}")
        if t3b.status != "PASS":
            conditions["T3b"] = dataclasses.replace(
                t3b, blocking=False,
                detail=f"{t3b.detail} | T3 group satisfied by {satisfied_by}")
    else:
        conditions["T3a"] = dataclasses.replace(
            t3a,
            detail=f"{t3a.detail} | T3 group veto: neither arm satisfied — "
                   f"T3b also {t3b.status} ({t3b.detail})")
        conditions["T3b"] = dataclasses.replace(
            t3b, blocking=False,
            detail=f"{t3b.detail} | folded into T3 group veto (carried by T3a)")

    veto_reasons: list[str] = []
    for condition_id, result in conditions.items():
        if result.blocking and result.status != "PASS":
            prefix = {"FAIL": "veto", "PENDING": "pending", "SKIPPED": "skipped"}[result.status]
            veto_reasons.append(f"{condition_id}: {prefix} — {result.detail}")

    # Arm scoping is already encoded in each condition's ``blocking`` flag: a condition
    # the arm does not carry (T6 for C) reports SKIPPED + blocking=False, so it can
    # neither pass nor veto. ``fire`` is therefore simply "no blocking condition is
    # anything other than PASS" — FAIL and PENDING both stop the trade.
    fire = not any(
        result.blocking and result.status != "PASS" for result in conditions.values()
    )

    return ChecklistResult(
        arm=arm,
        direction=direction,
        conditions=conditions,
        veto_reasons=tuple(veto_reasons),
        indicators=snapshot_indicators(snapshot, direction, arm),
        fire=fire,
    )


__all__ = [
    "Arm",
    "BollingerState",
    "ChecklistInputError",
    "ChecklistResult",
    "ConditionResult",
    "Direction",
    "FlowGate",
    "IndicatorSnapshot",
    "Status",
    "evaluate",
    "snapshot_indicators",
]