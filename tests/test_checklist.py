"""Checklist tests — table-driven, one row per veto path.

Two rulebooks are used deliberately:

* ``load_example()`` — the shipped v2 FROZEN rulebook (prereg-v2, ratified
  2026-11-18). T6's multiplier is frozen per side (call 5.59 / put 3.07, §3), the
  blocking set for arms B/C is T4 AND (T2b OR T3-group) (§1), and T1/T2/T5 are
  non-blocking observations. Both facts are pinned below, because "a gate that
  stops gating" is as much a failure mode as "pending treated as a pass".
* a **calibrated** fixture rulebook (both per-side T6 legs filled with N=2.0) —
  this is what the all-pass and per-condition blocking tables run against, so each
  row is an isolated block rather than one more failure riding on top of the frozen
  5.59 call leg.
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
    ChecklistResult,
    FlowGate,
    IndicatorSnapshot,
    evaluate,
)


@pytest.fixture(scope="module")
def rules():
    """The shipped v2 FROZEN rulebook (T6 frozen per side, prereg-v2 §3)."""
    return load_example()


@pytest.fixture(scope="module")
def calibrated(rules):
    """Same rulebook with the T6 flow multiplier calibrated (value 2.0x).

    Everything else is untouched, so a difference in behaviour between ``rules`` and
    ``calibrated`` is attributable to the T6 calibration alone.
    """
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    # prereg-v2 §3: per-side schema — the fixture fills both legs with the same N.
    data["checklist"]["t6_flow"]["multiplier"] = {
        "call": {
            "value": 2.0,
            "calibration_pending": False,
            "reason": "test fixture: calibrated from a historical SPY deep-OTM 0DTE flow study",
        },
        "put": {
            "value": 2.0,
            "calibration_pending": False,
            "reason": "test fixture: calibrated from a historical SPY deep-OTM 0DTE flow study",
        },
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
# T6 on the shipped v2 rulebook: frozen per side (prereg-v2 §3)
# ---------------------------------------------------------------------------


def test_t6_blocks_arm_b_when_flow_is_below_the_frozen_call_n(rules):
    # prereg-v2 §3: N is frozen per side (call 5.59 / put 3.07). The base snapshot's
    # 2.4x flow read is below the call N, so arm B is still gated on the shipped
    # rulebook — the gate stopped being PENDING, not gone.
    result = evaluate_arm_b(_snapshot(), rules)
    assert result.status("T6") == "FAIL"
    assert result.fire is False
    assert any(r.startswith("T6: veto") for r in result.veto_reasons)


def test_t6_uses_the_leg_of_the_trade_direction(rules):
    # prereg-v2 §3: per-side N (call 5.59 / put 3.07). A 4.0x read clears the put
    # leg but not the call leg — the gate must compare each side against its own
    # frozen N, not one pooled number.
    snap = _snapshot(flow_gate=FlowGate(status="pass", ratio=4.0, eod_confirmed=True))
    assert evaluate_arm_b(snap, rules, direction="put").status("T6") == "PASS"
    assert evaluate_arm_b(snap, rules, direction="call").status("T6") == "FAIL"


def test_t6_requires_eod_confirmed_flow(rules):
    # t6_flow.evaluation: EOD_only. An intraday read cannot satisfy T6 even when the
    # multiplier is calibrated, because an intraday volume is not comparable to a
    # 20-day daily baseline.
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {
        "call": {"value": 2.0, "calibration_pending": False},
        "put": {"value": 2.0, "calibration_pending": False},
    }
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


def test_arm_c_t5_is_an_iv_rank_ceiling_not_a_pending_band(rules):
    # Operator ruling 2026-10-02: arm C carries the same rank ceiling as arm B (< 50).
    # prereg-v2 §1: the ceiling is still computed and journaled (the IV series keeps
    # accumulating), but it no longer vetoes — high IV rank is an observation now.
    result = evaluate(_snapshot(), rules, direction="call", arm="C")
    assert result.status("T5") == "PASS"          # snapshot ships iv_rank=40

    hot = evaluate(_snapshot(iv_rank=60.0), rules, direction="call", arm="C")
    assert hot.status("T5") == "FAIL"
    assert hot.conditions["T5"].blocking is False
    assert hot.fire is True

    unknown = evaluate(_snapshot(iv_rank=None), rules, direction="call", arm="C")
    assert unknown.status("T5") == "PENDING"      # no IV history yet, still not a pass
    assert unknown.fire is True

    assert result.status("T6") == "SKIPPED"  # T6 is arm-B-only
    assert not any(r.startswith("T6") for r in result.veto_reasons)


def test_arm_c_can_fire_under_the_shipped_loader(rules):
    """The inverse of the Phase-1b constraint, and it is deliberate.

    ``config.loader.T5ArmC`` used to type ``iv_rank_max: None``, so an arm-C IV
    threshold could not be expressed at all and arm C was *inert* rather than merely
    uncalibrated. The 2026-10-02 ruling widened the field (same ceiling as arm B), so
    on this snapshot every arm-C condition passes and arm C is T5-satisfiable.
    """
    result = evaluate(_snapshot(), rules, direction="call", arm="C")
    non_pass = [cid for cid, c in result.conditions.items()
                if c.blocking and c.status != "PASS"]
    assert non_pass == []
    assert result.fire is True


def test_arm_c_iv_ceiling_still_rejects_a_smuggled_number_while_pending():
    """Pending stays honest: a number written under ``calibration_pending: true`` is
    still an invented threshold, and the loader still says so."""
    tampered = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    tampered["checklist"]["t5_options_chain"]["arm_c"] = {
        "requirement": "iv_on_chosen_strike_within_normal_band",
        "iv_rank_max": 60.0,
        "calibration_pending": True,
    }
    with pytest.raises(RulesError):
        load_rules_text(yaml.safe_dump(tampered))

    # ...and null without the pending flag is rejected too.
    tampered["checklist"]["t5_options_chain"]["arm_c"]["iv_rank_max"] = None
    tampered["checklist"]["t5_options_chain"]["arm_c"]["calibration_pending"] = False
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
def test_t1_failures_are_recorded_not_blocking(calibrated, overrides, why):
    # prereg-v2 §1: T1 is out of the blocking AND — a broken with-trend structure is
    # a journaled observation, not a veto (counter-trend entries are now explicitly
    # permitted, §1 "Declared consequence").
    result = evaluate_arm_b(_snapshot(**overrides), calibrated)
    assert result.status("T1") == "FAIL", why
    assert result.conditions["T1"].blocking is False
    assert result.fire is True, why


def test_t1_pending_without_ema_history(calibrated):
    result = evaluate_arm_b(_snapshot(ema_fast=None, ema_slow=None), calibrated)
    assert result.status("T1") == "PENDING"
    assert result.conditions["T1"].blocking is False   # prereg-v2 §1: observation, not a veto
    assert result.fire is True


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
    # prereg-v2 §1: T2 is out of the blocking AND — a bias-side RSI is context now.
    result = evaluate_arm_b(_snapshot(rsi=48.0), calibrated)
    assert result.status("T2") == "FAIL"
    assert result.conditions["T2"].blocking is False
    assert result.fire is True


@pytest.mark.parametrize("rsi", [75.1, 80.0, 99.0])
def test_t2_exhaustion_high_is_recorded_not_blocking(calibrated, rsi):
    # prereg-v2 §1: exhaustion is journaled, not a veto, in v2.
    result = evaluate_arm_b(_snapshot(rsi=rsi), calibrated)
    assert result.status("T2") == "FAIL"
    assert "EXHAUSTED" in result.conditions["T2"].detail
    assert result.conditions["T2"].blocking is False
    assert result.fire is True


@pytest.mark.parametrize("rsi", [24.9, 10.0, 0.5])
def test_t2_exhaustion_low_is_recorded_not_blocking(calibrated, rsi):
    result = evaluate_arm_b(_snapshot(rsi=rsi), calibrated)
    assert result.status("T2") == "FAIL"
    assert "EXHAUSTED" in result.conditions["T2"].detail
    assert result.conditions["T2"].blocking is False
    assert result.fire is True


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
    assert result.conditions["T2"].blocking is False   # prereg-v2 §1: observation, not a veto
    assert result.fire is True


# ---------------------------------------------------------------------------
# T2b
# ---------------------------------------------------------------------------


def test_t2b_line_against_bias_is_context_when_the_t3_group_passes(calibrated):
    # prereg-v2 §1: T2b still vetoes only when the T3 group also fails — here the
    # squeeze-release carries the (T2b OR T3) group, so a MACD line against the bias
    # is a journaled observation, not a veto.
    snap = _snapshot(macd_line=0.9, macd_signal=1.0, macd_hist=-0.1, macd_hist_prev=-0.2)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert result.conditions["T2b"].blocking is False
    assert "T2b OR T3-group satisfied by T3a squeeze-release" in result.conditions["T2b"].detail
    assert result.fire is True


def test_t2b_contracting_histogram_is_context_when_the_t3_group_passes(calibrated):
    # Line still above signal, but the histogram is shrinking -> momentum fading.
    # prereg-v2 §1: recorded, not a veto, while the T3 group carries the gate.
    snap = _snapshot(macd_line=1.2, macd_signal=1.0, macd_hist=0.05, macd_hist_prev=0.2)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert "CONTRACTING" in result.conditions["T2b"].detail
    assert result.conditions["T2b"].blocking is False
    assert result.fire is True


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
    # prereg-v2 §1: a pending T2b is an observation while the T3 group carries the gate.
    result = evaluate_arm_b(_snapshot(macd_hist_prev=None), calibrated)
    assert result.status("T2b") == "PENDING"
    assert result.conditions["T2b"].blocking is False
    assert result.fire is True


def test_t2b_fresh_cross_against_bias_is_context_when_t3b_carries(calibrated):
    # Cross down 3h ago (< 24h whipsaw guard), now trying to buy the recovery. No
    # squeeze-release, so the rulebook's single exception does not apply — but the
    # lower-band rejection still carries the (T2b OR T3) group, so in v2 the guard
    # is journaled context rather than a veto. (prereg-v2 §1.)
    snap = _snapshot(
        macd_cross_direction="put", macd_cross_age_hours=3.0, bollinger=_bb(squeeze=False)
    )
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert "WHIPSAW GUARD" in result.conditions["T2b"].detail
    assert result.conditions["T2b"].blocking is False
    assert result.fire is True


def test_t2b_fresh_cross_against_bias_allowed_with_squeeze_release(calibrated):
    # The rulebook's one exception (fresh_cross_guard.exception_if:
    # t3_squeeze_release_fires). The base snapshot is a squeeze release.
    snap = _snapshot(macd_cross_direction="put", macd_cross_age_hours=3.0)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T3a") == "PASS"  # the exception's precondition holds
    assert result.status("T2b") == "PASS"
    assert result.fire is True


def test_t2b_fresh_cross_without_squeeze_release_is_context_when_t3b_passes(calibrated):
    # The guard trips (fresh cross against bias, no squeeze-release exception), but
    # the band rejection carries the group, so the guard is context in v2.
    snap = _snapshot(
        macd_cross_direction="put",
        macd_cross_age_hours=2.0,
        bollinger=_bb(squeeze=False),
    )
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert result.conditions["T2b"].blocking is False
    assert result.fire is True


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


def test_t3_pending_without_bollinger_state_is_context_when_t2b_carries(calibrated):
    # prereg-v2 §1: T2b joined the OR group. With a healthy MACD read, T2b satisfies
    # (T2b OR T3) and a pending T3 group is journaled context, not a veto.
    result = evaluate_arm_b(_snapshot(bollinger=None), calibrated)
    assert result.status("T3a") == "PENDING"
    assert result.status("T3b") == "PENDING"
    assert result.status("T2b") == "PASS"
    assert result.conditions["T3a"].blocking is False
    assert result.conditions["T3b"].blocking is False
    assert "T2b OR T3-group satisfied by T2b macd" in result.conditions["T3a"].detail
    assert result.fire is True


# ---------------------------------------------------------------------------
# T3 as an OR group: t3_bollinger.satisfied_if_any_of = [squeeze_release,
# band_rejection]. These are the semantics tests — the evaluator treats T3 as one
# gate, so each arm is tested by what it contributes to the group.
# ---------------------------------------------------------------------------


def test_t3_group_fires_when_only_t3a_passes(calibrated):
    # MACD broken so the group is carried by a T3 leg alone, not by T2b.
    snap = _snapshot(bollinger=_bb(squeeze=False),
                     macd_line=0.9, macd_signal=1.0, macd_hist=-0.1, macd_hist_prev=-0.2)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert result.status("T3a") == "FAIL"
    assert result.status("T3b") == "PASS"
    assert result.conditions["T3a"].blocking is False
    assert result.fire is True
    assert result.veto_reasons == ()
    # the losing arms' details say the group is satisfied, and by which arm
    assert "T2b OR T3-group satisfied by T3b band-rejection" in result.conditions["T3a"].detail
    assert "T2b OR T3-group satisfied by T3b band-rejection" in result.conditions["T2b"].detail


def test_t3_group_fires_when_only_t3b_passes(calibrated):
    # MACD broken so the group is carried by the squeeze-release alone, not by T2b.
    snap = _snapshot(bollinger=_bb(low=596.0),
                     macd_line=0.9, macd_signal=1.0, macd_hist=-0.1, macd_hist_prev=-0.2)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert result.status("T3a") == "PASS"
    assert result.status("T3b") == "FAIL"
    assert result.conditions["T3b"].blocking is False
    assert result.fire is True
    assert result.veto_reasons == ()
    assert "T2b OR T3-group satisfied by T3a squeeze-release" in result.conditions["T3b"].detail
    assert "T2b OR T3-group satisfied by T3a squeeze-release" in result.conditions["T2b"].detail


def test_t3_group_vetoes_once_when_the_whole_group_fails(calibrated):
    # prereg-v2 §1: the group is now (T2b OR T3a OR T3b). Break all three members:
    # MACD line against the bias kills T2b, no squeeze kills T3a, no band touch kills
    # T3b. The group must still produce exactly ONE veto, carried by T3a.
    snap = _snapshot(bollinger=_bb(squeeze=False, low=596.0),
                     macd_line=0.9, macd_signal=1.0, macd_hist=-0.1, macd_hist_prev=-0.2)
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert result.status("T3a") == "FAIL"
    assert result.status("T3b") == "FAIL"
    assert result.fire is False
    # ONE group veto, carried by T3a; T2b and T3b are context folded into it.
    assert result.conditions["T3a"].blocking is True
    assert result.conditions["T3b"].blocking is False
    assert result.conditions["T2b"].blocking is False
    t3_vetoes = [r for r in result.veto_reasons if r.startswith("T3")]
    assert len(t3_vetoes) == 1, result.veto_reasons
    assert t3_vetoes[0].startswith("T3a:")
    assert "T2b OR T3-group veto: no member satisfied" in t3_vetoes[0]
    # T3b's own read is preserved in T3a's detail so the histogram is still readable.
    assert "tagged=False" in t3_vetoes[0]
    assert "folded into T2b OR T3-group veto" in result.conditions["T3b"].detail
    assert "folded into T2b OR T3-group veto" in result.conditions["T2b"].detail


def test_t3_group_veto_is_direction_agnostic_on_the_put_side(calibrated):
    # Put side: squeeze-release needs the middle band FALLING and no upper-band tag.
    bear_bb = _bb(upper=410.0, middle=402.5, lower=395.0, low=399.0, high=409.0,
                  middle_rising=False, squeeze=False)
    # Contracting put-side histogram breaks T2b too, so the whole group fails.
    result = evaluate_arm_b(
        _bear_snapshot(bollinger=bear_bb, macd_hist=-0.05, macd_hist_prev=-0.2),
        calibrated, direction="put",
    )
    assert result.status("T2b") == "FAIL"
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
    # prereg-v2 §1: the floor is 1.2 now, not v1's 1.5.
    result = evaluate_arm_b(_snapshot(relvol=1.09), calibrated)
    assert result.status("T4") == "FAIL"
    assert result.fire is False


def test_t4_exactly_at_threshold_passes(calibrated):
    # prereg-v2 §1: 1.2 is the frozen floor; "relative volume >= 1.2" is inclusive.
    result = evaluate_arm_b(_snapshot(relvol=1.2), calibrated)
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


def test_t5_arm_b_iv_rank_gate_is_recorded_not_blocking(calibrated):
    # prereg-v2 §1: the IV ceiling is still computed (the series keeps accumulating —
    # mechanism evidence for a future cycle), but it no longer vetoes.
    assert evaluate_arm_b(_snapshot(iv_rank=49.9), calibrated).status("T5") == "PASS"
    result = evaluate_arm_b(_snapshot(iv_rank=50.0), calibrated)
    assert result.status("T5") == "FAIL"
    assert result.conditions["T5"].blocking is False
    assert result.fire is True


def test_t5_pending_without_iv_rank(calibrated):
    result = evaluate_arm_b(_snapshot(iv_rank=None), calibrated)
    assert result.status("T5") == "PENDING"
    assert result.conditions["T5"].blocking is False
    assert result.fire is True


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
# table-driven: every v2 BLOCKING condition can individually stop the trade.
# Arm B on the calibrated rulebook, so each row is the ONLY reason the trade did not
# fire (asserted below).
#
# prereg-v2 §1: the blocking set is T4 AND (T2b OR T3-group), plus T6 for arm B.
# T2b cannot veto on its own anymore — it vetoes only when the T3 group fails with
# it, and the group veto is then carried by T3a with T2b folded in. The T1/T2/T5
# rows from v1 moved to the observation table below.
# ---------------------------------------------------------------------------

Mutator = Callable[[IndicatorSnapshot], IndicatorSnapshot]


def _mutate(**kwargs: Any) -> Mutator:
    def apply(snap: IndicatorSnapshot) -> IndicatorSnapshot:
        return dataclasses.replace(snap, **kwargs)

    return apply


# A T3-group-failing snapshot: no squeeze (kills T3a), no band touch (kills T3b),
# and the MACD line against the bias (kills T2b) so the WHOLE group is unsatisfied.
GROUP_FAIL = _mutate(macd_line=0.9, macd_signal=1.0, macd_hist=-0.1, macd_hist_prev=-0.2,
                     bollinger=_bb(squeeze=False, low=596.0))

CASES: list[tuple[str, Mutator, int]] = [
    # (condition id, mutator, expected number of vetoes)
    # T2b broken together with the group: the veto is the group's, carried by T3a.
    ("T2b", GROUP_FAIL, 1),
    # T3 is one OR group, so the blocking row is the GROUP: every member failing is
    # the only way it can veto, and it vetoes exactly once.
    ("T3", GROUP_FAIL, 1),
    ("T4", _mutate(relvol=0.9), 1),
    ("T6", _mutate(flow_gate=FlowGate(status="pass", ratio=0.5, eod_confirmed=True)), 1),
    # Event-day T5: the one exception to the v2 non-blocking T5 read. A veto-kind
    # event day is still a hard veto (v1 ruling 2026-10-02, carried by prereg-v2
    # §4/§6) — it blocks on its own, independent of the non_blocking list, so it is
    # a blocking CASE, not an OBSERVATION_CASE. The non-event IV read is the
    # observation (see OBSERVATION_CASES).
    ("T5", _mutate(is_event_day=True, event_kinds=("fomc",)), 1),
]


@pytest.mark.parametrize(
    ("condition_id", "mutate", "expected_vetoes"),
    CASES,
    ids=[f"{cid}-{i}" for i, (cid, _m, _n) in enumerate(CASES)],
)
def test_each_condition_individually_blocks(calibrated, condition_id, mutate, expected_vetoes):
    result = evaluate_arm_b(mutate(_snapshot()), calibrated)
    assert result.fire is False, f"{condition_id} should have blocked"
    if condition_id in ("T3", "T2b"):
        # The group veto is carried by T3a; T2b and T3b are folded in, never second vetoes.
        assert result.status("T3a") == "FAIL"
        assert result.conditions["T3a"].blocking is True
        assert result.conditions["T3b"].blocking is False
        assert result.conditions["T2b"].blocking is False
        assert any("T2b OR T3-group veto" in r for r in result.veto_reasons)
        first_veto_id = "T3a"
    else:
        assert result.status(condition_id) != "PASS"
        first_veto_id = condition_id
    # the condition under test is the FIRST veto listed (rulebook evaluation order),
    # and nothing else fails beyond the expected coupled conditions.
    assert result.veto_reasons[0].startswith(f"{first_veto_id}:")
    assert len(result.veto_reasons) == expected_vetoes, result.veto_reasons


def test_fresh_cross_guard_couples_into_the_group_veto(calibrated):
    # The whipsaw guard fires only when T3a squeeze-release has NOT fired. With the
    # band touch broken too, the whole (T2b OR T3) group fails and the guard's T2b
    # failure is folded into the single group veto — counted once, as in v1's OR.
    snap = _snapshot(
        macd_cross_direction="put", macd_cross_age_hours=1.0,
        bollinger=_bb(squeeze=False, low=596.0),
    )
    result = evaluate_arm_b(snap, calibrated)
    assert result.status("T2b") == "FAIL"
    assert "WHIPSAW GUARD" in result.conditions["T2b"].detail
    assert result.conditions["T2b"].blocking is False
    assert result.fire is False
    assert len(result.veto_reasons) == 1
    assert result.veto_reasons[0].startswith("T3a:")


# ---------------------------------------------------------------------------
# table-driven: every v2 OBSERVATION is computed but never blocks (prereg-v2 §1).
# ---------------------------------------------------------------------------

# The event-day T5 case is intentionally NOT here: it is the one exception to the
# "never blocking" rule — an event day of a veto kind is still a hard veto (v1 ruling
# 2026-10-02, carried by prereg-v2 §4/§6), covered by test_t5_event_day_is_a_hard_veto.
# The remaining T5 case is the non-event IV read, which is a v2 observation.
OBSERVATION_CASES: list[tuple[str, Mutator]] = [
    ("T1", _mutate(ema_fast=605.0)),                  # close 600 < EMA50 605
    ("T2", _mutate(rsi=49.0)),
    ("T2", _mutate(rsi=90.0)),
    ("T2", _mutate(rsi=20.0)),
    ("T5", _mutate(iv_rank=80.0)),
]


@pytest.mark.parametrize(
    ("condition_id", "mutate"),
    OBSERVATION_CASES,
    ids=[f"{cid}-{i}" for i, (cid, _m) in enumerate(OBSERVATION_CASES)],
)
def test_observations_are_journaled_never_blocking(calibrated, condition_id, mutate):
    # prereg-v2 §1: T1, T2 and T5 are removed from the blocking AND. They are still
    # computed (the row shows a real FAIL, not a skip) and never stop the trade.
    result = evaluate_arm_b(mutate(_snapshot()), calibrated)
    assert result.status(condition_id) != "PASS"
    assert result.conditions[condition_id].blocking is False
    assert result.fire is True, f"{condition_id} is a v2 observation, not a veto"
    assert not any(r.startswith(f"{condition_id}:") for r in result.veto_reasons)

def _t4_detail(result: ChecklistResult) -> str:
    return result.conditions["T4"].detail


def test_t4_passes_on_the_run_rate_alone_when_relvol_does_not(calibrated):
    """The brief's OR: "relative volume >= 1.5x 20-day average on the signal candle
    (or intraday run-rate >= 1.5x for intraday triggers)".

    relvol below threshold is the NORMAL intraday case — a partial day's volume against
    a mean of completed sessions — which is exactly the situation the parenthetical was
    written for. Before this branch existed, T4 could not be satisfied that way at all.
    """
    result = evaluate_arm_b(_snapshot(relvol=0.8, intraday_run_rate=1.9), calibrated)
    assert result.status("T4") == "PASS"
    detail = _t4_detail(result)
    assert "PASS via intraday run-rate" in detail
    assert "relative volume 0.800x" in detail  # reported, just not what carried it


def test_t4_passes_on_relvol_alone_when_the_run_rate_is_below(calibrated):
    """The OR is symmetric: a healthy relvol still passes with a quiet run rate."""
    result = evaluate_arm_b(_snapshot(relvol=1.6, intraday_run_rate=0.4), calibrated)
    assert result.status("T4") == "PASS"


def test_t4_fails_when_neither_branch_confirms(calibrated):
    result = evaluate_arm_b(_snapshot(relvol=0.8, intraday_run_rate=1.2), calibrated)
    assert result.status("T4") == "FAIL"
    assert "volume did not confirm" in _t4_detail(result)


def test_t4_still_passes_on_relvol_when_the_run_rate_is_unavailable(calibrated):
    """A missing run rate is a fallback, not a new blocking condition. The pre-market
    plan builds from daily bars only; if this went PENDING, no plan could ever be built.
    """
    result = evaluate_arm_b(_snapshot(relvol=1.6), calibrated)
    assert result.status("T4") == "PASS"
    assert "intraday run-rate unavailable" in _t4_detail(result)


def test_t4_pending_only_when_neither_branch_can_be_measured(calibrated):
    result = evaluate_arm_b(_snapshot(relvol=None, intraday_run_rate=None), calibrated)
    assert result.status("T4") == "PENDING"


def test_t4_run_rate_threshold_is_the_frozen_one_not_the_relvol_one(calibrated):
    """Both thresholds are 1.5 in the rulebook today. This pins that T4 reads
    ``intraday_run_rate_min`` for the run rate rather than reusing
    ``relative_volume_min`` — a copy-paste of the wrong field would be invisible while
    the two values happen to agree."""
    rules = calibrated
    result = evaluate_arm_b(_snapshot(relvol=0.8, intraday_run_rate=1.5), rules)
    assert result.status("T4") == "PASS"
    result = evaluate_arm_b(_snapshot(relvol=0.8, intraday_run_rate=1.49), rules)
    assert result.status("T4") == "FAIL"


def test_t4_non_finite_run_rate_is_a_fail_not_a_pass(calibrated):
    result = evaluate_arm_b(_snapshot(relvol=0.8, intraday_run_rate=math.inf), calibrated)
    assert result.status("T4") == "FAIL"


def test_t4_rejects_a_run_rate_when_relvol_is_unavailable(calibrated):
    """relvol is PENDING-shaped, the run rate is measurable and short — so volume did
    not confirm, which is a FAIL. PENDING would claim the system did not know."""
    result = evaluate_arm_b(_snapshot(relvol=None, intraday_run_rate=0.9), calibrated)
    assert result.status("T4") == "FAIL"
