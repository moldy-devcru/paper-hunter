"""Tests for the hunt plan — the pre-market brain (Phase 4a).

Three properties carry most of the weight here:

1. **A fully-warmed day produces a complete, honest plan.** Every indicator the
   checklist reads is defined, both directions and both active arms are planned, and
   the watch levels carry what the brief asks for (Bollinger bands, EMA levels, arm B's
   entry window).
2. **Trigger prices are honest.** A price level appears only where one genuinely
   exists (T1 with an aligned EMA stack, T3b once a bar has tagged a band); everything
   else is ``null`` with a stated reason. A plan full of invented levels would be the
   most dangerous artifact this project could produce.
3. **The journal row is the plan.** Inserts only, full indicator snapshot, and the
   append-only triggers still hold.

The 260-bar synthetic series is the end-to-end fixture (the same one Phase 3a uses).
Exact checklist states — "exactly one condition failed" — are driven through
``make_arm_plan`` with hand-built snapshots, the way ``test_checklist`` isolates each
veto path; that is deliberate, because with the shipped DRAFT rulebook both active arms
are inert (T6's multiplier and arm C's IV band are both ``calibration_pending``) and
no bar series can produce a firing cell at all.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3

import pytest
import yaml
from fixtures_synthetic import (
    calendar_with,
    deep_itm_chain,
    series_from,
    synthetic_daily_payload,
)

from config.loader import DEFAULT_RULES_PATH, load_rules, load_rules_text
from executor.alpaca_client import Bar
from executor.checklist import BollingerState, FlowGate, IndicatorSnapshot
from executor.hunt_plan import (
    ACTIVE_ARMS,
    DIRECTIONS,
    ArmPlan,
    HuntPlan,
    StaticMarketData,
    build_hunt_plan,
    compute_triggers,
    make_arm_plan,
    project_setup_day_range,
    write_hunt_plan,
)
from executor.iv_rank import IvRankStore
from executor.snapshot_builder import SnapshotResult
from journal.store import (
    DecisionEntry,
    append_decision,
    get_decision,
    init_db,
    list_decisions,
)

LONG = 260


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rules():
    """The shipped DRAFT rulebook."""
    return load_rules()


@pytest.fixture(scope="module")
def calibrated(rules):
    """Same rulebook with T6's flow multiplier calibrated, so T6 can pass at all.

    Arm C's T5 stays calibration_pending (the Phase-1b model types its band as
    ``None``, so it cannot be written) — arm C therefore cannot fire in any test, by
    design, and that fact is asserted rather than worked around.
    """
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": 2.0,
        "calibration_pending": False,
        "reason": "test fixture: calibrated multiplier",
    }
    return load_rules_text(yaml.safe_dump(data))


@pytest.fixture(scope="module")
def series():
    return series_from(synthetic_daily_payload(count=LONG))


@pytest.fixture(scope="module")
def session_day(series):
    """The session the plan is FOR: the session after the last bar in the series."""
    return series.bars[-1].t.date() + dt.timedelta(days=3)


@pytest.fixture(scope="module")
def spot(series):
    return series.bars[-1].c


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "journal.db")
    yield connection
    connection.close()


def _bb(**overrides) -> BollingerState:
    base = BollingerState(
        upper=632.0,
        middle=627.0,
        lower=621.0,
        low=620.0,               # tags the lower band, closes back inside -> T3b passes
        high=628.0,
        bandwidth=0.0175,
        bandwidth_percentile=10.0,  # < 20 -> squeeze
        bandwidth_expanding=True,
        middle_rising=True,
        squeeze=True,
    )
    return dataclasses.replace(base, **overrides)


def passing_snapshot(**overrides) -> IndicatorSnapshot:
    """A fully-defined long-call snapshot. Everything passes with the calibrated rules."""
    base = IndicatorSnapshot(
        close=625.0,
        ema_fast=620.0,
        ema_slow=560.0,
        rsi=58.0,
        macd_line=1.2,
        macd_signal=1.0,
        macd_hist=0.2,
        macd_hist_prev=0.1,
        macd_cross_direction=None,
        macd_cross_age_hours=None,
        bollinger=_bb(),
        relvol=1.8,
        iv_rank=40.0,
        is_event_day=False,
        event_kinds=(),
        flow_gate=FlowGate(status="pass", ratio=2.4, eod_confirmed=True),
    )
    return dataclasses.replace(base, **overrides)


def snapshot_result_for(snapshot: IndicatorSnapshot, *, bars: int = LONG) -> SnapshotResult:
    return SnapshotResult(snapshot=snapshot, pending_reasons=(), bars_used=bars)


def cell_for(snapshot: IndicatorSnapshot, rules, *, arm: str = "B", direction: str = "call",
             event_day_veto: bool = False, prior_bar: Bar | None = None) -> ArmPlan:
    return make_arm_plan(
        arm=arm,
        direction=direction,
        snapshot_result=snapshot_result_for(snapshot),
        rules=rules,
        prior_bar=prior_bar,
        event_day_veto=event_day_veto,
    )


def plan_for(cells, *, day: dt.date, rules, event_day_veto: bool = False) -> HuntPlan:
    return HuntPlan(
        day=day,
        symbol=rules.strategy.symbol,
        generated_ts="2025-06-02T12:35:00Z",
        strategy_version=rules.strategy_version,
        config_sha256=rules.config_sha256,
        arms=tuple(cells),
        event_day_veto=event_day_veto,
        event_reasons=("fomc: FOMC decision",) if event_day_veto else (),
    )


# ---------------------------------------------------------------------------
# a fully-warmed day
# ---------------------------------------------------------------------------


def test_plan_covers_every_direction_and_active_arm(series, rules, session_day):
    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    assert plan.cells() == (
        ("B", "call"), ("B", "put"), ("C", "call"), ("C", "put"),
    )
    assert set(ACTIVE_ARMS) == {"B", "C"}
    assert set(DIRECTIONS) == {"call", "put"}
    assert plan.symbol == rules.strategy.symbol
    assert plan.strategy_version == rules.strategy_version
    assert plan.config_sha256 == rules.config_sha256


def test_fully_warmed_day_has_every_indicator_defined(series, rules, session_day):
    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    for cell in plan.arms:
        indicators = cell.snapshot_dict["indicators"]
        for key in ("close", "ema_fast", "ema_slow", "rsi", "macd_line", "macd_signal",
                    "macd_hist", "macd_hist_prev", "relvol"):
            assert indicators[key] is not None, f"{cell.key} {key}"
        assert indicators["bollinger"] is not None
        for key in ("upper", "middle", "lower", "bandwidth", "bandwidth_percentile",
                    "squeeze", "bandwidth_expanding", "middle_rising"):
            assert indicators["bollinger"][key] is not None, f"{cell.key} bb.{key}"
        # No warmup gaps left in the provenance: the only pending notes left are the
        # honest "no cross in history" / "no IV rank supplied" statements.
        gaps = [
            r for r in cell.snapshot_dict["pending_reasons"]
            if "needs" in r or "unavailable" in r or "unusable" in r
        ]
        assert gaps == [], gaps


def test_pending_conditions_are_three_statuses_not_silent_passes(series, rules, session_day):
    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    b_call = plan.cell("B", "call").checklist
    # No chain and no IV store supplied -> arm B's T5 cannot be evaluated. PENDING, not PASS.
    assert b_call.status("T5") == "PENDING"
    assert b_call.fire is False
    # T6 is calibration_pending in the shipped rulebook, so arm B can never fire here.
    assert b_call.status("T6") == "PENDING"
    # T6 does not apply to arm C and is reported as SKIPPED, not silently missing.
    c_call = plan.cell("C", "call").checklist
    assert c_call.status("T6") == "SKIPPED"
    assert c_call.conditions["T6"].blocking is False


def test_watch_levels_carry_bands_emas_and_arm_b_entry_window(series, rules, session_day):
    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    for cell in plan.arms:
        watch = cell.watch
        assert watch.close == series.bars[-1].c
        assert watch.ema_fast == pytest.approx(626.7515, rel=1e-4)
        assert watch.ema_slow == pytest.approx(567.031, rel=1e-4)
        assert watch.bollinger_lower < watch.bollinger_middle < watch.bollinger_upper
        assert watch.prior_high == series.bars[-1].h
        assert watch.prior_low == series.bars[-1].l

    b_call = plan.cell("B", "call").watch.arm_b_criteria
    assert b_call["entry_window_et"] == {"start": "09:45", "end": "14:00"}
    assert b_call["hard_close_et"] == "15:30"
    assert b_call["premium_cap_usd"] == 200.0
    assert b_call["max_entries_per_week"] == 3
    # Arm B's strike rule needs a projection; arm C needs its DTE/delta window instead.
    assert plan.cell("B", "call").watch.strike_projection["level"] == pytest.approx(
        series.bars[-1].h * 2 - series.bars[-1].l
    )
    assert plan.cell("B", "call").watch.strike_projection["provisional"] is True
    assert plan.cell("C", "call").watch.arm_criteria["dte_min"] == 90
    assert plan.cell("C", "call").watch.arm_criteria["delta_min"] == 0.80
    assert plan.cell("C", "call").watch.arm_criteria["roll_delta_below"] == 0.70


def test_arm_b_strike_projection_is_one_setup_day_range_beyond_the_extreme(series):
    bar = series.bars[-1]
    call = project_setup_day_range(bar, "call", factor=1.0)
    put = project_setup_day_range(bar, "put", factor=1.0)
    assert call.level == pytest.approx(bar.h + (bar.h - bar.l))
    assert call.sense == "above"
    assert put.level == pytest.approx(bar.l - (bar.h - bar.l))
    assert put.sense == "below"
    # A wider factor projects further out; factor 0 is the bare extreme.
    assert project_setup_day_range(bar, "call", factor=2.0).level > call.level
    assert project_setup_day_range(bar, "call", factor=0.0).level == pytest.approx(bar.h)


# ---------------------------------------------------------------------------
# trigger prices: honest, or null with a reason
# ---------------------------------------------------------------------------


def test_t1_trigger_is_the_fast_ema_when_only_the_close_is_on_the_wrong_side(calibrated):
    snapshot = passing_snapshot(close=615.0)  # under EMA50, EMA stack still aligned
    cell = cell_for(snapshot, calibrated)
    assert cell.checklist.status("T1") == "FAIL"
    trigger = next(t for t in cell.triggers if t.condition == "T1")
    assert trigger.level == 620.0
    assert trigger.sense == "above"
    assert trigger.actionable is True


def test_t1_trigger_is_null_when_the_ema_stack_itself_is_misaligned(calibrated):
    snapshot = passing_snapshot(ema_fast=560.0, ema_slow=620.0)  # inverted stack
    cell = cell_for(snapshot, calibrated)
    assert cell.checklist.status("T1") == "FAIL"
    trigger = next(t for t in cell.triggers if t.condition == "T1")
    assert trigger.level is None
    assert trigger.actionable is False
    assert "crossover" in trigger.reason


def test_t3b_trigger_is_the_band_edge_once_the_bar_tagged_it(calibrated):
    snapshot = passing_snapshot(close=619.0)  # tagged the lower band, closed outside
    cell = cell_for(snapshot, calibrated)
    trigger = next(t for t in cell.triggers if t.condition == "T3b")
    assert trigger.level == 621.0
    assert trigger.sense == "above"
    assert "closed outside" in trigger.reason


def test_t3b_trigger_is_null_when_the_band_was_never_tagged(calibrated):
    snapshot = passing_snapshot(bollinger=_bb(low=622.5))  # never touched the lower band
    cell = cell_for(snapshot, calibrated)
    trigger = next(t for t in cell.triggers if t.condition == "T3b")
    assert trigger.level is None
    assert "candle-shape" in trigger.reason


@pytest.mark.parametrize(
    ("condition", "expected_fragment"),
    [
        ("T2", "14-close statistic"),
        ("T4", "volume ratio"),
    ],
)
def test_statistics_based_conditions_get_no_invented_price_level(calibrated, condition,
                                                                  expected_fragment):
    overrides = {"relvol": 1.1} if condition == "T4" else {"rsi": 49.0}
    snapshot = passing_snapshot(**overrides)
    cell = cell_for(snapshot, calibrated)
    trigger = next(t for t in cell.triggers if t.condition == condition)
    assert trigger.level is None
    assert trigger.actionable is False
    assert expected_fragment in trigger.reason


def test_pending_conditions_get_a_null_trigger_and_say_why(rules):
    # Arm B under the shipped DRAFT rulebook: T6's multiplier is calibration_pending.
    cell = cell_for(passing_snapshot(flow_gate=FlowGate(status="unknown")), rules)
    trigger = next(t for t in cell.triggers if t.condition == "T6")
    assert cell.checklist.status("T6") == "PENDING"
    assert trigger.level is None
    assert "PENDING" in trigger.reason


def test_event_day_trigger_for_t5_says_the_calendar_not_a_price(calibrated):
    snapshot = passing_snapshot(is_event_day=True, event_kinds=("fomc",))
    cell = cell_for(snapshot, calibrated, event_day_veto=True)
    trigger = next(t for t in cell.triggers if t.condition == "T5")
    assert trigger.level is None
    assert "hard calendar veto" in trigger.reason


def test_passing_conditions_produce_no_triggers(calibrated):
    cell = cell_for(passing_snapshot(), calibrated)
    assert cell.checklist.fire is True
    assert cell.triggers == ()


def test_compute_triggers_matches_the_plan_builder(calibrated):
    snapshot = passing_snapshot(relvol=1.1)
    from executor.checklist import evaluate

    sr = snapshot_result_for(snapshot)
    result = evaluate(snapshot, calibrated, "call", "B")
    direct = compute_triggers(result, sr, calibrated, "call")
    assert direct == cell_for(snapshot, calibrated).triggers


# ---------------------------------------------------------------------------
# event day
# ---------------------------------------------------------------------------


def test_event_day_vetoes_both_directions_on_both_arms(series, rules, session_day):
    calendar = calendar_with(day=session_day, kind="fomc")
    plan = build_hunt_plan(
        data=StaticMarketData(series=series),
        rules=rules,
        day=session_day,
        calendar=calendar,
    )
    assert plan.event_day_veto is True
    assert plan.event_reasons
    assert plan.firing() == ()
    for cell in plan.arms:
        assert cell.checklist.status("T5") == "FAIL"
        assert "EVENT DAY HARD VETO" in cell.checklist.conditions["T5"].detail
        assert cell.decision_kind == "NO_TRADE"
        assert "Event-day hard veto" in cell.reasoning
        assert any("EVENT DAY HARD VETO" in r for r in cell.checklist.veto_reasons)


def test_non_veto_event_kind_does_not_veto(series, rules, session_day):
    calendar = calendar_with(day=session_day, kind="opex", label="monthly OPEX", veto=False)
    plan = build_hunt_plan(
        data=StaticMarketData(series=series),
        rules=rules,
        day=session_day,
        calendar=calendar,
    )
    assert plan.event_day_veto is False
    assert plan.cell("B", "call").checklist.status("T5") == "PENDING"  # no IV rank, but not vetoed


# ---------------------------------------------------------------------------
# IV rank plumbing
# ---------------------------------------------------------------------------


def test_iv_rank_from_the_store_satisfies_t5_for_arm_b(tmp_path, series, rules, session_day,
                                                        spot):
    store = IvRankStore.open(tmp_path / "ivrank.db", min_observations=5)
    from executor.iv_rank import tenor_key

    chain = deep_itm_chain(day=session_day, spot=spot)
    contract = min(chain.contracts, key=lambda c: abs(c.strike - spot))
    tenor = tenor_key(expiry=contract.expiry, right="call", strike=contract.strike)
    # The current reading must not be the highest in the window: rank is the share of
    # observations STRICTLY BELOW it, so seeding everything below would give rank 100
    # and fail T5 for the wrong reason. Three below, seven above -> rank 30.
    current_iv = contract.implied_volatility or 0.2
    offsets = [-0.030, -0.020, -0.010, 0.010, 0.020, 0.030, 0.040, 0.050, 0.060, 0.070]
    for i, offset in enumerate(offsets):
        store.record(
            underlying="SPY",
            as_of=(session_day - dt.timedelta(days=20 - i * 2)).isoformat(),
            tenor=tenor,
            iv=current_iv + offset,
            source="alpaca_chain",
        )
    plan = build_hunt_plan(
        data=StaticMarketData(series=series, chain=chain),
        rules=rules,
        day=session_day,
        iv_store=store,
        spot=spot,
    )
    cell = plan.cell("B", "call")
    assert cell.snapshot_dict["indicators"]["iv_rank"] is not None
    assert cell.snapshot_dict["iv_rank_provenance"]["status"] == "ok"
    # Arm B's T5 now has a rank to judge, and it is well under the frozen ceiling.
    assert cell.checklist.status("T5") == "PASS"
    store.close()


def test_iv_rank_warmup_surfaces_its_own_reason(tmp_path, series, rules, session_day, spot):
    store = IvRankStore.open(tmp_path / "ivrank.db")
    chain = deep_itm_chain(day=session_day, spot=spot)
    plan = build_hunt_plan(
        data=StaticMarketData(series=series, chain=chain),
        rules=rules,
        day=session_day,
        iv_store=store,
        spot=spot,
    )
    cell = plan.cell("B", "call")
    assert cell.snapshot_dict["indicators"]["iv_rank"] is None
    assert cell.snapshot_dict["iv_rank_provenance"]["status"] == "no_history"
    assert cell.checklist.status("T5") == "PENDING"
    store.close()


def test_no_iv_rank_is_evaluated_for_arm_c(series, rules, session_day, spot):
    """Arm C's T5 band is calibration_pending, so scoring a rank would imply a rule."""
    chain = deep_itm_chain(day=session_day, spot=spot)
    plan = build_hunt_plan(
        data=StaticMarketData(series=series, chain=chain),
        rules=rules,
        day=session_day,
        spot=spot,
    )
    cell = plan.cell("C", "call")
    assert cell.snapshot_dict["indicators"]["iv_rank"] is None
    assert cell.checklist.status("T5") == "PENDING"
    assert "calibration_pending" in cell.checklist.conditions["T5"].detail


# ---------------------------------------------------------------------------
# journaling
# ---------------------------------------------------------------------------


def test_plan_journals_one_insert_per_cell_with_the_full_snapshot(series, rules, session_day,
                                                                   conn):
    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    ids = write_hunt_plan(conn, plan)
    assert set(ids) == {("B", "call"), ("B", "put"), ("C", "call"), ("C", "put")}
    rows = list_decisions(conn)
    assert len(rows) == 4
    for row in rows:
        assert row.arm in ("B", "C")
        assert row.kind == "NO_TRADE"
        assert row.symbol == "SPY"
        assert row.strategy_version == rules.strategy_version
        assert row.references == []
        assert 1 <= row.conviction <= 10
        # The brief: ALL checklist values, not just the deciding ones.
        indicators = row.checklist_snapshot["indicators"]
        for key in ("close", "ema_fast", "ema_slow", "rsi", "macd_line", "macd_signal",
                    "macd_hist", "relvol", "iv_rank", "is_event_day", "event_kinds",
                    "flow_gate", "bollinger"):
            assert key in indicators, key
        assert len(row.checklist_state["conditions"]) == 8
        assert row.checklist_state["decision_kind"] == "NO_TRADE"
        assert "watch_levels" in row.checklist_state
        assert "trigger_prices" in row.checklist_state


def test_journal_plan_rows_are_insert_only(series, rules, session_day, conn):
    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    ids = write_hunt_plan(conn, plan)
    decision_id = ids[("B", "call")]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE decisions SET reasoning = 'tampered' WHERE id = ?", (decision_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))

    assert get_decision(conn, decision_id).reasoning.startswith("B/call: NO TRADE")


def test_a_firing_cell_journals_a_proposal_not_a_trade(calibrated, conn):
    """A firing checklist writes PROPOSAL: a contingent plan, never an order."""
    cells = [cell_for(passing_snapshot(), calibrated)]
    plan = plan_for(cells, day=dt.date(2025, 6, 2), rules=calibrated)
    assert plan.cell("B", "call").fire is True
    assert plan.firing() == (("B", "call"),)

    ids = write_hunt_plan(conn, plan)
    row = get_decision(conn, ids[("B", "call")])
    assert row.kind == "PROPOSAL"
    assert row.conviction == 10
    assert "no order placed" in row.reasoning
    assert "stale checklist entries are forbidden" in row.reasoning
    assert row.checklist_state["fire"] is True
    assert row.checklist_state["failed_conditions"] == []


def test_conviction_ranks_near_misses_above_hopeless_days(rules):
    one_fail = cell_for(passing_snapshot(relvol=1.1, flow_gate=FlowGate(status="unknown")),
                        rules)
    many_fail = cell_for(passing_snapshot(rsi=45.0, relvol=1.1, ema_fast=560.0,
                                           ema_slow=620.0), rules)
    assert len(one_fail.failed()) == 1
    assert len(many_fail.failed()) >= 3
    assert one_fail.conviction > many_fail.conviction


def test_corrections_to_a_plan_are_new_rows_referencing_the_old(series, rules, session_day,
                                                                conn):
    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    ids = write_hunt_plan(conn, plan)
    original = ids[("B", "put")]
    correction = append_decision(
        conn,
        DecisionEntry(
            ts="2025-06-02T13:00:00Z",
            arm="B",
            kind="NO_TRADE",
            checklist_snapshot=plan.cell("B", "put").snapshot_dict,
            checklist_state=plan.cell("B", "put").to_state(),
            reasoning="re-issued after the calendar file was corrected; supersedes the "
                      "earlier plan row rather than editing it",
            strategy_version=rules.strategy_version,
            symbol="SPY",
            conviction=3,
            references=[original],
        ),
    )
    assert correction != original
    assert get_decision(conn, correction).references == [original]
    assert len(list_decisions(conn)) == 5


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_unknown_arm_or_direction_is_refused(series, rules, session_day):
    data = StaticMarketData(series=series)
    with pytest.raises(ValueError):
        build_hunt_plan(data=data, rules=rules, day=session_day, arms=["A"])
    with pytest.raises(ValueError):
        build_hunt_plan(data=data, rules=rules, day=session_day, directions=["straddle"])


def test_empty_series_is_refused(rules, session_day):
    from executor.alpaca_client import BarSeries

    empty = BarSeries(symbol="SPY", timeframe="1Day", feed="sip", bars=[])
    with pytest.raises(ValueError):
        build_hunt_plan(data=StaticMarketData(series=empty), rules=rules, day=session_day)


def test_summarise_mentions_every_cell(series, rules, session_day):
    from executor.hunt_plan import summarise

    plan = build_hunt_plan(data=StaticMarketData(series=series), rules=rules, day=session_day)
    text = summarise(plan)
    for arm in ACTIVE_ARMS:
        for direction in DIRECTIONS:
            assert f"{arm}/{direction}" in text