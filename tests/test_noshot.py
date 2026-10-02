"""Tests for the NO-SHOT log — the hunting part (Phase 4a).

The interesting assertions are the negative ones. A NO-SHOT ledger earns its keep only
if it stays quiet when there was nothing to shoot at:

* two failed conditions is not "the sights were on", it is a bad day;
* a checklist blocked only by PENDING is blindness, not discipline;
* an event-day veto removed the whole session, so there is no sighting to record;
* a session where a position WAS taken is a trade, not a no-shot.

And one positive one that matters: a day exactly one condition away produces a row
that names the contract that would have been bought — with the premium cap checked,
because "we would have bought the $340 contract" is not the same claim as "we would
have bought it".

``counterfactual_outcome`` stays NULL on the day and is filled by a later pass; both
halves of that are tested, including that it is filled exactly once.
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
    zero_dte_chain,
)

from config.loader import DEFAULT_RULES_PATH, load_rules, load_rules_text
from executor.alpaca_client import Bar
from executor.checklist import BollingerState, FlowGate, IndicatorSnapshot
from executor.hunt_plan import (
    ArmPlan,
    HuntPlan,
    StaticMarketData,
    build_hunt_plan,
    make_arm_plan,
    write_hunt_plan,
)
from executor.noshot import (
    arm_b_hypothesis,
    arm_c_hypothesis,
    assess_sights,
    build_noshots,
    fill_counterfactuals,
    write_noshots,
)
from executor.snapshot_builder import SnapshotResult
from journal.store import get_decision, init_db, list_decisions, list_noshots

DAY = dt.date(2025, 6, 2)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rules():
    return load_rules()


@pytest.fixture(scope="module")
def calibrated():
    """T6's multiplier calibrated, so a cell can actually fire."""
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": 2.0,
        "calibration_pending": False,
        "reason": "test fixture: calibrated multiplier",
    }
    return load_rules_text(yaml.safe_dump(data))


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
        low=620.0,
        high=628.0,
        bandwidth=0.0175,
        bandwidth_percentile=10.0,
        bandwidth_expanding=True,
        middle_rising=True,
        squeeze=True,
    )
    return dataclasses.replace(base, **overrides)


def passing_snapshot(**overrides) -> IndicatorSnapshot:
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


def cell_for(snapshot, rules, *, arm="B", direction="call", prior_bar=None,
             event_day_veto=False) -> ArmPlan:
    return make_arm_plan(
        arm=arm,
        direction=direction,
        snapshot_result=SnapshotResult(snapshot=snapshot, pending_reasons=(), bars_used=260),
        rules=rules,
        prior_bar=prior_bar,
        event_day_veto=event_day_veto,
    )


def plan_for(cells, rules, *, day=DAY, event_day_veto=False) -> HuntPlan:
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


def session_bar(day=DAY, *, o=624.0, h=627.5, low=623.0, c=626.0, v=55_000_000.0) -> Bar:
    return Bar(
        t=dt.datetime.combine(day, dt.time(20, 0), tzinfo=dt.UTC),
        o=o, h=h, l=low, c=c, v=v, n=480_000, vw=c - 0.01,
    )


def get_noshot(conn, noshot_id):
    """Read one noshot row back (the store has no single-row getter yet)."""
    for entry in list_noshots(conn):
        if entry.id == noshot_id:
            return entry
    raise LookupError(noshot_id)


# ---------------------------------------------------------------------------
# "sights on" — the definition, pinned down
# ---------------------------------------------------------------------------


def test_exactly_one_failure_is_a_sighting(calibrated):
    cell = cell_for(passing_snapshot(relvol=1.1), calibrated)  # T4 the only FAIL
    assert cell.failed() == ("T4",)
    assessment = assess_sights(cell)
    assert assessment.on is True
    assert "one condition away" in assessment.reason
    assert assessment.would_fire_if_failed_cleared is True


def test_two_failures_is_not_a_sighting(calibrated):
    cell = cell_for(passing_snapshot(relvol=1.1, rsi=45.0), calibrated)
    assert set(cell.failed()) == {"T2", "T4"}
    assessment = assess_sights(cell)
    assert assessment.on is False
    assert "not a near miss" in assessment.reason


def test_pending_only_is_blindness_not_a_sighting(rules):
    # The shipped rulebook: T6 uncalibrated, so arm B is blocked without a single FAIL.
    cell = cell_for(passing_snapshot(flow_gate=FlowGate(status="unknown")), rules)
    assert cell.failed() == ()
    assert cell.pending() == ("T6",)
    assessment = assess_sights(cell)
    assert assessment.on is False
    assert "blind" in assessment.reason
    assert assessment.would_fire_if_failed_cleared is False


def test_event_day_is_not_a_sighting(calibrated):
    cell = cell_for(
        passing_snapshot(relvol=1.1, is_event_day=True, event_kinds=("fomc",)),
        calibrated,
        event_day_veto=True,
    )
    assessment = assess_sights(cell, event_day_veto=True)
    assert assessment.on is False
    assert "no sighting was taken" in assessment.reason


def test_fired_but_no_position_taken_is_a_sighting(calibrated):
    cell = cell_for(passing_snapshot(), calibrated)
    assert cell.fire is True
    assessment = assess_sights(cell, position_taken=False)
    assert assessment.on is True
    assert "shot not taken" in assessment.reason
    assert assessment.failed == ()


def test_a_taken_position_is_not_a_noshot(calibrated):
    cell = cell_for(passing_snapshot(), calibrated)
    assert assess_sights(cell, position_taken=True).on is False


def test_pending_alongside_one_fail_says_so(calibrated):
    cell = cell_for(
        passing_snapshot(relvol=1.1, flow_gate=FlowGate(status="unknown")), calibrated
    )
    assessment = assess_sights(cell)
    assert assessment.on is True
    assert "T6" in assessment.reason
    # The honest part: clearing T4 would NOT have produced a trade, because T6 blocks.
    assert assessment.would_fire_if_failed_cleared is False


def test_marginal_limit_is_configurable_and_validated(calibrated):
    cell = cell_for(passing_snapshot(relvol=1.1, rsi=45.0), calibrated)
    assert assess_sights(cell, marginal_limit=2).on is True
    with pytest.raises(ValueError):
        assess_sights(cell, marginal_limit=0)


# ---------------------------------------------------------------------------
# instrument hypotheses
# ---------------------------------------------------------------------------


def test_arm_b_hypothesis_names_the_first_otm_strike_beyond_the_projection(rules):
    bar = session_bar()          # range 4.5, so the call projection is 627.5 + 4.5 = 632.0
    chain = zero_dte_chain(day=DAY, spot=630.0, step=1.0, ask=1.40)
    hyp = arm_b_hypothesis(day=DAY, direction="call", setup_bar=bar, chain=chain, rules=rules)

    assert hyp["strike_projection"]["level"] == pytest.approx(632.0)
    assert hyp["strike_projection"]["sense"] == "above"
    # Chain calls are 631..634, so the first strike beyond 632.0 is 633.
    assert hyp["strike"] == 633.0
    assert hyp["expiry"] == DAY.strftime("%Y%m%d")
    assert hyp["dte"] == 0
    assert hyp["premium"] == 1.40
    assert hyp["premium_per_contract_usd"] == pytest.approx(140.0)
    assert hyp["premium_within_cap"] is True
    assert hyp["max_contracts"] == 1  # $200 cap / $140 per contract
    assert hyp["unavailable_reason"] is None


def test_arm_b_hypothesis_mirrors_downside_for_puts(rules):
    bar = session_bar()          # put projection: 623.0 - 4.5 = 618.5
    chain = zero_dte_chain(day=DAY, spot=621.0, step=1.0, ask=1.10)
    hyp = arm_b_hypothesis(day=DAY, direction="put", setup_bar=bar, chain=chain, rules=rules)
    assert hyp["strike_projection"]["level"] == pytest.approx(618.5)
    # Puts are 620, 619, 618 ... -> the first below 618.5 is 618.
    assert hyp["strike"] == 618.0
    assert hyp["premium_per_contract_usd"] == pytest.approx(110.0)


def test_arm_b_hypothesis_flags_a_contract_over_the_premium_cap(rules):
    bar = session_bar()
    chain = zero_dte_chain(day=DAY, spot=630.0, ask=3.40)  # $340/contract > $200 cap
    hyp = arm_b_hypothesis(day=DAY, direction="call", setup_bar=bar, chain=chain, rules=rules)
    assert hyp["strike"] == 633.0
    assert hyp["premium_within_cap"] is False
    assert hyp["max_contracts"] == 0
    assert "exceeds" in hyp["premium_cap_check"]


def test_arm_b_hypothesis_refuses_to_invent_a_strike_without_a_chain(rules):
    hyp = arm_b_hypothesis(
        day=DAY, direction="call", setup_bar=session_bar(), chain=None, rules=rules
    )
    assert hyp["strike"] is None
    assert "no options chain supplied" in hyp["unavailable_reason"]
    assert hyp["premium_cap_usd"] == 200.0


def test_arm_b_hypothesis_reports_a_chain_with_no_0dte_expiry(rules):
    from fixtures_synthetic import deep_itm_chain

    chain = deep_itm_chain(day=DAY, spot=620.0)
    hyp = arm_b_hypothesis(
        day=DAY, direction="call", setup_bar=session_bar(), chain=chain, rules=rules
    )
    assert hyp["strike"] is None
    assert "0DTE" in hyp["unavailable_reason"]


def test_arm_c_hypothesis_picks_the_cheapest_qualifying_deep_itm_call(rules):
    chain = deep_itm_chain(day=DAY, spot=625.0)
    hyp = arm_c_hypothesis(day=DAY, chain=chain, rules=rules)

    counts = hyp["selection_counts"]
    assert counts["rejected_outside_dte_window"] == 2   # 60 DTE and 200 DTE
    assert counts["rejected_below_delta"] == 1          # delta 0.55 at 120 DTE
    assert counts["qualifying"] == 2
    assert hyp["dte"] == 120
    assert hyp["delta"] >= 0.80
    # The two qualifying contracts are 615 (delta .82) and 613 (delta .85); premium
    # falls with the strike, so 613 is the cheaper one and wins.
    assert hyp["strike"] == 613.0
    assert hyp["premium"] == 8.00                       # cheapest qualifying
    assert hyp["premium_within_cap"] is True
    assert hyp["max_contracts"] == 6  # $5,000 cap / $800 per contract
    assert hyp["roll"]["delta_below"] == 0.70
    assert hyp["exit_all"]["action"] == "liquidate_to_cash"


def test_arm_c_hypothesis_says_when_nothing_qualifies(rules):
    chain = deep_itm_chain(day=DAY, spot=625.0, specs=[(120, 600.0, 0.55), (30, 610.0, 0.95)])
    hyp = arm_c_hypothesis(day=DAY, chain=chain, rules=rules)
    assert hyp["contract"] is None
    assert "delta 0.8" in hyp["unavailable_reason"]
    assert hyp["selection_counts"]["qualifying"] == 0


def test_arm_c_dte_is_measured_from_the_session_not_wall_clock(rules):
    chain = deep_itm_chain(day=DAY, spot=625.0, specs=[(90, 615.0, 0.90)])
    hyp = arm_c_hypothesis(day=DAY, chain=chain, rules=rules)
    assert hyp["dte"] == 90
    assert hyp["contract"] is not None


# ---------------------------------------------------------------------------
# the EOD pass end to end
# ---------------------------------------------------------------------------


def test_a_barely_failing_day_writes_one_noshot_with_the_hypothetical_contract(calibrated,
                                                                               conn):
    prior = Bar(t=dt.datetime(2025, 5, 30, 20, 0, tzinfo=dt.UTC),
                o=624.0, h=627.5, l=623.0, c=626.0, v=55_000_000.0)
    cell = cell_for(passing_snapshot(relvol=1.1), calibrated, prior_bar=prior)
    plan = plan_for([cell], calibrated)
    ids = write_hunt_plan(conn, plan)

    chain = zero_dte_chain(day=DAY, spot=630.0, ask=1.40)
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=chain, rules=calibrated,
        decision_ids=ids,
    )
    assert len(drafts) == 1
    written = write_noshots(conn, drafts)
    assert len(written) == 1

    row = get_noshot(conn, written[0])
    assert row.date == DAY.isoformat()
    assert row.counterfactual_outcome is None          # filled later, never up front
    assert row.counterfactual_entry_ref == ids[("B", "call")]
    assert set(row.failed_conditions) == {"T4"}
    assert row.failed_conditions["T4"]["status"] == "FAIL"
    assert "1.100x" in row.failed_conditions["T4"]["detail"]
    hyp = row.instrument_hypothesis
    assert hyp["arm"] == "B"
    assert hyp["strike"] == 633.0
    assert hyp["premium_within_cap"] is True
    assert hyp["max_contracts"] == 1
    # The full indicator snapshot rides along, not just T4.
    indicators = row.indicator_values["plan_snapshot"]["indicators"]
    assert indicators["rsi"] == 58.0
    assert indicators["relvol"] == 1.1
    assert indicators["bollinger"]["lower"] == 621.0
    assert row.indicator_values["session_bar"]["close"] == 626.0
    assert row.indicator_values["triggers"][0]["condition"] == "T4"


def test_the_session_bar_is_used_for_the_projection_not_the_prior_close(calibrated):
    """Arm B's rule is defined on the setup day's range, which only exists at EOD."""
    prior = Bar(t=dt.datetime(2025, 5, 30, 20, 0, tzinfo=dt.UTC),
                o=600.0, h=601.0, l=599.0, c=600.5, v=1.0)
    cell = cell_for(passing_snapshot(relvol=1.1), calibrated, prior_bar=prior)
    plan = plan_for([cell], calibrated)
    drafts = build_noshots(
        plan, setup_bar=session_bar(o=624.0, h=632.0, low=624.0, c=631.0), chain=None,
        rules=calibrated,
    )
    hyp = drafts[0].instrument_hypothesis
    assert hyp["strike_projection"]["setup_day_high"] == 632.0
    # range 8.0 projected one range beyond 632.0 -> 640.0
    assert hyp["strike_projection"]["level"] == pytest.approx(640.0)


def test_no_noshots_are_written_on_an_event_day(rules, conn):
    series = series_from(synthetic_daily_payload(count=260))
    calendar = calendar_with(day=DAY, kind="fomc")
    plan = build_hunt_plan(
        data=StaticMarketData(series=series),
        rules=rules,
        day=DAY,
        calendar=calendar,
    )
    assert plan.event_day_veto is True
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=None, rules=rules,
    )
    assert drafts == []
    assert write_noshots(conn, drafts) == []
    assert list_noshots(conn) == []


def test_a_taken_position_suppresses_its_own_cell(calibrated, conn):
    cell = cell_for(passing_snapshot(), calibrated)  # fires
    plan = plan_for([cell], calibrated)
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=None, rules=calibrated,
        taken=[("B", "call")],
    )
    assert drafts == []


def test_noshot_row_is_immutable_except_for_the_counterfactual(calibrated, conn):
    cell = cell_for(passing_snapshot(relvol=1.1), calibrated)
    plan = plan_for([cell], calibrated)
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=zero_dte_chain(day=DAY, spot=630.0),
        rules=calibrated,
    )
    noshot_id = write_noshots(conn, drafts)[0]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE noshots SET failed_conditions = '{}' WHERE id = ?", (noshot_id,))
    with pytest.raises(sqlite3.IntegrityError, match="may not be deleted"):
        conn.execute("DELETE FROM noshots WHERE id = ?", (noshot_id,))

    # Filling the outcome is allowed exactly once.
    series = series_from(synthetic_daily_payload(count=260))
    fill_counterfactuals(conn, [DAY + dt.timedelta(days=1)], bars=series)
    assert get_noshot(conn, noshot_id).counterfactual_outcome is not None
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE noshots SET counterfactual_outcome = '{\"rewritten\": true}' "
            "WHERE id = ?",
            (noshot_id,),
        )


# ---------------------------------------------------------------------------
# counterfactual fill
# ---------------------------------------------------------------------------


def post_day_series(day=DAY, *, days=3, start=626.0, step=2.0, volume=55_000_000.0):
    """The daily series as a later pass would see it: the sighting session, then after.

    Arm B's window closes WITH the session, so the session bar has to be in the series
    for its counterfactual to be computable at all — a series that starts the next day
    genuinely cannot answer "what did the 0DTE session do". The session bar here is the
    same shape the EOD pass saw (``session_bar`` defaults), so the two agree.
    """
    from executor.alpaca_client import BarSeries

    bars = [session_bar(day, v=volume)]
    price = start
    for i in range(days):
        bars.append(
            Bar(
                t=dt.datetime.combine(day + dt.timedelta(days=i + 1), dt.time(20, 0),
                                      tzinfo=dt.UTC),
                o=price, h=price + 1.0, l=price - 1.0, c=price + step, v=volume,
            )
        )
        price += step
    return BarSeries(symbol="SPY", timeframe="1Day", feed="sip", bars=bars)


def test_fill_counterfactuals_computes_arm_b_outcome_from_the_post_day_series(rules, conn):
    cell = cell_for(passing_snapshot(relvol=1.1), rules, arm="B", direction="call")
    plan = plan_for([cell], rules)
    drafts = build_noshots(
        plan, setup_bar=session_bar(o=624.0, h=627.5, low=623.0, c=626.0),
        chain=zero_dte_chain(day=DAY, spot=630.0, ask=1.40), rules=rules,
    )
    noshot_id = write_noshots(conn, drafts)[0]

    results = fill_counterfactuals(conn, [DAY + dt.timedelta(days=1)], bars=post_day_series())
    assert len(results) == 1
    assert results[0].computed is True
    outcome = get_noshot(conn, noshot_id).counterfactual_outcome
    assert outcome["basis"] == "underlying_session"
    assert outcome["option_pnl_modelled"] is False
    assert outcome["session"]["close"] == 626.0
    assert outcome["hypothetical_strike"] == 633.0
    assert outcome["max_favourable_excursion_pct"] == pytest.approx(3.5 / 624.0)
    assert outcome["max_adverse_excursion_pct"] == pytest.approx(1.0 / 624.0)
    assert outcome["move_from_open_pct"] == pytest.approx(2.0 / 624.0)
    assert outcome["close_beyond_projection"] is False


def test_fill_counterfactuals_computes_arm_c_delta_proxy(rules, conn):
    cell = cell_for(passing_snapshot(relvol=1.1), rules, arm="C", direction="call")
    plan = plan_for([cell], rules)
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=deep_itm_chain(day=DAY, spot=625.0), rules=rules,
    )
    hyp = drafts[0].instrument_hypothesis
    assert hyp["strike"] == 613.0
    noshot_id = write_noshots(conn, drafts)[0]

    # Post-day series starting at the sighting close (626.0) and climbing 2/session.
    bars = post_day_series(days=3, start=626.0, step=2.0)
    fill_counterfactuals(conn, [DAY + dt.timedelta(days=3)], bars=bars)
    outcome = get_noshot(conn, noshot_id).counterfactual_outcome

    assert outcome["basis"] == "underlying_delta_proxy"
    assert outcome["APPROXIMATION" if "APPROXIMATION" in outcome else "basis_note"]
    assert outcome["underlying_close_at_sighting"] == 626.0
    assert outcome["underlying_close_at_evaluation"] == 632.0
    assert outcome["underlying_move"] == 6.0
    assert outcome["contracts"] == hyp["max_contracts"]
    expected_gross = hyp["delta"] * 100.0 * 6.0
    assert outcome["gross_per_contract_usd"] == pytest.approx(expected_gross)
    assert outcome["net_per_contract_usd"] == pytest.approx(
        expected_gross - hyp["premium"] * 100.0
    )
    assert outcome["evaluated_on"] == (DAY + dt.timedelta(days=3)).isoformat()


def test_arm_c_window_stays_open_until_a_session_has_passed(rules, conn):
    cell = cell_for(passing_snapshot(relvol=1.1), rules, arm="C", direction="call")
    plan = plan_for([cell], rules)
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=deep_itm_chain(day=DAY, spot=625.0), rules=rules,
    )
    noshot_id = write_noshots(conn, drafts)[0]

    # The sighting session is known; no session after it exists yet, and arm C holds
    # 90-180 DTE, so the pass-through window is nowhere near closed.
    from executor.alpaca_client import BarSeries

    only_the_day = BarSeries(symbol="SPY", timeframe="1Day", feed="sip", bars=[session_bar()])
    results = fill_counterfactuals(conn, [DAY], bars=only_the_day)
    assert results[0].computed is False
    assert "still open" in results[0].reason
    assert get_noshot(conn, noshot_id).counterfactual_outcome is None


def test_fill_counterfactuals_without_bars_leaves_everything_null(calibrated, conn):
    cell = cell_for(passing_snapshot(relvol=1.1), calibrated)
    plan = plan_for([cell], calibrated)
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=zero_dte_chain(day=DAY, spot=630.0),
        rules=calibrated,
    )
    noshot_id = write_noshots(conn, drafts)[0]

    results = fill_counterfactuals(conn, [DAY + dt.timedelta(days=1)], bars=None)
    assert results[0].computed is False
    assert "no outcome bars" in results[0].reason
    assert get_noshot(conn, noshot_id).counterfactual_outcome is None


def test_fill_counterfactuals_can_be_run_in_dry_run(rules, conn):
    cell = cell_for(passing_snapshot(relvol=1.1), rules)
    plan = plan_for([cell], rules)
    write_noshots(
        conn,
        build_noshots(plan, setup_bar=session_bar(), chain=None, rules=rules),
    )
    results = fill_counterfactuals(
        conn, [DAY + dt.timedelta(days=1)], bars=post_day_series(), write=False
    )
    assert results[0].computed is True
    assert get_noshot(conn, results[0].noshot_id).counterfactual_outcome is None


# ---------------------------------------------------------------------------
# dry-run plumbing guard (a decision row must exist for counterfactual_entry_ref)
# ---------------------------------------------------------------------------


def test_counterfactual_ref_must_point_at_a_real_decision(calibrated, conn):
    """A NO-SHOT row claiming a decision id that does not exist is rejected by the FK."""
    cell = cell_for(passing_snapshot(relvol=1.1), calibrated)
    plan = plan_for([cell], calibrated)
    drafts = build_noshots(
        plan, setup_bar=session_bar(), chain=zero_dte_chain(day=DAY, spot=630.0),
        rules=calibrated, decision_ids={("B", "call"): 9999},
    )
    with pytest.raises(sqlite3.IntegrityError):
        write_noshots(conn, drafts)


def test_decision_row_survives_the_noshot_pass(calibrated, conn):
    """The NO-SHOT must not touch the decision ledger: no updates, no new rows."""
    cell = cell_for(passing_snapshot(relvol=1.1), calibrated)
    plan = plan_for([cell], calibrated)
    ids = write_hunt_plan(conn, plan)
    write_noshots(
        conn,
        build_noshots(plan, setup_bar=session_bar(),
                      chain=zero_dte_chain(day=DAY, spot=630.0),
                      rules=calibrated, decision_ids=ids),
    )
    rows = list_decisions(conn)
    assert len(rows) == 1
    assert get_decision(conn, ids[("B", "call")]).kind == "NO_TRADE"