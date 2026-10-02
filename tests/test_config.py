"""Tests for the frozen rulebook loader and the T5 event calendar.

The theme: a rulebook that is *frozen* must fail loudly when tampered with, and the
event veto must actually fire. A silently-mistyped threshold would produce an
experiment that measures the wrong thing without ever crashing — so most of these
tests are negative tests on purpose.
"""

from __future__ import annotations

import copy
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
from data.event_calendar import EVENTS_DIR, EventCalendar, EventError, load_calendar_file

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
    assert (c.t4_volume.relative_volume_min, c.t4_volume.lookback_days) == (1.5, 20)
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


def test_arm_a_is_buy_and_hold_day_one():
    a = load_rules().arms.A
    assert a.entry.when == "first_market_open_of_window"
    assert a.trading_days == 1
    assert a.managed is False


def test_t6_is_pending_not_invented():
    t6 = load_rules().checklist.t6_flow
    assert t6.applies_to == ["B"]
    assert t6.multiplier.value is None
    assert t6.multiplier.calibration_pending is True
    assert t6.evaluation == "EOD_only"
    assert any("t6" in p for p in load_rules().pending_calibrations)


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
    """The whole point of calibration_pending: a fabricated threshold is not a default."""
    with pytest.raises(RulesError):
        load_rules_text(_tamper(lambda d: _set(d, "checklist.t6_flow.multiplier.value", 2.0)))
    with pytest.raises(RulesError):
        load_rules_text(
            _tamper(lambda d: _set(d, "checklist.t6_flow.multiplier.calibration_pending", False))
        )


def test_calibrated_t6_multiplier_is_allowed():
    """Once calibrated, a real number is legitimate — still must be numeric."""
    rules = load_rules_text(
        _tamper(
            lambda d: d["checklist"]["t6_flow"]["multiplier"].update(
                {"value": 1.8, "calibration_pending": False}
            )
        )
    )
    assert rules.checklist.t6_flow.multiplier.value == 1.8


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
    """Enabling counter-trend is allowed, but only with T2-T5 ALL + a documented
    reversal trigger (brief, T1). Half-enabling it is a rulebook contradicting itself."""
    ct = "checklist.t1_ema_alignment.counter_trend"

    def enable(d, **overrides):
        _set(d, f"{ct}.allowed", True)
        for key, value in overrides.items():
            _set(d, f"{ct}.{key}", value)

    # fully specified: fine
    rules = load_rules_text(_tamper(enable))
    assert rules.checklist.t1_ema_alignment.counter_trend.allowed is True

    # no documented reversal trigger: rejected
    with pytest.raises(RulesError):
        load_rules_text(_tamper(lambda d: enable(d, requires_documented_reversal_trigger=False)))
    # a condition dropped from the required set: rejected
    with pytest.raises(RulesError):
        load_rules_text(_tamper(lambda d: enable(d, requires_all_of=["T2", "T3"])))


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