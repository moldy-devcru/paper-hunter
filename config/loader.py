"""Strict loader/validator for the frozen rulebook (Phase 1b).

Two jobs, both load-bearing:

1. **Validate.** The rulebook is frozen before the first trade. A typo in a veto
   threshold is not a crash, it is an experiment that quietly measures the wrong
   thing — so every model sets ``extra="forbid"`` and unknown keys are hard errors.
   Likewise, any value the brief left TBD must carry ``calibration_pending: true``
   with a null value; the loader refuses a TBD value that *looks* frozen.

2. **Stamp an identity.** ``strategy_version`` recorded on every journal entry is
   ``<version>+<sha256[:12] of the file bytes>``. Two different rulebooks can share
   a version string if someone edited one without bumping; the content hash cannot
   lie about that, so the journal can always answer "which rules produced this trade".

Python 3.12+, pydantic v2 + pyyaml.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = Path(__file__).with_name("rules.example.yaml")

# Thresholds frozen from docs/brief.md. Used by tests as the single source of truth
# for "the example file still says what the brief says".
FROZEN = {
    "ema_fast": 50,
    "ema_slow": 200,
    "rsi_period": 14,
    "rsi_exhaustion_high": 75,
    "rsi_exhaustion_low": 25,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "macd_fresh_cross_hours": 24,
    "bb_percentile": 20,
    "bb_lookback_days": 60,
    "relvol_min": 1.5,
    "relvol_lookback_days": 20,
    "iv_rank_max": 50,
    "t6_baseline_lookback_days": 20,
    "arm_entry_open_et": "09:45",
    "arm_entry_close_et": "14:00",
    "arm_b_premium_pct": 0.02,
    "arm_b_premium_usd_cap": 200,
    "arm_b_time_stop_minutes": 120,
    "arm_b_time_stop_gain_pct": 0.30,
    "arm_b_profit_take_pct": 0.50,
    "arm_b_profit_take_gain_pct": 1.00,
    "arm_b_hard_close_et": "15:30",
    "arm_b_max_concurrent": 1,
    "arm_b_max_entries_per_week": 3,
    "arm_c_dte_min": 90,
    "arm_c_dte_max": 180,
    "arm_c_delta_min": 0.80,
    "arm_c_premium_pct": 0.50,
    "arm_c_roll_delta": 0.70,
    "arm_c_roll_dte": 45,
    "arm_c_max_concurrent": 1,
    "bankroll_usd": 10000,
}

STATUS_VALUES = Literal["DRAFT", "FROZEN"]


class FrozenModel(BaseModel):
    """Base for every rulebook section: unknown keys are errors, not defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Pending(FrozenModel):
    """A value the brief left open.

    Either it is calibrated (``value`` set, ``calibration_pending`` False) or it is
    explicitly marked pending with a null value. A pending item carrying a real
    number is a fabricated threshold and is rejected — that is the whole point.
    """

    value: float | str | bool | None = None
    calibration_pending: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def _pending_is_honest(self) -> Pending:
        if self.calibration_pending and self.value is not None:
            raise ValueError(
                "calibration_pending: true must carry value: null — a pending value with a "
                "number in it is an invented threshold (brief: 'N to be calibrated')"
            )
        if not self.calibration_pending and self.value is None:
            raise ValueError("value is null but not marked calibration_pending")
        return self


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------


class Strategy(FrozenModel):
    version: str
    status: STATUS_VALUES = "DRAFT"
    symbol: str = "SPY"
    timezone: str = "America/New_York"
    brief: str = "docs/brief.md"


# ---------------------------------------------------------------------------
# arms
# ---------------------------------------------------------------------------


class WindowET(FrozenModel):
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def _ordered(self) -> WindowET:
        if self.start >= self.end:
            raise ValueError(f"window start {self.start} must precede end {self.end}")
        return self


class ArmAEntry(FrozenModel):
    when: Literal["first_market_open_of_window"]
    instrument: Literal["equity"]
    symbol: str


class ArmA(FrozenModel):
    name: Literal["control"]
    description: str
    bankroll_usd: float
    trading_days: int = 1
    entry: ArmAEntry
    managed: bool = False
    rules: list[str] = Field(default_factory=list)


class ArmBEntry(FrozenModel):
    window_et: WindowET
    instrument: Literal["option"]
    right: str
    dte: Literal[0]
    moneyness: Literal["OTM"]
    strike_rule: str
    premium_pct_of_bankroll_max: float = Field(gt=0, le=1)
    premium_usd_hard_cap: float = Field(gt=0)

    @model_validator(mode="after")
    def _cap_consistent_with_pct(self) -> ArmBEntry:
        implied = self.premium_pct_of_bankroll_max * FROZEN["bankroll_usd"]
        if implied != self.premium_usd_hard_cap:
            raise ValueError(
                f"premium cap {self.premium_usd_hard_cap} != "
                f"{self.premium_pct_of_bankroll_max} x "
                f"${FROZEN['bankroll_usd']:,.0f} bankroll (= ${implied:,.2f})"
            )
        return self


class TimeStop(FrozenModel):
    after_entry_minutes: int = Field(gt=0)
    if_under_gain_pct: float = Field(ge=0)
    action: Literal["close_at_market"]


class ProfitTake(FrozenModel):
    close_pct_at_100pct_gain: float = Field(gt=0, lt=1)
    trail_remainder: bool = True


class ArmBExits(FrozenModel):
    time_stop: TimeStop
    profit_take: ProfitTake
    hard_close_et: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    hold_to_expiry: Literal[False]


class ArmBLimits(FrozenModel):
    max_concurrent_positions: int = Field(ge=1)
    max_entries_per_week: int = Field(ge=1)


class ArmB(FrozenModel):
    name: Literal["gambler"]
    description: str
    bankroll_usd: float
    checklist: list[str]
    entry: ArmBEntry
    exits: ArmBExits
    limits: ArmBLimits
    rules: list[str] = Field(default_factory=list)


class DTERange(FrozenModel):
    min: int = Field(gt=0)
    max: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> DTERange:
        if self.min >= self.max:
            raise ValueError(f"DTE min {self.min} must be below max {self.max}")
        return self


class ArmCEntry(FrozenModel):
    instrument: Literal["option"]
    right: Literal["call"]
    dte: DTERange
    delta_min: float = Field(ge=0, le=1)
    premium_pct_of_bankroll_max: float = Field(gt=0, le=1)


class ArmCRoll(FrozenModel):
    trigger: RollTrigger
    to: str
    discretionary: Literal[False]


class ArmCRollExit(FrozenModel):
    trigger: str
    condition: str
    action: str
    then: str


class ArmCExits(FrozenModel):
    roll: ArmCRoll
    exit_all: ArmCRollExit


class ArmCLimits(FrozenModel):
    max_concurrent_positions: int = Field(ge=1)


class ArmC(FrozenModel):
    name: Literal["stalker"]
    description: str
    bankroll_usd: float
    checklist: list[str]
    entry: ArmCEntry
    exits: ArmCExits
    limits: ArmCLimits
    rules: list[str] = Field(default_factory=list)


class RollTrigger(FrozenModel):
    delta_below: float = Field(ge=0, le=1)
    dte_below: int = Field(ge=0)
    rule: Literal["whichever_first"]


class Arms(FrozenModel):
    A: ArmA
    B: ArmB
    C: ArmC


# ---------------------------------------------------------------------------
# checklist
# ---------------------------------------------------------------------------


class T1CounterTrend(FrozenModel):
    allowed: bool
    requires_all_of: list[str]
    requires_documented_reversal_trigger: bool

    @model_validator(mode="after")
    def _counter_trend_is_earned(self) -> T1CounterTrend:
        """The brief: counter-trend entries need T2+T3+T4+T5 ALL plus a documented
        reversal trigger. Enabling counter-trend without those guardrails is a
        rulebook that contradicts itself, so it fails validation."""
        if self.allowed:
            missing = {"T2", "T3", "T4", "T5"} - set(self.requires_all_of)
            if missing:
                raise ValueError(
                    "counter_trend.allowed=true requires all of T2,T3,T4,T5; "
                    f"missing {sorted(missing)}"
                )
            if not self.requires_documented_reversal_trigger:
                raise ValueError(
                    "counter_trend.allowed=true requires a documented reversal trigger "
                    "(brief: 'counter-trend trades require T2+T3+T4+T5 ALL plus a documented "
                    "reversal trigger')"
                )
        return self


class T1(FrozenModel):
    id: Literal["T1"]
    applies_to: list[Literal["B", "C"]]
    ema_fast: int
    ema_slow: int
    call_bias: str
    put_bias: str
    counter_trend: T1CounterTrend


class Exhaustion(FrozenModel):
    high: float = Field(gt=50)
    low: float = Field(lt=50)
    action: Literal["block_new_entries"]

    @model_validator(mode="after")
    def _ordered(self) -> Exhaustion:
        if self.low >= self.high:
            raise ValueError(f"exhaustion low {self.low} must be below high {self.high}")
        return self


class T2(FrozenModel):
    id: Literal["T2"]
    applies_to: list[Literal["B", "C"]]
    period: int
    call_bias_min: float
    put_bias_max: float
    exhaustion: Exhaustion


class Histogram(FrozenModel):
    expanding_in_trade_direction: Literal["preferred"]
    contracting: Literal["veto"]


class FreshCrossGuard(FrozenModel):
    max_age_hours: int = Field(gt=0)
    exception_if: str


class T2b(FrozenModel):
    id: Literal["T2b"]
    applies_to: list[Literal["B", "C"]]
    fast: int
    slow: int
    signal: int
    line_vs_signal: str
    histogram: Histogram
    fresh_cross_guard: FreshCrossGuard

    @model_validator(mode="after")
    def _ordered(self) -> T2b:
        if self.fast >= self.slow:
            raise ValueError(f"MACD fast {self.fast} must be below slow {self.slow}")
        return self


class SqueezeRelease(FrozenModel):
    bandwidth_below_percentile: float = Field(ge=0, le=100)
    lookback_days: int = Field(gt=0)
    then: str


class BandRejection(FrozenModel):
    calls: str
    puts: str


class T3AnyOf(FrozenModel):
    squeeze_release: SqueezeRelease
    band_rejection: BandRejection


class T3(FrozenModel):
    id: Literal["T3"]
    applies_to: list[Literal["B", "C"]]
    satisfied_if_any_of: T3AnyOf


class T4(FrozenModel):
    id: Literal["T4"]
    applies_to: list[Literal["B", "C"]]
    relative_volume_min: float = Field(gt=0)
    lookback_days: int = Field(gt=0)
    intraday_run_rate_min: float = Field(gt=0)


class T5ArmB(FrozenModel):
    iv_rank_max: float = Field(ge=0, le=100)
    iv_rank_lookback: str


class T5ArmC(FrozenModel):
    requirement: str
    iv_rank_max: None = None
    calibration_pending: Literal[True]


class EarningsVeto(FrozenModel):
    required: bool
    implemented: bool
    calibration_pending: bool


class EventCalendar(FrozenModel):
    checked: Literal["daily"]
    hard_veto: Literal[True]
    calendar_dir: str
    veto_kinds: list[str]
    earnings_veto: EarningsVeto


class T5(FrozenModel):
    id: Literal["T5"]
    applies_to: list[Literal["B", "C"]]
    purpose: str
    arm_b: T5ArmB
    arm_c: T5ArmC
    event_calendar: EventCalendar
    notes: list[str] = Field(default_factory=list)


class T6(FrozenModel):
    id: Literal["T6"]
    applies_to: list[Literal["B"]]
    source: str
    side: str
    baseline_lookback_days: int = Field(gt=0)
    multiplier: Pending
    deep_otm: DeepOtm
    evaluation: Literal["EOD_only"]
    purpose: str
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _multiplier_is_pending_or_set(self) -> T6:
        if not self.multiplier.calibration_pending and not isinstance(
            self.multiplier.value, (int, float)
        ):
            raise ValueError("a calibrated T6 multiplier must be numeric")
        return self


class DeepOtm(FrozenModel):
    """What "deep OTM" means, in numbers the soak can execute.

    The brief says "deep-OTM 0DTE volume" without defining deep. Something has to
    decide it before the window opens or the T6 baseline is not a fixed quantity, so
    it lives here where a change is a versioned commit.

    # INTERPRETATION: moneyness is a **percentage distance from spot**, with separate
    thresholds per side, because calls and puts do not populate symmetrically around
    spot on SPY and one shared number would silently call a crowded strike "deep" on
    one side only. Defaults are 3% calls / 3% puts — the research note's own example
    ("filter to deep-OTM (e.g. >=3% OTM)", 2026-10-02-flow-data-market.md §5.3).
    They are marked calibration_pending because the note's "e.g." is an illustration,
    not a measurement, and the operator freezes them at ratification.
    """

    measure: Literal["pct_distance_from_spot"]
    calls: Pending
    puts: Pending
    bucket_size_pct: float = Field(gt=0, le=5)
    applies_to_expiry: Literal["zero_dte"]
    volume_source: Literal["daily_bar_volume"]


class Checklist(FrozenModel):
    t1_ema_alignment: T1
    t2_rsi: T2
    t2b_macd: T2b
    t3_bollinger: T3
    t4_volume: T4
    t5_options_chain: T5
    t6_flow: T6

    def for_arm(self, arm: str) -> list[str]:
        """Checklist condition ids that apply to an arm, in rulebook order."""
        sections = (
            self.t1_ema_alignment,
            self.t2_rsi,
            self.t2b_macd,
            self.t3_bollinger,
            self.t4_volume,
            self.t5_options_chain,
            self.t6_flow,
        )
        return [s.id for s in sections if arm in s.applies_to]


# ---------------------------------------------------------------------------
# cadence / execution / etc
# ---------------------------------------------------------------------------


class CadenceStep(FrozenModel):
    id: str
    does: list[str]
    forbids: list[str] | None = None
    output: str | None = None
    at_et: str | None = None
    window_et: list[str] | None = None


class Execution(FrozenModel):
    mode: Literal["paper_only"]
    executor: str
    total_bankroll_usd: float
    arms_bankroll_usd_each: float


class Noshot(FrozenModel):
    required_fields: list[str]
    review: str


class RuleChanges(FrozenModel):
    allowed_only_at: str
    requires_versioned_commit: bool
    forbidden_mid_position: bool
    journal_seam_entry: bool


class CatalystClause(FrozenModel):
    in_robot_path: Literal[False]
    exception_path: str
    pnl_tracked_separately: bool
    intake: str
    envelope_vs_content: str


class Rulebook(FrozenModel):
    """The whole frozen rulebook. ``config_sha256``/``strategy_version`` are derived."""

    strategy: Strategy
    arms: Arms
    checklist: Checklist
    cadence: list[CadenceStep]
    execution: Execution
    noshot: Noshot
    rule_changes: RuleChanges
    catalyst_clause: CatalystClause
    config_sha256: str = ""
    strategy_version: str = ""

    # -- invariants the brief states in prose but no single field can hold --------

    @model_validator(mode="after")
    def _self_consistent(self) -> Rulebook:
        bankrolls = {a: getattr(self.arms, a).bankroll_usd for a in "ABC"}
        if len(set(bankrolls.values())) != 1:
            raise ValueError(f"all three arms share one bankroll, got {bankrolls}")
        if self.execution.arms_bankroll_usd_each != bankrolls["A"]:
            raise ValueError("execution.arms_bankroll_usd_each disagrees with the arms")
        if self.execution.total_bankroll_usd != bankrolls["A"] * 3:
            raise ValueError("execution.total_bankroll_usd != 3 x arm bankroll")

        # an arm's checklist list must match the conditions that declare it
        for arm in ("B", "C"):
            declared = getattr(self.arms, arm).checklist
            actual = self.checklist.for_arm(arm)
            if sorted(declared) != sorted(actual):
                raise ValueError(
                    f"arm {arm} checklist {declared} != applicable conditions {actual}"
                )
        return self

    def fingerprint(self, source: str | bytes) -> None:
        """Stamp ``config_sha256`` + ``strategy_version`` from the raw file bytes.

        Hashing the bytes (not the parsed model) is the point: it catches comments,
        key reordering and anything else that changes the file, so two journals with
        the same hash really did run the same file.
        """
        raw = source.encode("utf-8") if isinstance(source, str) else source
        digest = hashlib.sha256(raw).hexdigest()
        object.__setattr__(self, "config_sha256", digest)
        object.__setattr__(self, "strategy_version", f"{self.strategy.version}+{digest[:12]}")

    @property
    def pending_calibrations(self) -> list[str]:
        """Human-readable list of every value still marked calibration_pending."""
        t6 = self.checklist.t6_flow
        pending = [f"checklist.t6_flow.multiplier (evaluation={t6.evaluation})"]
        for side in ("calls", "puts"):
            leg = getattr(t6.deep_otm, side)
            if leg.calibration_pending:
                pending.append(f"checklist.t6_flow.deep_otm.{side}")
        if self.checklist.t5_options_chain.arm_c.calibration_pending:
            pending.append("checklist.t5_options_chain.arm_c.iv_rank_max")
        if self.checklist.t5_options_chain.event_calendar.earnings_veto.calibration_pending:
            pending.append("checklist.t5_options_chain.event_calendar.earnings_veto")
        return pending


class RulesError(ValueError):
    """Raised when a rulebook file fails validation. Message names the offending keys."""


def load_rules(path: str | Path | None = None) -> Rulebook:
    """Load + validate a rulebook file. Raises ``RulesError`` (or ``FileNotFoundError``)."""
    rules_path = Path(path) if path is not None else DEFAULT_RULES_PATH
    raw = rules_path.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except Exception as exc:  # yaml raises YAMLError or plain ValueError, depending
        raise RulesError(f"{rules_path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RulesError(f"{rules_path}: top level must be a mapping, got {type(data).__name__}")
    try:
        rules = Rulebook.model_validate(data)
    except Exception as exc:  # pydantic ValidationError and our own invariants
        raise RulesError(f"{rules_path} is not a valid frozen rulebook:\n{exc}") from exc
    rules.fingerprint(raw)
    return rules


def load_rules_text(text: str) -> Rulebook:
    """Validate an in-memory rulebook (used by tests for the tamper cases)."""
    try:
        data = yaml.safe_load(text)
        rules = Rulebook.model_validate(data)
    except Exception as exc:
        raise RulesError(f"invalid rulebook:\n{exc}") from exc
    rules.fingerprint(text)
    return rules


def load_example() -> Rulebook:
    """Convenience for tests/notebooks: the repo's own example rulebook."""
    return load_rules(DEFAULT_RULES_PATH)


__all__ = [
    "DEFAULT_RULES_PATH",
    "FROZEN",
    "REPO_ROOT",
    "DeepOtm",
    "Rulebook",
    "RulesError",
    "load_example",
    "load_rules",
    "load_rules_text",
]

ArmCRoll.model_rebuild()
