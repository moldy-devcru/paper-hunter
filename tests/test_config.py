"""Tests for the frozen rulebook loader and the T5 event calendar.

The theme: a rulebook that is *frozen* must fail loudly when tampered with, and the
event veto must actually fire. A silently-mistyped threshold would produce an
experiment that measures the wrong thing without ever crashing — so most of these
tests are negative tests on purpose.
"""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path

import pytest
import yaml

from config.loader import (
    DEFAULT_RULES_PATH,
    FROZEN,
    RulesError,
    load_rules,
    load_rules_text,
)
from data.event_calendar import (
    EVENTS_DIR,
    CalendarEvent,
    EventCalendar,
    EventError,
    load_calendar_file,
)

EXAMPLE_TEXT = DEFAULT_RULES_PATH.read_text(encoding="utf-8")
CALENDAR_FILE = EVENTS_DIR / "2026-Q4.yaml"


@pytest.fixture
def raw() -> dict:
    """The example rulebook as plain data, for tamper tests."""
    return yaml.safe_load(EXAMPLE_TEXT)


def _tamper(mutate) -> str:
    """Apply a mutation to the example rulebook and return the serialized YAML."""
    data = copy.deepcopy(yaml.safe_load(EXAMPLE_TEXT))
    mutate(data)
    return yaml.safe_dump(data, sort_keys=False)


def _set(d: dict, path: str, value) -> None:
    """Set a dotted path in the parsed rulebook — keeps tamper cases readable."""
    node = d
    *parts, last = path.split(".")
    for part in parts:
        node = node[part]
    node[last] = value


# ---------------------------------------------------------------------------
# the example file is the brief, transcribed
# ---------------------------------------------------------------------------


def test_example_file_loads():
    rules = load_rules(DEFAULT_RULES_PATH)
    assert rules.strategy.symbol == "SPY"
    assert rules.arms.A.bankroll_usd == FROZEN["bankroll_usd"]
    assert rules.arms.B.bankroll_usd == FROZEN["bankroll_usd"]
    assert rules.arms.C.bankroll_usd == FROZEN["bankroll_usd"]
    assert rules.execution.total_bankroll_usd == 3 * FROZEN["bankroll_usd"]


def test_checklist_thresholds_match_the_brief():
    c = load_rules().checklist
    assert (c.t1_ema_alignment.ema_fast, c.t1_ema_alignment.ema_slow) == (50, 200)
    assert c.t2_rsi.period == 14
    assert (c.t2_rsi.exhaustion.high, c.t2_rsi.exhaustion.low) == (75, 25)
    assert (c.t2b_macd.fast, c.t2b_macd.slow, c.t2b_macd.signal) == (12, 26, 9)
    assert c.t2b_macd.fresh_cross_guard.max_age_hours == 24
    assert c.t3_bollinger.satisfied_if_any_of.squeeze_release.bandwidth_below_percentile == 20
    assert c.t3_bollinger.satisfied_if_any_of.squeeze_release.lookback_days == 60
    # prereg-v2 §1: the relative-volume floor is 1.2 (v1: 1.5); run-rate and lookback unchanged.
    assert (c.t4_volume.relative_volume_min, c.t4_volume.lookback_days) == (1.2, 20)
    assert c.t4_volume.intraday_run_rate_min == 1.5
    assert c.t5_options_chain.arm_b.iv_rank_max == 50
    assert c.t5_options_chain.event_calendar.hard_veto is True


def test_arm_b_specifics_match_the_brief():
    b = load_rules().arms.B
    assert (b.entry.window_et.start, b.entry.window_et.end) == ("09:45", "14:00")
    assert b.entry.dte == 0
    assert b.entry.moneyness == "OTM"
    assert b.entry.premium_pct_of_bankroll_max == 0.02
    assert b.entry.premium_usd_hard_cap == 200
    assert b.exits.time_stop.after_entry_minutes == 120
    assert b.exits.time_stop.if_under_gain_pct == 0.30
    assert b.exits.profit_take.close_pct_at_100pct_gain == 0.50
    assert b.exits.hard_close_et == "15:30"
    assert b.exits.hold_to_expiry is False
    assert b.limits.max_concurrent_positions == 1
    assert b.limits.max_entries_per_week == 3


def test_arm_c_specifics_match_the_brief():
    c = load_rules().arms.C
    assert (c.entry.dte.min, c.entry.dte.max) == (90, 180)
    assert c.entry.delta_min == 0.80
    assert c.entry.premium_pct_of_bankroll_max == 0.50
    roll = c.exits.roll
    assert roll.trigger.delta_below == 0.70
    assert roll.trigger.dte_below == 45
    assert roll.trigger.rule == "whichever_first"
    assert roll.discretionary is False
    assert "3_consecutive_eods" in c.exits.exit_all.condition
    assert c.limits.max_concurrent_positions == 1


def test_arm_c_declares_its_own_entry_window():
    """RULED 2026-10-03 (operator): arm C's window is 09:45-15:30 ET, inclusive.

    The declaration is the whole ruling. A window the code invents (the old
    whole-session fallback) is a policy nobody voted for, and R2's "the entry window
    alone" had nothing to be alone to.
    """
    window = load_rules().arms.C.entry.window_et
    assert (window.start, window.end) == ("09:45", "15:30")
    # prereg-v2 (ratified 2026-11-18): the rulebook is now FROZEN at 2.0.0 and T6's
    # multiplier is frozen per side (§3) — the point of the assertion is that the
    # version *status* tracks the pre-registration lifecycle, not the window shape.
    rules = load_rules()
    assert rules.strategy.status == "FROZEN"
    assert rules.strategy.version == "2.0.0"
    assert rules.checklist.t6_flow.multiplier.call.calibration_pending is False


def test_arm_c_without_a_window_fails_to_load():
    """The field is REQUIRED, not optional-with-a-default.

    If a window can be absent then the closed-by-default governor below is the only
    thing standing between a typo and a silently-open arm, and a typo in a YAML file
    should be a load error the operator sees at startup.
    """
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    del data["arms"]["C"]["entry"]["window_et"]
    with pytest.raises(RulesError):
        load_rules_text(yaml.safe_dump(data))


def test_arm_a_is_buy_and_hold_day_one():
    a = load_rules().arms.A
    assert a.entry.when == "first_market_open_of_window"
    assert a.trading_days == 1
    assert a.managed is False


def test_t6_is_frozen_per_side_not_invented():
    # prereg-v2 §3: N is frozen per side (call 5.59 / put 3.07 — P90 over all 94
    # recorded soak sessions at the working 3% deep-OTM threshold), deep-OTM 3.0 on
    # both legs. The old "pending until the first monthly review" state is gone.
    t6 = load_rules().checklist.t6_flow
    assert t6.applies_to == ["B"]
    assert (t6.multiplier.call.value, t6.multiplier.call.calibration_pending) == (5.59, False)
    assert (t6.multiplier.put.value, t6.multiplier.put.calibration_pending) == (3.07, False)
    assert (t6.deep_otm.calls.value, t6.deep_otm.calls.calibration_pending) == (3.0, False)
    assert (t6.deep_otm.puts.value, t6.deep_otm.puts.calibration_pending) == (3.0, False)
    assert t6.evaluation == "EOD_only"
    assert load_rules().pending_calibrations == []


# ---------------------------------------------------------------------------
# operator rulings, 2026-10-02
# ---------------------------------------------------------------------------


def test_window_is_three_months_sixty_sessions_renewable_only_at_review():
    w = load_rules().window
    assert w.months == FROZEN["window_months"] == 3
    assert w.target_sessions == FROZEN["window_target_sessions"] == 60
    assert w.extension == "monthly_review_only"


def test_window_opens_all_three_arms_on_the_v2_start_date():
    """RULED 2026-10-03 (R5) pinned the start; prereg-v2 §2 renamed the roster.

    The v2 window opens arms A + B + C together on 2026-11-19. The start is a number
    in the rulebook that can be read and checked — a stronger claim than the absence
    ("stamped when the window opens") it originally replaced.
    """
    w = load_rules().window
    assert w.start == dt.date(2026, 11, 19)
    assert w.arms == ["A", "B", "C"]


def test_arm_b_gate_is_retired_not_deferred():
    """prereg-v2 §1: the arm B activation gate is retired, not just satisfied.

    It existed only because T5 was blocking and could not warm for 0-DTE. With T5 a
    non-blocking observation, arm B opens at window start — ``arm_b_gate: null`` is
    the visible record of that retirement (an absence of the gate, not a gate that
    happens to pass).
    """
    rb = load_rules()
    assert rb.window.arm_b_gate is None
    assert rb.inert_arms() == []
    assert rb.active_arms() == ["A", "B", "C"]
    # and the t5_iv_rank_defined knob no longer changes anything with the gate gone
    assert rb.inert_arms(t5_iv_rank_defined=True) == []
    assert rb.active_arms(t5_iv_rank_defined=True) == ["A", "B", "C"]


def test_window_length_must_be_plausible():
    bad = _tamper(lambda d: _set(d, "window.target_sessions", 400))
    with pytest.raises(RulesError):
        load_rules_text(bad)


def test_window_start_can_be_stamped_when_the_window_opens():
    stamped = load_rules_text(_tamper(lambda d: _set(d, "window.start", "2026-10-05")))
    assert stamped.window.start == dt.date(2026, 10, 5)


def test_arm_c_iv_ceiling_is_configurable_at_50():
    """Ruled 2026-10-02: same ceiling as arm B. The loader no longer refuses a number."""
    t5 = load_rules().checklist.t5_options_chain
    assert t5.arm_c.iv_rank_max == FROZEN["arm_c_iv_rank_max"] == 50
    assert t5.arm_c.calibration_pending is False
    assert not any("arm_c" in p for p in load_rules().pending_calibrations)

    # the field is genuinely configurable now, within the same 0-100 domain
    moved = load_rules_text(
        _tamper(lambda d: _set(d, "checklist.t5_options_chain.arm_c.iv_rank_max", 35.0))
    )
    assert moved.checklist.t5_options_chain.arm_c.iv_rank_max == 35.0
    with pytest.raises(RulesError):
        load_rules_text(
            _tamper(lambda d: _set(d, "checklist.t5_options_chain.arm_c.iv_rank_max", 120.0))
        )


def test_arm_c_ceiling_cannot_be_smuggled_in_while_pending():
    bad = _tamper(
        lambda d: (
            _set(d, "checklist.t5_options_chain.arm_c.calibration_pending", True),
            _set(d, "checklist.t5_options_chain.arm_c.iv_rank_max", 60.0),
        )
    )
    with pytest.raises(RulesError):
        load_rules_text(bad)


def test_earnings_veto_is_disabled_by_ruling_and_cannot_be_silently_re_enabled():
    ev = load_rules().checklist.t5_options_chain.event_calendar
    assert ev.veto_kinds == ["fomc", "cpi"]
    veto = ev.earnings_veto
    assert (veto.enabled, veto.required, veto.implemented) == (False, False, False)
    assert veto.calibration_pending is False
    assert "2026-10-02" in veto.disabled_by
    assert veto.reason  # the brief asked for it; the file says why it is not there
    assert not any("earnings" in p for p in load_rules().pending_calibrations)

    # flipping it back on is not a YAML edit — the model refuses it
    with pytest.raises(RulesError):
        load_rules_text(
            _tamper(
                lambda d: _set(
                    d,
                    "checklist.t5_options_chain.event_calendar.earnings_veto.enabled",
                    True,
                )
            )
        )
    with pytest.raises(RulesError):
        load_rules_text(
            _tamper(
                lambda d: _set(
                    d,
                    "checklist.t5_options_chain.event_calendar.earnings_veto.calibration_pending",
                    True,
                )
            )
        )
    # ...nor by quietly adding earnings back to the veto kinds
    with pytest.raises(RulesError):
        load_rules_text(
            _tamper(
                lambda d: _set(
                    d, "checklist.t5_options_chain.event_calendar.veto_kinds",
                    ["fomc", "cpi", "earnings"],
                )
            )
        )


def test_earnings_kind_no_longer_vetoes_by_default():
    ev = EventCalendar.load(CALENDAR_FILE)
    day = dt.date(2026, 10, 7)
    earnings = CalendarEvent(
        date=day, kind="earnings", label="a component reports", verified=True
    )
    assert earnings.is_veto() is False  # ruled 2026-10-02: index IV regime does not care
    assert ev.is_event_day(day, veto_kinds=["fomc", "cpi"]) is False


def test_no_calibrations_remain_pending():
    """prereg-v2 §3 froze the T6 pair and both deep-OTM legs; T5 arm C was ruled
    2026-10-02. The v2 rulebook opens with nothing pending."""
    assert load_rules().pending_calibrations == []


def test_strategy_version_is_version_plus_content_hash():
    rules = load_rules(DEFAULT_RULES_PATH)
    assert rules.strategy_version.startswith(f"{rules.strategy.version}+")
    assert rules.config_sha256.startswith(rules.strategy_version.split("+")[1])
    # same bytes -> same version; different bytes -> different version, same version string
    bumped = load_rules_text(_tamper(lambda d: d["strategy"].__setitem__("version", "1.0.1-draft")))
    assert bumped.strategy_version != rules.strategy_version
    assert bumped.strategy.version != rules.strategy.version


def test_comment_edit_changes_the_fingerprint():
    """The hash covers file bytes, not just parsed data — a comment is still a change."""
    assert load_rules_text(EXAMPLE_TEXT).strategy_version != load_rules_text(
        EXAMPLE_TEXT + "\n# touched\n"
    ).strategy_version


# ---------------------------------------------------------------------------
# tampering is rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "path", "value"),
    [
        ("unknown top-level key", "typo_key", 1),
        ("unknown nested key", "arms.B.limits.max_postion", 3),
        ("misspelled threshold", "checklist.t4_volume.relative_volume_min_typo", 1.5),
        ("unknown section", "checklist.t7_vibes", {}),
    ],
)
def test_unknown_keys_are_rejected(label, path, value):
    with pytest.raises(RulesError):
        load_rules_text(_tamper(lambda d: _set(d, path, value)))


def test_pending_value_with_a_number_in_it_is_rejected():
    """The whole point of calibration_pending: a fabricated threshold is not a default.

    prereg-v2 §3: the schema is per-side; both legs still carry the honesty rules.
    """
    # value null WITH the pending flag is the honest pending state — it loads
    honest = load_rules_text(
        _tamper(
            lambda d: d["checklist"]["t6_flow"]["multiplier"]["call"].update(
                {"value": None, "calibration_pending": True}
            )
        )
    )
    assert honest.checklist.t6_flow.multiplier.call.calibration_pending is True
    # pending flag + a number in it is an invented threshold
    with pytest.raises(RulesError):
        load_rules_text(
            _tamper(
                lambda d: d["checklist"]["t6_flow"]["multiplier"]["call"].update(
                    {"value": 2.0, "calibration_pending": True}
                )
            )
        )
    # value null without the pending flag is an unexplained null
    with pytest.raises(RulesError):
        load_rules_text(
            _tamper(
                lambda d: d["checklist"]["t6_flow"]["multiplier"]["call"].update(
                    {"value": None, "calibration_pending": False}
                )
            )
        )


def test_calibrated_t6_multiplier_is_allowed():
    """Once calibrated, a real number is legitimate — still must be numeric.

    prereg-v2 §3: per-side schema, so the call leg is the one under test here.
    """
    rules = load_rules_text(
        _tamper(
            lambda d: d["checklist"]["t6_flow"]["multiplier"]["call"].update(
                {"value": 1.8, "calibration_pending": False}
            )
        )
    )
    assert rules.checklist.t6_flow.multiplier.call.value == 1.8


@pytest.mark.parametrize(
    ("label", "path", "value"),
    [
        ("premium cap inconsistent with pct", "arms.B.entry.premium_usd_hard_cap", 150),
        ("entry window inverted", "arms.B.entry.window_et.start", "14:00"),
        ("hold to expiry enabled", "arms.B.exits.hold_to_expiry", True),
        ("arm bankrolls disagree", "arms.C.bankroll_usd", 9000),
        ("rolling made discretionary", "arms.C.exits.roll.discretionary", True),
        ("t6 applied to arm C", "checklist.t6_flow.applies_to", ["B", "C"]),
        ("arm checklist mismatch", "arms.B.checklist", ["T1", "T2"]),
        (
            "event veto disabled",
            "checklist.t5_options_chain.event_calendar.hard_veto",
            False,
        ),
        ("live-money mode", "execution.mode", "live"),
    ],
)
def test_incoherent_rulebooks_are_rejected(label, path, value):
    with pytest.raises(RulesError):
        load_rules_text(_tamper(lambda d: _set(d, path, value)))


def test_counter_trend_needs_the_full_guardrail():
    """Enabling counter-trend is allowed, but only with the v2 blocking set
    (T4 AND (T2b OR T3-group) -> ``requires_all_of: [T2b, T3, T4]``) plus a documented
    reversal trigger (brief, T1). Half-enabling it is a rulebook contradicting itself.
    prereg-v2 §1: v1's guardrail was T2-T5 all; v2 narrows it to the v2 blocking set."""
    ct = "checklist.t1_ema_alignment.counter_trend"

    def enable(d, **overrides):
        _set(d, f"{ct}.allowed", True)
        for key, value in overrides.items():
            _set(d, f"{ct}.{key}", value)

    # fully specified (the v2 blocking set): fine
    rules = load_rules_text(_tamper(lambda d: enable(d, requires_all_of=["T2b", "T3", "T4"])))
    assert rules.checklist.t1_ema_alignment.counter_trend.allowed is True

    # no documented reversal trigger: rejected
    with pytest.raises(RulesError):
        load_rules_text(_tamper(lambda d: enable(d, requires_documented_reversal_trigger=False)))
    # T4 dropped from the required set: rejected (it is the floor of the v2 gate)
    with pytest.raises(RulesError):
        load_rules_text(_tamper(lambda d: enable(d, requires_all_of=["T2b", "T3"])))


def test_missing_file_is_an_error():
    with pytest.raises(FileNotFoundError):
        load_rules(Path("/nonexistent/rules.yaml"))


# ---------------------------------------------------------------------------
# event calendar
# ---------------------------------------------------------------------------


def test_calendar_file_loads():
    cal = EventCalendar.load(CALENDAR_FILE)
    assert len(cal) > 0
    assert cal.coverage()[0] == cal.events[0].date


def test_event_veto_true_on_fomc_days():
    cal = EventCalendar.load(CALENDAR_FILE)
    for day in ("2026-10-27", "2026-10-28", "2026-12-08", "2026-12-09"):
        assert cal.is_event_day(day) is True, day
        assert any("fomc" in r for r in cal.veto_reasons(day))


def test_event_veto_true_on_cpi_days():
    cal = EventCalendar.load(CALENDAR_FILE)
    assert cal.is_event_day("2026-10-13") is True
    assert cal.is_event_day("2026-11-10") is True
    assert cal.is_event_day("2026-12-10") is True


def test_event_veto_false_on_a_random_tuesday():
    cal = EventCalendar.load(CALENDAR_FILE)
    for day in ("2026-10-06", "2026-11-17", "2026-12-01", "2026-10-20"):
        assert cal.is_event_day(day) is False, day
        assert cal.veto_reasons(day) == []


def test_event_veto_respects_the_rulebook_veto_kinds():
    """OPEX is in the calendar but is not a T5 hard veto."""
    cal = EventCalendar.load(CALENDAR_FILE)
    assert len(cal.events_on("2026-10-16")) == 1
    assert cal.is_event_day("2026-10-16", veto_kinds=["fomc", "cpi"]) is False
    assert cal.is_event_day("2026-10-28", veto_kinds=["fomc", "cpi"]) is True
    assert cal.is_event_day("2026-10-28", veto_kinds=["cpi"]) is False


def test_veto_reasons_carry_the_cause():
    reasons = EventCalendar.load(CALENDAR_FILE).veto_reasons("2026-12-09")
    assert any("fomc" in r and "2026-12-09" in r for r in reasons)


def test_unverified_dates_are_surfaced_not_hidden():
    cal = EventCalendar.load(CALENDAR_FILE)
    unverified = cal.unverified()
    assert {e.kind for e in unverified} == {"cpi"}  # FOMC confirmed against the Fed calendar
    assert any("UNVERIFIED" in r for r in cal.veto_reasons("2026-10-13"))


def test_rulebook_calendar_dir_is_where_the_files_are():
    rules = load_rules()
    cal_dir = Path(rules.checklist.t5_options_chain.event_calendar.calendar_dir)
    assert cal_dir == Path("data/events")
    assert (cal_dir / "2026-Q4.yaml").is_file()


def test_load_dir_merges_and_bad_dates_are_rejected(tmp_path: Path):
    merged = EventCalendar.load_dir()
    assert len(merged) >= len(EventCalendar.load(CALENDAR_FILE))

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "events:\n  - date: 2026-13-45\n    kind: fomc\n    label: nope\n", encoding="utf-8"
    )
    with pytest.raises(EventError):
        load_calendar_file(bad)

    typo = tmp_path / "typo.yaml"
    typo.write_text(
        "events:\n  - date: 2026-10-28\n    kind: fomc\n    lable: typo\n", encoding="utf-8"
    )
    with pytest.raises(EventError):
        load_calendar_file(typo)


def test_empty_calendar_directory_is_an_error(tmp_path: Path):
    """A veto that can never fire is worse than a crash."""
    (tmp_path / "empty.yaml").write_text("events: []\n", encoding="utf-8")
    with pytest.raises(EventError):
        EventCalendar.load_dir(tmp_path)