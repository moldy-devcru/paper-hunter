"""Checklist tests — table-driven, one row per veto path.

Two rulebooks are used deliberately:

* ``load_example()`` — the shipped DRAFT rulebook, where **both** active arms are
  inert: arm B on ``t6_flow.multiplier`` (``calibration_pending: true``) and arm C on
  ``t5_options_chain.arm_c`` (the Phase-1b model types ``iv_rank_max`` as ``None``,
  so an arm-C IV band cannot even be written). Those two facts are pinned below,
  because "pending gate treated as a pass" is the single most likely thing to get
  quietly "fixed" later.
* a **calibrated** fixture rulebook (T6 multiplier filled in) — this is what the
  all-pass and per-condition blocking tables run against, so each row is an isolated
  block rather than one more failure riding on top of an uncalibrated gate.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable
from typing import Any

import pytest
import yaml

from config.loader import (
    DEFAULT_RULES_PATH,
    RulesError,
    load_example,
    load_rules_text,
)
from executor.checklist import (
    BollingerState,
    ChecklistInputError,
    FlowGate,
    IndicatorSnapshot,
    evaluate,
)


@pytest.fixture(scope="module")
def rules():
    """The shipped DRAFT rulebook (T6 uncalibrated, arm-C IV band uncalibrable)."""
    return load_example()


@pytest.fixture(scope="module")
def calibrated(rules):
    """Same rulebook with the T6 flow multiplier calibrated (value 2.0x).

    Everything else is untouched, so a difference in behaviour between ``rules`` and
    ``calibrated`` is attributable to the T6 calibration alone.
    """
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": 2.0,
        "calibration_pending": False,
        "reason": "test fixture: calibrated from a historical SPY deep-OTM 0DTE flow study",
    }
    return load_rules_text(yaml.safe_dump(data))





# ---------------------------------------------------------------------------
# snapshot fixtures
# ---------------------------------------------------------------------------


def _bb(**overrides: Any) -> BollingerState:
    base = BollingerState(
        upper=610.0,
        middle=602.5,
        lower=595.0,
        low=594.5,      # tags the lower band
        high=601.0,
        bandwidth=0.0249,
        bandwidth_percentile=10.0,   # < 20 -> squeeze
        bandwidth_expanding=True,
        middle_rising=True,          # expansion in the call direction
        squeeze=True,
    )
    return dataclasses.replace(base, **overrides)


def _snapshot(**overrides: Any) -> IndicatorSnapshot:
    """A fully-passing long-call snapshot; override any field per test."""
    base = IndicatorSnapshot(
        close=600.0,
        ema_fast=590.0,
        ema_slow=550.0,
        rsi=58.0,
        macd_line=1.2,
        macd_signal=1.0,
        macd_hist=0.2,
        macd_hist_prev=0.1,
        macd_cross_direction=None,
        macd_cross_age_hours=None,
        bollinger=_bb(),
        relvol=1.8,
        iv_rank=40.0,          # < 50 -> arm B T5 passes
        is_event_day=False,
        event_kinds=(),
        flow_gate=FlowGate(status="pass", ratio=2.4, eod_confirmed=True),
    )
    return dataclasses.replace(base, **overrides)


def _bear_snapshot(**overrides: Any) -> IndicatorSnapshot:
    """Full inversion of the above for a put."""
    base = IndicatorSnapshot(
        close=400.0,
        ema_fast=410.0,
        ema_slow=450.0,
        rsi=42.0,
        macd_line=-1.2,
        macd_signal=-1.0,
        macd_hist=-0.2,
        macd_hist_prev=-0.1,
        bollinger=_bb(
            upper=410.0,
            middle=402.5,
            lower=395.0,
            low=399.0,
            high=411.0,          # tags the upper band
            middle_rising=False,  # expansion in the put direction
        ),
        relvol=1.8,
        iv_rank=40.0,
        flow_gate=FlowGate(status="pass", ratio=2.4, eod_confirmed=True),
    )
    return dataclasses.replace(base, **overrides)


def evaluate_arm_b(snap: IndicatorSnapshot, rules, direction: str = "call"):
    return evaluate(snap, rules, direction=direction, arm="B")


# ---------------------------------------------------------------------------
# happy paths (calibrated rulebook)
# ---------------------------------------------------------------------------


def test_all_pass_bull_case_fires(calibrated):
    result = evaluate_arm_b(_snapshot(), calibrated)
    assert result.fire is True, result.veto_reasons
    assert result.veto_reasons == ()
    assert all(result.status(cid) == "PASS" for cid in ("T1", "T2", "T2b", "T4", "T5", "T6"))
    assert result.status("T3a") == "PASS"
    assert result.status("T3b") == "PASS"


def test_all_pass_bear_case_fires(calibrated):
    result = evaluate_arm_b(_bear_snapshot(), calibrated, direction="put")
    assert result.fire is True, result.veto_reasons


def test_result_records_every_indicator_for_the_journal(calibrated):
    result = evaluate_arm_b(_snapshot(), calibrated)
    indicators = result.indicators
    for key in ("close", "ema_fast", "ema_slow", "rsi", "macd_line", "macd_signal",
                "macd_hist", "macd_hist_prev", "relvol", "iv_rank", "is_event_day",
                "event_kinds", "flow_gate", "bollinger", "direction", "arm"):
        assert key in indicators, key
    assert indicators["bollinger"]["squeeze"] is True
    assert indicators["flow_gate"] == {"status": "pass", "ratio": 2.4, "eod_confirmed": True}
    assert result.to_dict()["fire"] is True


# ---------------------------------------------------------------------------
# uncalibrated gates: the shipped rulebook cannot fire either active arm
# ---------------------------------------------------------------------------


def test_t6_pending_blocks_arm_b_on_the_shipped_rulebook(rules):
    result = evaluate_arm_b(_snapshot(), rules)
    assert result.status("T6") == "PENDING"
    assert "not calibrated" in result.conditions["T6"].detail
    assert result.fire is False
    assert any(r.startswith("T6: pending") for r in result.veto_reasons)


def test_t6_never_passes_while_the_multiplier_is_uncalibrated(rules):
    # Even with a spectacular EOD-confirmed flow read, an uncalibrated multiplier
    # leaves T6 PENDING. A pending gate cannot pass.
    snap = _snapshot(flow_gate=FlowGate(status="pass", ratio=99.0, eod_confirmed=True))
    result = evaluate_arm_b(snap, rules)
    assert result.status("T6") == "PENDING"
    assert result.fire is False


def test_t6_requires_eod_confirmed_flow(rules):
    # t6_flow.evaluation: EOD_only. An intraday read cannot satisfy T6 even when the
    # multiplier is calibrated, because an intraday volume is not comparable to a
    # 20-day daily baseline.
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {"value": 2.0, "calibration_pending": False}
    calibrated_eod = load_rules_text(yaml.safe_dump(data))
    snap = _snapshot(flow_gate=FlowGate(status="pass", ratio=9.0, eod_confirmed=False))
    result = evaluate_arm_b(snap, calibrated_eod)
    assert result.status("T6") == "PENDING"
    assert "EOD_only" in result.conditions["T6"].detail
    assert result.fire is False


def test_t6_flow_ratio_below_calibrated_multiplier_fails(calibrated):
    snap = _snapshot(flow_gate=FlowGate(status="pass", ratio=1.2, eod_confirmed=True))
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T6") == "FAIL"
    assert result.fire is False


def test_t6_flow_at_calibrated_multiplier_passes(calibrated):
    snap = _snapshot(flow_gate=FlowGate(status="pass", ratio=2.0, eod_confirmed=True))
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T6") == "PASS"


def test_t6_pending_flow_data_does_not_pass(calibrated):
    snap = _snapshot(flow_gate=FlowGate(status="unknown", ratio=None, eod_confirmed=True))
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T6") == "PENDING"
    assert result.fire is False


def test_arm_c_t5_iv_band_is_pending_not_pass(rules):
    # Arm C carries T1/T2/T2b/T3a/T3b/T4/T5 only. With the arm-C IV band
    # uncalibrated, T5 evaluates to PENDING and blocks arm C.
    result = evaluate(_snapshot(), rules, direction="call", arm="C")
    assert result.status("T5") == "PENDING"
    assert "calibration_pending" in result.conditions["T5"].detail
    assert result.status("T6") == "SKIPPED"  # T6 is arm-B-only
    assert not any(r.startswith("T6") for r in result.veto_reasons)
    assert result.fire is False


def test_arm_c_cannot_fire_at_all_under_the_shipped_loader(rules):
    """Records a real constraint found while building this phase.

    ``config.loader.T5ArmC`` types ``iv_rank_max: None = None``, so an arm-C IV
    threshold cannot be expressed in the rulebook at all: the loader rejects it.
    Consequence: arm C is inert — not merely uncalibrated — until that model is
    widened (a rulebook/loader change, i.e. an operator decision, deliberately NOT
    made in this phase). Every arm-C condition other than T5 passes on this
    snapshot, which isolates T5 as the sole blocker.
    """
    result = evaluate(_snapshot(), rules, direction="call", arm="C")
    non_pass = [cid for cid, c in result.conditions.items()
                if c.blocking and c.status != "PASS"]
    assert non_pass == ["T5"]

    tampered = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    tampered["checklist"]["t5_options_chain"]["arm_c"] = {
        "requirement": "iv_on_chosen_strike_within_normal_band",
        "iv_rank_max": 60.0,
        "calibration_pending": False,
    }
    with pytest.raises(RulesError):
        load_rules_text(yaml.safe_dump(tampered))


def test_t6_is_skipped_for_arm_c_and_cannot_veto(rules):
    result = evaluate(_snapshot(), rules, direction="call", arm="C")
    assert result.status("T6") == "SKIPPED"
    assert result.conditions["T6"].blocking is False


# ---------------------------------------------------------------------------
# T1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"close": 585.0}, "close below EMA50"),
        ({"ema_fast": 545.0, "ema_slow": 550.0}, "EMA50 below EMA200 (not aligned)"),
        ({"ema_slow": 600.0}, "close below EMA200"),
    ],
)
def test_t1_failures(calibrated, overrides, why):
    result = evaluate_arm_b(_snapshot(**overrides), calibrated)
    assert result.fire is False
    assert result.status("T1") == "FAIL"
    assert any(r.startswith("T1: veto") for r in result.veto_reasons), why


def test_t1_pending_without_ema_history(calibrated):
    result = evaluate_arm_b(_snapshot(ema_fast=None, ema_slow=None), calibrated)
    assert result.status("T1") == "PENDING"
    assert result.fire is False


def test_t1_inverted_for_puts(calibrated):
    result = evaluate_arm_b(_bear_snapshot(), calibrated, direction="put")
    assert result.status("T1") == "PASS"
    # the same inverted structure read as a call must fail T1
    result = evaluate_arm_b(_bear_snapshot(), calibrated, direction="call")
    assert result.status("T1") == "FAIL"


# ---------------------------------------------------------------------------
# T2
# ---------------------------------------------------------------------------


def test_t2_regime_failure_on_the_bias_side(calibrated):
    result = evaluate_arm_b(_snapshot(rsi=48.0), calibrated)
    assert result.status("T2") == "FAIL"
    assert result.fire is False


@pytest.mark.parametrize("rsi", [75.1, 80.0, 99.0])
def test_t2_exhaustion_high_blocks(calibrated, rsi):
    result = evaluate_arm_b(_snapshot(rsi=rsi), calibrated)
    assert result.status("T2") == "FAIL"
    assert "EXHAUSTED" in result.conditions["T2"].detail
    assert result.fire is False


@pytest.mark.parametrize("rsi", [24.9, 10.0, 0.5])
def test_t2_exhaustion_low_blocks(calibrated, rsi):
    result = evaluate_arm_b(_snapshot(rsi=rsi), calibrated)
    assert result.status("T2") == "FAIL"
    assert "EXHAUSTED" in result.conditions["T2"].detail
    assert result.fire is False


def test_t2_exhaustion_boundary_is_strict(calibrated):
    # Brief: "> 75 / < 25 = no new entries". Exactly 75 is NOT exhausted.
    result = evaluate_arm_b(_snapshot(rsi=75.0), calibrated)
    assert result.status("T2") == "PASS"
    # Exactly 25 is not exhausted either, but it fails the CALL bias gate instead
    # (needs rsi > 50) — a different veto reason for a different reason.
    result = evaluate_arm_b(_snapshot(rsi=25.0), calibrated)
    assert result.status("T2") == "FAIL"
    assert "EXHAUSTED" not in result.conditions["T2"].detail
    # For a put, 25 is inside the bias band and inside the exhaustion band -> pass.
    snap = dataclasses.replace(_bear_snapshot(), rsi=25.0)
    result = evaluate_arm_b(snap, calibrated, direction="put")
    assert result.status("T2") == "PASS"


def test_t2_pending_without_rsi(calibrated):
    result = evaluate_arm_b(_snapshot(rsi=None), calibrated)
    assert result.status("T2") == "PENDING"
    assert result.fire is False


# ---------------------------------------------------------------------------
# T2b
# ---------------------------------------------------------------------------


def test_t2b_line_against_bias(calibrated):
    snap = _snapshot(macd_line=0.9, macd_signal=1.0, macd_hist=-0.1, macd_hist_prev=-0.2)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert result.fire is False


def test_t2b_contracting_histogram_is_a_veto(calibrated):
    # Line still above signal, but the histogram is shrinking -> momentum fading.
    snap = _snapshot(macd_line=1.2, macd_signal=1.0, macd_hist=0.05, macd_hist_prev=0.2)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert "CONTRACTING" in result.conditions["T2b"].detail
    assert result.fire is False


def test_t2b_put_histogram_must_be_falling(calibrated):
    # For puts, "expanding in trade direction" means the negative histogram is
    # getting MORE negative. -0.05 from -0.2 is contracting -> veto.
    snap = _bear_snapshot(macd_hist=-0.05, macd_hist_prev=-0.2)
    result = evaluate_arm_b(snap, calibrated, direction="put")
    assert result.status("T2b") == "FAIL"
    assert "CONTRACTING" in result.conditions["T2b"].detail


def test_t2b_flat_histogram_is_a_pass(calibrated):
    # INTERPRETATION: a flat histogram is neither expanding nor contracting, so it is
    # not vetoed — "contracting" is the only failing momentum state in the rulebook.
    result = evaluate_arm_b(_snapshot(macd_hist=0.2, macd_hist_prev=0.2), calibrated)
    assert result.status("T2b") == "PASS"
    assert result.fire is True


def test_t2b_missing_previous_histogram_is_pending(calibrated):
    result = evaluate_arm_b(_snapshot(macd_hist_prev=None), calibrated)
    assert result.status("T2b") == "PENDING"
    assert result.fire is False


def test_t2b_fresh_cross_against_bias_blocks(calibrated):
    # Cross down 3h ago (< 24h whipsaw guard), now trying to buy the recovery. No
    # squeeze-release, so the rulebook's single exception does not apply.
    snap = _snapshot(
        macd_cross_direction="put", macd_cross_age_hours=3.0, bollinger=_bb(squeeze=False)
    )
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert "WHIPSAW GUARD" in result.conditions["T2b"].detail
    assert result.fire is False


def test_t2b_fresh_cross_against_bias_allowed_with_squeeze_release(calibrated):
    # The rulebook's one exception (fresh_cross_guard.exception_if:
    # t3_squeeze_release_fires). The base snapshot is a squeeze release.
    snap = _snapshot(macd_cross_direction="put", macd_cross_age_hours=3.0)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T3a") == "PASS"  # the exception's precondition holds
    assert result.status("T2b") == "PASS"
    assert result.fire is True


def test_t2b_fresh_cross_without_squeeze_release_still_blocks(calibrated):
    snap = _snapshot(
        macd_cross_direction="put",
        macd_cross_age_hours=2.0,
        bollinger=_bb(squeeze=False),
    )
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert result.fire is False


def test_t2b_fresh_cross_at_the_24h_boundary_does_not_trip_the_guard(calibrated):
    # max_age_hours: 24 — "no entries against a fresh MACD cross < 1 day old".
    # Exactly 24h is not < 24h.
    snap = _snapshot(macd_cross_direction="put", macd_cross_age_hours=24.0)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "PASS"


def test_t2b_fresh_cross_in_trade_direction_is_fine(calibrated):
    snap = _snapshot(macd_cross_direction="call", macd_cross_age_hours=1.0)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "PASS"
    assert result.fire is True


# ---------------------------------------------------------------------------
# T3a / T3b reads (the individual logic — group semantics live further down)
# ---------------------------------------------------------------------------


def test_t3a_rejects_expansion_against_the_trade_direction(calibrated):
    # The T3 *read* fails, but T3b still passes, so the OR group is satisfied and the
    # failure is context, not a veto. Blocking behaviour needs BOTH arms to fail.
    result = evaluate_arm_b(_snapshot(bollinger=_bb(middle_rising=False)), calibrated)
    assert result.status("T3a") == "FAIL"
    assert "against trade direction" in result.conditions["T3a"].detail
    assert result.status("T3b") == "PASS"
    assert result.conditions["T3a"].blocking is False
    assert result.fire is True


def test_t3a_requires_bandwidth_expansion(calibrated):
    result = evaluate_arm_b(_snapshot(bollinger=_bb(bandwidth_expanding=False)), calibrated)
    assert result.status("T3a") == "FAIL"
    assert result.conditions["T3a"].blocking is False   # T3b carries the group
    assert result.fire is True


def test_t3a_no_squeeze(calibrated):
    result = evaluate_arm_b(_snapshot(bollinger=_bb(squeeze=False)), calibrated)
    assert result.status("T3a") == "FAIL"
    assert result.conditions["T3a"].blocking is False   # T3b carries the group
    assert result.fire is True


def test_t3b_band_rejection_requires_touch_and_close_back_inside(calibrated):
    # Touched the band but closed below it: no rejection. T3a still passes, so the
    # group is satisfied and the trade can fire.
    result = evaluate_arm_b(_snapshot(close=592.0, bollinger=_bb(low=588.0)), calibrated)
    assert result.status("T3b") == "FAIL"
    assert result.conditions["T3b"].blocking is False
    assert result.fire is True


def test_t3b_no_touch(calibrated):
    result = evaluate_arm_b(_snapshot(bollinger=_bb(low=596.0)), calibrated)
    assert result.status("T3b") == "FAIL"
    assert result.conditions["T3b"].blocking is False
    assert result.fire is True


def test_t3b_put_rejection_is_inverted(calibrated):
    result = evaluate_arm_b(_bear_snapshot(), calibrated, direction="put")
    assert result.status("T3b") == "PASS"
    # the put read evaluated as a call touches nothing on the call side
    result = evaluate_arm_b(_bear_snapshot(), calibrated, direction="call")
    assert result.status("T3b") == "FAIL"


def test_t3_pending_without_bollinger_state(calibrated):
    result = evaluate_arm_b(_snapshot(bollinger=None), calibrated)
    assert result.status("T3a") == "PENDING"
    assert result.status("T3b") == "PENDING"
    # Neither arm can satisfy the group, so T3a carries one group veto (as PENDING,
    # which blocks) and T3b is folded in rather than vetoing a second time.
    assert result.conditions["T3a"].blocking is True
    assert result.conditions["T3b"].blocking is False
    assert result.fire is False
    assert len([r for r in result.veto_reasons if r.startswith("T3")]) == 1


# ---------------------------------------------------------------------------
# T3 as an OR group: t3_bollinger.satisfied_if_any_of = [squeeze_release,
# band_rejection]. These are the semantics tests — the evaluator treats T3 as one
# gate, so each arm is tested by what it contributes to the group.
# ---------------------------------------------------------------------------


def test_t3_group_fires_when_only_t3a_passes(calibrated):
    result = evaluate_arm_b(_snapshot(bollinger=_bb(squeeze=False)), calibrated)
    assert result.status("T3a") == "FAIL"
    assert result.status("T3b") == "PASS"
    assert result.conditions["T3a"].blocking is False
    assert result.fire is True
    assert result.veto_reasons == ()
    # the losing arm's detail says the group is satisfied, and by which arm
    assert "T3 group satisfied by T3b band-rejection" in result.conditions["T3a"].detail


def test_t3_group_fires_when_only_t3b_passes(calibrated):
    result = evaluate_arm_b(_snapshot(bollinger=_bb(low=596.0)), calibrated)
    assert result.status("T3a") == "PASS"
    assert result.status("T3b") == "FAIL"
    assert result.conditions["T3b"].blocking is False
    assert result.fire is True
    assert result.veto_reasons == ()
    assert "T3 group satisfied by T3a squeeze-release" in result.conditions["T3b"].detail


def test_t3_group_vetoes_once_when_both_arms_fail(calibrated):
    result = evaluate_arm_b(_snapshot(bollinger=_bb(squeeze=False, low=596.0)), calibrated)
    assert result.status("T3a") == "FAIL"
    assert result.status("T3b") == "FAIL"
    assert result.fire is False
    # ONE group veto, carried by T3a; T3b is context folded into it.
    assert result.conditions["T3a"].blocking is True
    assert result.conditions["T3b"].blocking is False
    t3_vetoes = [r for r in result.veto_reasons if r.startswith("T3")]
    assert len(t3_vetoes) == 1, result.veto_reasons
    assert t3_vetoes[0].startswith("T3a:")
    assert "T3 group veto: neither arm satisfied" in t3_vetoes[0]
    # T3b's own read is preserved in T3a's detail so the histogram is still readable.
    assert "tagged=False" in t3_vetoes[0]
    assert "folded into T3 group veto" in result.conditions["T3b"].detail


def test_t3_group_veto_is_direction_agnostic_on_the_put_side(calibrated):
    # Put side: squeeze-release needs the middle band FALLING and no upper-band tag.
    bear_bb = _bb(upper=410.0, middle=402.5, lower=395.0, low=399.0, high=409.0,
                  middle_rising=False, squeeze=False)
    result = evaluate_arm_b(
        _bear_snapshot(bollinger=bear_bb), calibrated, direction="put"
    )
    assert result.status("T3a") == "FAIL"
    assert result.status("T3b") == "FAIL"
    assert result.fire is False
    t3_vetoes = [r for r in result.veto_reasons if r.startswith("T3")]
    assert len(t3_vetoes) == 1, result.veto_reasons


def test_t3_group_fires_on_the_put_side_with_only_t3a(calibrated):
    # T3b's put read needs an upper-band tag; there is none, but squeeze-release holds.
    bear_bb = _bb(upper=410.0, middle=402.5, lower=395.0, low=399.0, high=409.0,
                  middle_rising=False)
    result = evaluate_arm_b(_bear_snapshot(bollinger=bear_bb), calibrated, direction="put")
    assert result.status("T3a") == "PASS"
    assert result.status("T3b") == "FAIL"
    assert result.conditions["T3b"].blocking is False
    assert result.fire is True


# ---------------------------------------------------------------------------
# T4
# ---------------------------------------------------------------------------


def test_t4_below_threshold(calibrated):
    result = evaluate_arm_b(_snapshot(relvol=1.49), calibrated)
    assert result.status("T4") == "FAIL"
    assert result.fire is False


def test_t4_exactly_at_threshold_passes(calibrated):
    result = evaluate_arm_b(_snapshot(relvol=1.5), calibrated)
    assert result.status("T4") == "PASS"
    assert result.fire is True


def test_t4_zero_baseline_is_a_fail_not_an_exception(calibrated):
    result = evaluate_arm_b(_snapshot(relvol=math.inf), calibrated)
    assert result.status("T4") == "FAIL"
    assert result.fire is False


def test_t4_pending_without_volume_data(calibrated):
    result = evaluate_arm_b(_snapshot(relvol=None), calibrated)
    assert result.status("T4") == "PENDING"
    assert result.fire is False


# ---------------------------------------------------------------------------
# T5
# ---------------------------------------------------------------------------


def test_t5_event_day_is_a_hard_veto(calibrated):
    snap = _snapshot(is_event_day=True, event_kinds=("fomc",))
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T5") == "FAIL"
    assert "HARD VETO" in result.conditions["T5"].detail
    assert result.fire is False


def test_t5_event_veto_outranks_a_good_iv_rank(calibrated):
    snap = _snapshot(is_event_day=True, event_kinds=("cpi",), iv_rank=10.0)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T5") == "FAIL"
    assert "HARD VETO" in result.conditions["T5"].detail


def test_t5_arm_b_iv_rank_gate(calibrated):
    assert evaluate_arm_b(_snapshot(iv_rank=49.9), calibrated).status("T5") == "PASS"
    result = evaluate_arm_b(_snapshot(iv_rank=50.0), calibrated)
    assert result.status("T5") == "FAIL"
    assert result.fire is False


def test_t5_pending_without_iv_rank(calibrated):
    result = evaluate_arm_b(_snapshot(iv_rank=None), calibrated)
    assert result.status("T5") == "PENDING"
    assert result.fire is False


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


def test_invalid_direction_and_arm(calibrated):
    with pytest.raises(ChecklistInputError):
        evaluate(_snapshot(), calibrated, direction="sideways", arm="C")  # type: ignore[arg-type]
    with pytest.raises(ChecklistInputError):
        evaluate(_snapshot(), calibrated, direction="call", arm="A")  # type: ignore[arg-type]


def test_nan_close_rejected(calibrated):
    with pytest.raises(ChecklistInputError):
        evaluate(_snapshot(close=math.nan), calibrated, direction="call", arm="B")


# ---------------------------------------------------------------------------
# table-driven: every blocking condition can individually stop the trade.
# Arm B on the calibrated rulebook, so each row is the ONLY reason the trade did not
# fire (asserted below).
# ---------------------------------------------------------------------------

Mutator = Callable[[IndicatorSnapshot], IndicatorSnapshot]


def _mutate(**kwargs: Any) -> Mutator:
    def apply(snap: IndicatorSnapshot) -> IndicatorSnapshot:
        return dataclasses.replace(snap, **kwargs)

    return apply


CASES: list[tuple[str, Mutator, int]] = [
    # (condition id, mutator, expected number of vetoes)
    ("T1", _mutate(ema_fast=605.0), 1),          # close 600 < EMA50 605
    ("T2", _mutate(rsi=49.0), 1),
    ("T2", _mutate(rsi=90.0), 1),
    ("T2", _mutate(rsi=20.0), 1),
    ("T2b", _mutate(macd_line=0.1, macd_signal=1.0), 1),
    ("T2b", _mutate(macd_hist=0.0, macd_hist_prev=0.4), 1),
    # The whipsaw guard fires only when T3a squeeze-release has NOT fired, and
    # squeeze-release is exactly T3a's pass condition — so this row necessarily
    # vetoes twice. `low=596.0` breaks T3b too, so the T3 group genuinely fails and
    # carries its own veto; the coupling is the rulebook's own design, not a test
    # artifact. (Under OR semantics a squeeze-only break would NOT add a second
    # veto: T3b would still pass and carry the group.)
    ("T2b", _mutate(macd_cross_direction="put", macd_cross_age_hours=1.0,
                    bollinger=_bb(squeeze=False, low=596.0)), 2),
    # T3 is one OR gate, so the blocking row is the GROUP: both arms failing is the
    # only way T3 can veto, and it vetoes exactly once.
    ("T3", _mutate(bollinger=_bb(squeeze=False, low=596.0)), 1),
    ("T4", _mutate(relvol=0.9), 1),
    ("T5", _mutate(is_event_day=True, event_kinds=("fomc",)), 1),
    ("T5", _mutate(iv_rank=80.0), 1),
    ("T6", _mutate(flow_gate=FlowGate(status="pass", ratio=0.5, eod_confirmed=True)), 1),
]


@pytest.mark.parametrize(
    ("condition_id", "mutate", "expected_vetoes"),
    CASES,
    ids=[f"{cid}-{i}" for i, (cid, _m, _n) in enumerate(CASES)],
)
def test_each_condition_individually_blocks(calibrated, condition_id, mutate, expected_vetoes):
    result = evaluate_arm_b(mutate(_snapshot()), calibrated)
    assert result.fire is False, f"{condition_id} should have blocked"
    if condition_id == "T3":
        # The group veto is carried by T3a; the row must not also veto T3b.
        assert result.status("T3a") == "FAIL"
        assert result.conditions["T3a"].blocking is True
        assert result.conditions["T3b"].blocking is False
        assert any("T3 group veto" in r for r in result.veto_reasons)
        first_veto_id = "T3a"
    else:
        assert result.status(condition_id) != "PASS"
        first_veto_id = condition_id
    # the condition under test is the FIRST veto listed (rulebook evaluation order),
    # and nothing else fails beyond the expected coupled conditions.
    assert result.veto_reasons[0].startswith(f"{first_veto_id}:")
    assert len(result.veto_reasons) == expected_vetoes, result.veto_reasons