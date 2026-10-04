"""Phase 3b — the intraday tick: stale data, exit precedence, dry run, the loop itself.

Offline by construction. Every test injects :class:`StaticWatchData` (no I/O) and
either :class:`DryRunRouter` (records, sends nothing) or a live router wired to a
:class:`MockTradingTransport` (which is a dict of canned responses, not a socket). No
test in this file can place a real order.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pytest
import yaml

from config.loader import DEFAULT_RULES_PATH, load_rules_text
from executor import watch_loop
from executor.alpaca_trading import AlpacaTradingClient, MockTradingTransport
from executor.checklist import BollingerState, FlowGate, IndicatorSnapshot, evaluate
from executor.hunt_plan import ArmPlan, HuntPlan, TriggerPrice, WatchLevels
from executor.main import load_plan_file, write_plan_file
from executor.position_manager import (
    ArmState,
    ManagedPosition,
    MemoryJournalSink,
    PositionManager,
)
from executor.watch_loop import (
    DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    DryRunRouter,
    StaticWatchData,
    WatchLoopError,
    WatchState,
    run_loop,
    run_once,
    trigger_fired,
)
from tests.fixtures_synthetic import (
    _chain,
    make_contract,
    series_from,
    synthetic_daily_payload,
    zero_dte_chain,
)

ET = dt.timezone(dt.timedelta(hours=-4))
LONG = 260
DAY = dt.date(2026, 10, 2)


def at(hh: int, mm: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, 2, hh, mm, second, tzinfo=ET)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rules():
    """The shipped rulebook with T6 calibrated — without it arm B cannot fire at all.

    Same shape as the Phase 3a tests' ``calibrated`` fixture: one threshold changed,
    so any behaviour difference is attributable to that calibration.
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
        bollinger=BollingerState(
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
        ),
        relvol=1.8,
        iv_rank=40.0,
        flow_gate=FlowGate(status="pass", ratio=2.4, eod_confirmed=True),
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def passing_put_snapshot() -> IndicatorSnapshot:
    """The put-side mirror: close < EMA50 < EMA200, RSI < 50, MACD below signal, and a
    band rejection inverted (tagged the upper band, closed back inside)."""
    return passing_snapshot(
        ema_fast=630.0,
        ema_slow=700.0,
        rsi=42.0,
        macd_line=0.8,
        macd_signal=1.0,
        macd_hist=-0.2,
        macd_hist_prev=-0.1,
        bollinger=BollingerState(
            upper=627.0,
            middle=624.0,
            lower=620.0,
            low=618.0,
            high=628.0,
            bandwidth=0.0112,
            bandwidth_percentile=8.0,
            bandwidth_expanding=False,   # squeeze-release needs expansion upward for calls
            middle_rising=False,
            squeeze=False,
        ),
    )


def snapshot_for(direction: str) -> IndicatorSnapshot:
    return passing_snapshot() if direction == "call" else passing_put_snapshot()


def firing_cell(rules, *, direction: str = "call", level: float = 624.0,
                sense: str = "above", strike_level: float = 626.0) -> ArmPlan:
    """A plan cell that fires, with one actionable trigger level."""
    result = evaluate(snapshot_for(direction), rules, direction, "B")  # type: ignore[arg-type]
    assert result.fire is True, result.veto_reasons
    return ArmPlan(
        arm="B",
        direction=direction,
        checklist=result,
        triggers=(TriggerPrice("T1", level, sense, "FAIL", "close below the fast EMA"),),
        watch=WatchLevels(
            close=625.0,
            ema_fast=620.0,
            strike_projection={"level": strike_level, "sense": "above"},
        ),
        decision_kind="PROPOSAL",
        conviction=8,
        reasoning="fixture cell",
        snapshot_dict=result.indicators,
    )


def not_green_cell(rules, *, direction: str = "call") -> ArmPlan:
    """A plan cell whose checklist did NOT fire at plan time.

    Built by forcing a failing condition into the snapshot rather than by stubbing
    ``fire``: the ruling's "green" test reads the same field the plan writes, so a
    fixture that faked the field would not prove the field means what it claims.
    """
    snapshot = dataclasses.replace(snapshot_for(direction), rsi=10.0)
    result = evaluate(snapshot, rules, direction, "B")  # type: ignore[arg-type]
    assert result.fire is False, "fixture must not be green"
    return ArmPlan(
        arm="B",
        direction=direction,
        checklist=result,
        triggers=(),
        watch=WatchLevels(close=625.0, ema_fast=620.0, strike_projection={}),
        decision_kind="NO_TRADE",
        conviction=0,
        reasoning="fixture cell, checklist did not fire",
        snapshot_dict=result.indicators,
    )


def plan_for(rules, *cells: ArmPlan) -> HuntPlan:
    return HuntPlan(
        day=DAY,
        symbol=rules.strategy.symbol,
        generated_ts="2026-10-02T12:35:00Z",
        strategy_version=rules.strategy_version,
        config_sha256=rules.config_sha256,
        arms=tuple(cells) or (firing_cell(rules),),
        event_day_veto=False,
        event_reasons=(),
    )


def state_for(rules, plan: HuntPlan) -> WatchState:
    return WatchState.initial(day=DAY, plan=plan, rules=rules)


def b_position(**overrides) -> ManagedPosition:
    base = dict(
        arm="B",
        symbol="SPY261002C00626000",
        qty=1,
        entry_price=1.40,
        entry_ts=at(10, 0),
        kind="option",
        right="call",
        expiry="20261002",
        strike=626.0,
    )
    return ManagedPosition(**{**base, **overrides})


def provider(rules, series, *, spot=625.0, fetched_at=None, chain=None,
             option_prices=None) -> StaticWatchData:
    return StaticWatchData(
        spot=spot,
        daily=series,
        chain=chain if chain is not None else zero_dte_chain(day=DAY, spot=625.0),
        fetched_at=fetched_at or at(10, 30),
        option_prices=option_prices,
    )


# ---------------------------------------------------------------------------
# triggers
# ---------------------------------------------------------------------------


def test_a_trigger_fires_when_spot_reaches_the_level_in_the_trade_direction(rules):
    call_cell = firing_cell(rules, direction="call", level=624.0, sense="above")
    assert trigger_fired(call_cell, 624.0)
    assert trigger_fired(call_cell, 630.0)
    assert not trigger_fired(call_cell, 623.99)


def test_a_put_cell_fires_below_its_level(rules):
    put_cell = firing_cell(rules, direction="put", level=626.0, sense="below")
    assert trigger_fired(put_cell, 626.0)
    assert trigger_fired(put_cell, 600.0)
    assert not trigger_fired(put_cell, 626.01)


def test_a_cell_with_no_level_is_not_watchable(rules):
    result = evaluate(passing_snapshot(), rules, "call", "B")  # type: ignore[arg-type]
    cell = ArmPlan(
        arm="B",
        direction="call",
        checklist=result,
        triggers=(TriggerPrice("T4", None, None, "FAIL", "statistical, no level"),),
        watch=WatchLevels(close=625.0),
        decision_kind="PROPOSAL",
        conviction=8,
        reasoning="fixture",
        snapshot_dict={},
    )
    assert trigger_fired(cell, 1e9) == ()


# ---------------------------------------------------------------------------
# the quiet tick
# ---------------------------------------------------------------------------


def test_a_green_plan_arms_the_loop_on_the_entry_window_alone(rules, series):
    """R2 (2026-10-03): a green plan re-verifies intraday even with no live trigger.

    Before the ruling, a cell whose pre-market triggers had all decayed was skipped
    before any live evaluation, so a plan that was green at 08:30 became unreachable
    the moment the day's first trigger decayed. The cell here is green (``firing_cell``
    asserts ``fire is True``) and 10:30 is inside arm B's 09:45-14:00 window, so the
    loop owes it a live re-verification.
    """
    plan = plan_for(rules)
    result = run_once(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
    )
    # The checklist re-verified. T6 is PENDING with no flow gate supplied, so the
    # honest outcome is a veto naming the condition, not an entry.
    assert result.quiet is False
    assert ("B", "call") in result.reverified
    assert any("T6" in v.reason for v in result.vetoes)


def test_a_green_cell_is_reverified_once_per_session_not_every_tick(rules, series):
    """R2's throttle: one re-verification, so the veto histogram is not flooded.

    Un-throttled this writes ~1000 identical NO_TRADE rows per cell per day, which
    ``analysis.rollup.checklist_failure_histogram`` would report as a single condition
    vetoing ~99% of all decisions.
    """
    plan = plan_for(rules)
    state = state_for(rules, plan)
    first = run_once(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state,
        now=at(10, 30),
    )
    second = run_once(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state,
        now=at(10, 31),
    )
    assert ("B", "call") in first.reverified
    assert ("B", "call") not in second.reverified
    assert state.green_checked == {("B", "call")}


def test_a_green_cell_outside_the_entry_window_stays_quiet(rules, series):
    """R2 arms the loop on the ENTRY WINDOW, so 09:44 is not in it."""
    result = run_once(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state_for(rules, plan_for(rules)),
        now=at(9, 44),
    )
    assert result.quiet is True
    assert result.reverified == {}


def test_a_green_cell_after_the_entry_window_stays_quiet(rules, series):
    """14:01 is past arm B's 14:00 close — "no late hero entries"."""
    result = run_once(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state_for(rules, plan_for(rules)),
        now=at(14, 1),
    )
    assert result.quiet is True
    assert result.reverified == {}


def test_a_non_green_cell_with_no_trigger_does_nothing_and_says_so(rules, series):
    """The pre-R2 quiet tick still exists — for a cell that was never green."""
def test_arm_c_green_plan_arms_inside_its_declared_window(rules, series):
    """RULED 2026-10-03: arm C's arming window is 09:45-15:30 ET, not the session.

    The cell is green and has no live trigger (spot 625 is below its 630 level), so
    the arming path is the one under test. 15:00 used to be inside this cell's
    reach and now is not; 10:30 always was.
    """
    inside = run_once(
        provider=provider(rules, series, spot=625.0),
        rules=rules,
        state=state_for(rules, plan_for(rules, _arm_c_cell(rules))),
        now=at(10, 30),
    )
    assert ("C", "call") in inside.reverified
    assert inside.quiet is False

    late = run_once(
        provider=provider(rules, series, spot=625.0),
        rules=rules,
        state=state_for(rules, plan_for(rules, _arm_c_cell(rules))),
        now=at(15, 31),
    )
    assert ("C", "call") not in late.reverified
    assert late.quiet is True


def test_arm_c_green_plan_does_not_arm_before_0945(rules, series):
    """The opening drive is outside the window: 09:35 is not in it."""
    result = run_once(
        provider=provider(rules, series, spot=625.0),
        rules=rules,
        state=state_for(rules, plan_for(rules, _arm_c_cell(rules))),
        now=at(9, 35),
    )
    assert result.quiet is True
    assert result.reverified == {}


def test_a_roll_is_not_gated_by_the_entry_window(rules, series):
    """The entry window must not reach the exits ladder.

    Arm C is rolled at 15:45 with a live position whose delta has fallen through the
    trigger. That is an R1 roll, not a new entry, so the declared ENTRY window has no
    say in it — pinning this because the window and the roll share a module, and a
    "reasonable" future edit to window-gate the whole arm would strand the position
    until the next session with no row in the journal to explain why.
    """
    chain = _arm_c_chain(expiry="20270115", dte_ok=True, ask=7.00, delta=0.85)
    state = state_for(rules, plan_for(rules, _arm_c_cell(rules)))
    state.arms["C"] = ArmState(
        bankroll_usd=rules.arms.C.bankroll_usd,
        position=ManagedPosition(
            arm="C",
            symbol="SPY20270115C00500000",
            qty=1,
            entry_price=18.0,
            entry_ts=at(10, 0),
            kind="option",
            right="call",
            expiry="20270115",
            strike=500.0,
            delta=0.60,
        ),
    )
    result = run_once(
        provider=provider(rules, series, spot=625.0, chain=chain),
        rules=rules,
        state=state,
        now=at(15, 45),
    )
    assert result.quiet is False
    assert any(a.kind == "ROLL" for a in result.actions), [a.kind for a in result.actions]


    result = run_once(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state_for(rules, plan_for(rules, not_green_cell(rules))),
        now=at(10, 30),
    )
    assert result.quiet is True
    assert result.actions == () and result.vetoes == ()
    assert result.stale is False
    assert result.snapshot_age_seconds == 0.0


def test_a_tick_with_no_router_at_all_cannot_place_an_order(rules, series, monkeypatch):
    """No router argument → DryRunRouter → nothing leaves the process, even though the
    tick still evaluates, re-verifies and journals the whole decision path."""
    patch_reverification(monkeypatch, rules, fire=True)
    plan = plan_for(rules)
    sink = MemoryJournalSink()
    result = run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
        journal=sink,
    )
    assert [a.kind for a in result.entry_actions] == ["TRADE"]
    assert result.journaled == len(sink.records) == 2


# ---------------------------------------------------------------------------
# staleness — the brief's "stale-checklist entries are forbidden"
# ---------------------------------------------------------------------------


def test_a_stale_snapshot_refuses_the_entry(rules, series):
    plan = plan_for(rules)
    fetched = at(10, 20)  # ten minutes old
    result = run_once(
        provider=provider(rules, series, fetched_at=fetched),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
    )
    assert result.stale is True
    assert result.entry_actions == ()
    assert [v.governor for v in result.vetoes] == ["stale_snapshot"]
    assert "stale-checklist entries are forbidden" in result.vetoes[0].reason
    assert result.reverified == {}  # the checklist was never even evaluated


def test_the_staleness_budget_is_sixty_seconds_and_configurable(rules, series):
    assert DEFAULT_MAX_SNAPSHOT_AGE_SECONDS == 60.0
    plan = plan_for(rules)
    result = run_once(
        provider=provider(rules, series, fetched_at=at(10, 29, 30)),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
        max_snapshot_age_seconds=10.0,
    )
    assert result.stale is True
    fresh = run_once(
        provider=provider(rules, series, fetched_at=at(10, 29, 30)),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
        max_snapshot_age_seconds=120.0,
    )
    assert fresh.stale is False


def test_a_clock_that_moves_backwards_is_treated_as_stale(rules, series):
    plan = plan_for(rules)
    result = run_once(
        provider=provider(rules, series, fetched_at=at(10, 31)),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
    )
    assert result.stale is True


def test_staleness_blocks_entries_but_never_exits(rules, series):
    """The one asymmetry, stated as a test: an old quote cannot stop a liquidation."""
    plan = plan_for(rules)
    state = state_for(rules, plan)
    state = WatchState(
        day=state.day,
        plan=state.plan,
        arms={**state.arms, "B": ArmState(position=b_position(entry_ts=at(10, 0)))},
    )
    result = run_once(
        provider=provider(
            rules, series, fetched_at=at(9, 0), option_prices={"SPY261002C00626000": 1.20}
        ),
        rules=rules,
        state=state,
        now=at(12, 0),  # two hours in, under +30% → time stop
    )
    assert result.stale is True
    assert [a.kind for a in result.exit_actions] == ["STOP"]
    assert "time stop" in result.exit_actions[0].reason
    assert result.next_state is not None
    assert result.next_state.arms["B"].position is None


# ---------------------------------------------------------------------------
# exits before entries
# ---------------------------------------------------------------------------


def test_exits_run_before_entries_and_the_entry_governor_sees_the_flat_position(rules, series):
    """At 12:05 the time stop fires; the entry that would also have been eligible is
    then blocked by max_concurrent, which is only possible if the exit came first."""
    plan = plan_for(rules)
    state = state_for(rules, plan)
    state = WatchState(
        day=state.day,
        plan=state.plan,
        arms={**state.arms, "B": ArmState(position=b_position(entry_ts=at(10, 0)))},
    )
    result = run_once(
        provider=provider(
            rules, series, spot=630.0, option_prices={"SPY261002C00626000": 1.40}
        ),
        rules=rules,
        state=state,
        now=at(12, 5),
        max_snapshot_age_seconds=600.0,
    )
    assert [a.kind for a in result.exit_actions] == ["STOP"]
    assert result.exit_actions[0].legs[0].side == "sell"


def test_the_hard_close_fires_with_no_option_quote_at_all(rules, series):
    plan = plan_for(rules)
    state = state_for(rules, plan)
    state = WatchState(
        day=state.day,
        plan=state.plan,
        arms={**state.arms, "B": ArmState(position=b_position(entry_ts=at(10, 0)))},
    )
    data = provider(rules, series, option_prices={})
    # strip the chain so the option genuinely cannot be priced
    data.chain = None
    result = run_once(provider=data, rules=rules, state=state, now=at(15, 30))
    assert [a.kind for a in result.exit_actions] == ["STOP"]
    assert "hard close" in result.exit_actions[0].reason
    assert any("no quote" in note for note in result.notes)
    assert result.entry_actions == ()


def test_the_profit_take_leaves_the_remainder_open(rules, series):
    plan = plan_for(rules)
    state = state_for(rules, plan)
    state = WatchState(
        day=state.day,
        plan=state.plan,
        arms={**state.arms, "B": ArmState(position=b_position(qty=4, entry_price=1.40))},
    )
    result = run_once(
        provider=provider(rules, series, option_prices={"SPY261002C00626000": 2.80}),
        rules=rules,
        state=state,
        now=at(11, 0),
    )
    assert [a.legs[0].qty for a in result.exit_actions] == [2]
    remaining = result.next_state.arms["B"].position  # type: ignore[union-attr]
    assert remaining is not None and remaining.qty == 2 and remaining.profit_taken is True


# ---------------------------------------------------------------------------
# entries: re-verify live, then the governors
# ---------------------------------------------------------------------------


def patch_reverification(monkeypatch, rules, *, fire: bool):
    """Force the live re-verification verdict so the loop's own logic is under test.

    The checklist's own correctness is Phase 3a's job (tests/test_checklist.py); what is
    under test here is the loop's control flow around it.
    """
    result = evaluate(passing_snapshot(), rules, "call", "B")  # type: ignore[arg-type]
    if not fire:
        result = evaluate(passing_snapshot(rsi=48.0), rules, "call", "B")  # type: ignore[arg-type]
    monkeypatch.setattr(
        watch_loop, "reverify_cell", lambda *a, **k: result
    )
    return result


def test_a_firing_trigger_with_a_live_pass_produces_one_entry_and_journals_it(
    rules, series, monkeypatch
):
    patch_reverification(monkeypatch, rules, fire=True)
    plan = plan_for(rules)
    sink = MemoryJournalSink()
    router = DryRunRouter()
    result = run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
        router=router,
        journal=sink,
    )
    assert [a.kind for a in result.entry_actions] == ["TRADE"]
    action = result.entry_actions[0]
    assert action.legs[0].side == "buy" and action.legs[0].qty == 1
    # the strike is the first OTM contract beyond the plan's projection (626.0)
    assert action.symbol == "SPY20261002C00627000"
    kinds = [record.kind for record in sink.records]
    assert "PROPOSAL" in kinds and "TRADE" in kinds
    assert result.next_state.traded_cells == frozenset({("B", "call")})
    assert result.next_state.arms["B"].entries_this_week == 1


def test_a_firing_trigger_whose_live_checklist_fails_is_a_veto_not_a_trade(
    rules, series, monkeypatch
):
    patch_reverification(monkeypatch, rules, fire=False)
    plan = plan_for(rules)
    sink = MemoryJournalSink()
    router = DryRunRouter()
    result = run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
        router=router,
        journal=sink,
    )
    assert result.entry_actions == ()
    assert [v.governor for v in result.vetoes] == ["checklist_reverification"]
    assert router.legs == []
    assert any(record.kind == "VETO" for record in sink.records)


def test_a_traded_cell_is_not_re_entered_on_a_later_tick(rules, series, monkeypatch):
    patch_reverification(monkeypatch, rules, fire=True)
    plan = plan_for(rules)
    first = run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
    )
    state = first.next_state
    assert state is not None
    second = run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state,
        now=at(10, 31),
    )
    assert second.entry_actions == ()
    assert second.reverified == {}


def test_the_premium_cap_vetoes_an_overpriced_contract(rules, series, monkeypatch):
    """A $2.50 ask is $250 of premium — over the $200 hard cap."""
    patch_reverification(monkeypatch, rules, fire=True)
    plan = plan_for(rules)
    chain = zero_dte_chain(day=DAY, spot=625.0, ask=2.50)
    result = run_once(
        provider=provider(rules, series, spot=626.5, chain=chain),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
    )
    assert result.entry_actions == ()
    assert [v.governor for v in result.vetoes] == ["premium_cap"]


def test_the_entry_window_closes_at_1400_even_with_a_firing_trigger(rules, series, monkeypatch):
    patch_reverification(monkeypatch, rules, fire=True)
    plan = plan_for(rules)
    result = run_once(
        provider=provider(rules, series, spot=626.5, fetched_at=at(14, 30)),
        rules=rules,
        state=state_for(rules, plan),
        now=at(14, 30),
    )
    assert result.entry_actions == ()
    assert [v.governor for v in result.vetoes] == ["entry_window"]


def test_dry_run_submits_nothing_while_still_evaluating(rules, series, monkeypatch):
    patch_reverification(monkeypatch, rules, fire=True)
    plan = plan_for(rules)
    transport = MockTradingTransport({})  # every path would 404 if it were hit
    router = DryRunRouter()
    run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
        router=router,
    )
    assert transport.calls == []
    assert len(router.legs) == 1
    assert "DRY RUN" in router.describe()


def test_a_live_router_on_a_paper_account_submits_the_leg(rules, series, monkeypatch):
    """The one test that exercises the live path end to end — against a mock."""
    patch_reverification(monkeypatch, rules, fire=True)
    plan = plan_for(rules)
    transport = MockTradingTransport(
        {
            "/v2/account": {"id": "PA999", "status": "ACTIVE", "portfolio_value": "30000"},
            "/v2/orders": {"id": "o-77", "status": "accepted", "symbol": "x", "qty": 1,
                           "side": "buy", "type": "limit"},
        }
    )
    client = AlpacaTradingClient(transport=transport, key="PK", secret="SK")
    router = watch_loop.LiveOrderRouter(client, banner=False)
    result = run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state_for(rules, plan),
        now=at(10, 30),
        router=router,
    )
    assert [a.kind for a in result.entry_actions] == ["TRADE"]
    assert ("POST", "/v2/orders") in [(m, p) for m, p, _b in transport.calls]
    assert router.confirmation.account_id == "PA999"


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def test_run_loop_stops_at_the_requested_tick_count_without_real_time(rules, series):
    slept: list[float] = []
    seen: list[int] = []
    results = run_loop(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state_for(rules, plan_for(rules)),
        poll_seconds=0.01,
        max_ticks=3,
        sleep=slept.append,
        clock=lambda: at(10, 30),
        tick_hook=lambda r: seen.append(r.snapshot_age_seconds),
    )
    assert len(results) == 3
    assert slept == [0.01, 0.01, 0.01]  # sleeps between ticks, not after the last
    assert len(seen) == 3


def test_run_loop_stops_at_the_stop_time(rules, series):
    times = iter([at(15, 59), at(16, 0)])
    results = run_loop(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state_for(rules, plan_for(rules)),
        clock=lambda: next(times, at(16, 0)),
        sleep=lambda _s: None,
    )
    assert len(results) == 1


def test_a_tick_error_is_recorded_and_the_session_continues(rules, series):
    class Flaky:
        def __init__(self):
            self.calls = 0

        def watch_snapshot(self, symbol):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("alpaca HTTP 502 (simulated)")
            return provider(rules, series, spot=600.0).watch_snapshot(symbol)

        def option_price(self, symbol, *, snapshot):
            return None

    results = run_loop(
        provider=Flaky(),
        rules=rules,
        state=state_for(rules, plan_for(rules)),
        max_ticks=3,
        sleep=lambda _s: None,
        clock=lambda: at(10, 30),
    )
    assert len(results) == 3
    assert "tick error" in results[0].notes[0]
    assert "simulated" in results[0].notes[0]
    # the failure is attached to the next tick's notes so it cannot be swallowed, and
    # it is carried exactly once
    assert any("tick error" in note for note in results[1].notes)
    assert not any("tick error" in note for note in results[2].notes)
    # Tick 1 raised before reaching the entries, so R2's green-cell re-verification is
    # still owed on tick 2 — which is why tick 2 is not quiet. Tick 3 is, having spent
    # the one re-verification R2 allows per cell per session.
    assert results[1].quiet is False
    assert results[2].quiet is True


def test_a_provider_for_the_wrong_symbol_is_refused(rules, series):
    data = provider(rules, series)
    with pytest.raises(WatchLoopError):
        data.watch_snapshot("QQQ")


def test_a_provider_with_no_intraday_bars_cannot_watch(rules, series):
    from executor.watch_loop import AlpacaWatchData

    client = type(
        "C",
        (),
        {
            "get_intraday_bars": lambda self, *a, **k: type(
                "S", (), {"bars": []}
            )(),
        },
    )()
    with pytest.raises(WatchLoopError):
        AlpacaWatchData(client).watch_snapshot("SPY")


def test_the_live_provider_is_also_the_plans_data_provider(series):
    """``hunt-plan`` hands this class to ``build_hunt_plan``; the plan reads differently.

    REGRESSION 2026-10-03 — plan.service died with
    ``AttributeError: 'AlpacaWatchData' object has no attribute 'daily_series'`` on
    every run. Phase 3b introduced the class for the watch loop's ``watch_snapshot``
    interface and Phase 4a introduced the plan's ``MarketDataProvider``
    (``daily_series`` / ``option_chain``); ``cmd_hunt_plan`` wired the two together
    with nothing in between. No test caught it because every plan test injects
    ``StaticMarketData`` and no test drove the CLI past the parser.

    The ``isinstance`` check is the pin: ``MarketDataProvider`` is a
    ``runtime_checkable`` Protocol, so it verifies *method presence* — exactly the
    contract that was broken — without a live client.
    """
    from executor.hunt_plan import MarketDataProvider
    from executor.watch_loop import AlpacaWatchData

    class _Client:
        def get_daily_bars(self, symbol, *, feed, limit=None, **kwargs):
            return series

        def get_option_chain(self, symbol):
            return _chain(symbol, [])

    provider = AlpacaWatchData(_Client())
    assert isinstance(provider, MarketDataProvider)
    assert provider.daily_series("SPY") is series
    assert provider.option_chain("SPY") is not None


def test_hunt_plan_over_the_live_provider_writes_a_plan(tmp_path, monkeypatch, capsys):
    """The whole CLI seam, offline: ``cmd_hunt_plan`` + the live provider, no keys.

    Reads the same seam end to end, so the regression above cannot pass on a provider
    the plan never actually reads through.
    """
    from executor.alpaca_client import AlpacaClient
    from executor.main import build_parser, cmd_hunt_plan
    from executor.watch_loop import AlpacaWatchData

    class _Client:
        def get_daily_bars(self, symbol, *, feed, limit=None, **kwargs):
            return series_from(synthetic_daily_payload(count=LONG))

        def get_option_chain(self, symbol):
            return _chain(symbol, [])

    monkeypatch.setattr(AlpacaClient, "from_env", classmethod(lambda _cls: _Client()))
    out = tmp_path / "plan.json"
    args = build_parser().parse_args(
        [
            "hunt-plan",
            "--out",
            str(out),
            "--ivrank",
            str(tmp_path / "absent-ivrank.db"),
            "--flow-gate",
            "none",
        ]
    )
    assert cmd_hunt_plan(args, at(8, 0)) == 0
    assert out.exists()
    plan = load_plan_file(out)
    assert plan.symbol == "SPY"
    assert plan.arms, "the plan wrote no cells — the provider fed it nothing"
    assert AlpacaWatchData(_Client()).daily_series("SPY").bars


# ---------------------------------------------------------------------------
# the plan file is the loop's input contract
# ---------------------------------------------------------------------------


def test_a_plan_survives_the_json_round_trip(tmp_path, rules):
    plan = plan_for(rules)
    path = write_plan_file(plan, tmp_path / "plan.json")
    loaded = load_plan_file(path)
    assert loaded.day == plan.day
    assert loaded.strategy_version == plan.strategy_version
    cell = loaded.cell("B", "call")
    assert cell.fire is True
    assert cell.checklist.status("T4") == "PASS"
    assert [t.level for t in cell.triggers] == [624.0]
    assert cell.watch.strike_projection["level"] == 626.0
    assert trigger_fired(cell, 630.0)


def test_the_state_dict_is_serialisable(rules, series):
    result = run_once(
        provider=provider(rules, series, spot=626.5),
        rules=rules,
        state=state_for(rules, plan_for(rules)),
        now=at(10, 30),
    )
    import json

    payload = result.to_dict()
    assert json.loads(json.dumps(payload))["stale"] is False


# ---------------------------------------------------------------------------
# the manager is reachable from the loop with its own journal
# ---------------------------------------------------------------------------


def test_the_loop_can_use_a_manager_with_a_persistent_sink(rules, series):
    sink = MemoryJournalSink()
    manager = PositionManager(rules, journal=sink)
    plan = plan_for(rules)
    state = WatchState(
        day=DAY,
        plan=plan,
        arms={
            **state_for(rules, plan).arms,
            "B": ArmState(position=b_position(entry_ts=at(10, 0))),
        },
    )
    result = run_once(
        provider=provider(rules, series, spot=600.0, option_prices={"SPY261002C00626000": 1.40}),
        rules=rules,
        state=state,
        now=at(12, 0),
        manager=manager,
        journal=sink,
    )
    assert len(sink.records) == result.journaled >= 1
    # The exit ladder's STOP is journalled, and under R2 the green arm-B cell is then
    # re-verified for an entry, so an entry VETO follows it. The ordering assertion
    # that matters is "the STOP is there", not "the STOP is last".
    assert "STOP" in {r.kind for r in sink.records}


# ---------------------------------------------------------------------------
# arm C's live contract selection (found broken by the full-cycle rehearsal)
# ---------------------------------------------------------------------------


def _arm_c_chain(*, expiry: str, dte_ok: bool, ask: float, delta: float = 0.85):
    """One call in the entry band, so selection is decided by the caps, not the pool."""
    return _chain(
        "SPY",
        [
            make_contract(
                symbol=f"SPY{expiry}C00450000",
                underlying="SPY",
                expiry=expiry,
                strike=450.0,
                right="call",
                ask=ask,
                delta=delta,
            )
        ],
    )


def _arm_c_cell(rules, **criteria_overrides):
    """An arm C/call cell carrying the criteria the plan writes for the live selector."""
    criteria = {
        "right": "call",
        "dte_min": 90,
        "dte_max": 180,
        "delta_min": 0.80,
        "bankroll_usd": 10000.0,
        "premium_pct_of_bankroll_max": 0.50,
        "max_concurrent_positions": 1,
        **criteria_overrides,
    }
    return ArmPlan(
        arm="C",
        direction="call",
        checklist=evaluate(passing_snapshot(), rules, "call", "C"),
        decision_kind="NO_TRADE",
        conviction=1,
        triggers=(TriggerPrice("T1", 630.0, "above", "FAIL", "close above the fast EMA"),),
        watch=WatchLevels(close=625.0, arm_criteria=criteria),
        reasoning="fixture arm C cell for the live selector",
        snapshot_dict={},
    )


def test_the_plan_writes_the_keys_the_arm_c_selector_reads(rules, series):
    """Regression (full-cycle rehearsal, 2026-10-03): the selector read
    ``premium_pct_max`` and the plan wrote ``premium_pct_of_bankroll_max``, and neither
    side carried the bankroll — so the cap evaluated to $0 and arm C could never select
    a contract, silently. ``max_premium <= 0`` was in the skip condition, so nothing
    even complained."""
    from executor.hunt_plan import StaticMarketData, build_hunt_plan

    plan = build_hunt_plan(
        data=StaticMarketData(series=series, chain=None),
        rules=rules,
        day=DAY + dt.timedelta(days=3),
        spot=series.bars[-1].c,
    )
    cell = next(c for c in plan.arms if c.arm == "C" and c.direction == "call")
    criteria = cell.watch.arm_criteria
    assert criteria["bankroll_usd"] == rules.arms.C.bankroll_usd
    assert criteria["premium_pct_of_bankroll_max"] == rules.arms.C.entry.premium_pct_of_bankroll_max
    # ...and the attributes the selector touches exist on the dataclass at all: it read
    # ``arm_c_criteria`` (only ``to_dict()`` spells it that way), which raised
    # AttributeError on the first arm C entry attempt of every session.
    assert isinstance(cell.watch.arm_criteria, dict)
    # ...and the arm C ENTRY WINDOW the loop arms on is journalled with the cell, so a
    # row explains its own timing at the monthly review without re-reading the YAML.
    assert criteria["entry_window_et"] == {"start": "09:45", "end": "15:30"}


def test_arm_c_selects_the_cheapest_contract_inside_the_premium_cap(rules, series):
    expiry = "20270115"  # ~135 DTE from DAY — inside the 90-180 band
    chain = _arm_c_chain(expiry=expiry, dte_ok=True, ask=30.0)
    cell = _arm_c_cell(rules)
    snapshot = watch_loop.WatchSnapshot(
        fetched_at=at(10, 30), symbol="SPY", spot=625.0, daily=series, chain=chain
    )
    leg = watch_loop._arm_c_contract(cell, snapshot)
    assert leg is not None, "arm C selected nothing from a qualifying chain"
    assert leg.symbol == "SPY20270115C00450000"
    assert leg.qty == 1 and leg.side == "buy"


def test_arm_c_refuses_a_contract_over_the_premium_cap(rules, series):
    """The same call at $600 premium is over the 50% of a $10k bankroll, and must be
    refused with a reason rather than bought."""
    expiry = "20270115"
    chain = _arm_c_chain(expiry=expiry, dte_ok=True, ask=600.0)
    cell = _arm_c_cell(rules)
    snapshot = watch_loop.WatchSnapshot(
        fetched_at=at(10, 30), symbol="SPY", spot=625.0, daily=series, chain=chain
    )
    assert watch_loop._arm_c_contract(cell, snapshot) is None


def test_arm_c_selection_follows_the_rulebook_dte_band(rules, series):
    """DTE and delta are read from the plan's criteria, so changing the rulebook changes
    the selection instead of leaving 90/180/0.80 hard-coded in two places."""
    expiry = "20270115"
    chain = _arm_c_chain(expiry=expiry, dte_ok=True, ask=30.0, delta=0.50)
    tight = _arm_c_cell(rules, delta_min=0.80)
    loose = _arm_c_cell(rules, delta_min=0.40)
    snapshot = watch_loop.WatchSnapshot(
        fetched_at=at(10, 30), symbol="SPY", spot=625.0, daily=series, chain=chain
    )
    assert watch_loop._arm_c_contract(tight, snapshot) is None
    assert watch_loop._arm_c_contract(loose, snapshot) is not None


# ---------------------------------------------------------------------------
# arm B: the 0DTE assertion (outsider review 4.1)
# ---------------------------------------------------------------------------


def _unsorted_chain(day: dt.date, *, expiries: list[str], spot: float = 625.0):
    """A chain whose contract order is NOT expiry order — the bug's whole premise.

    ``OptionChain.expiries()`` sorts a copy; the chain's own ``contracts`` list keeps
    whatever order the API returned. So a chain can perfectly legally lead with a
    deferred expiry, and the old selector (``contracts[0].expiry``) would follow it.
    """
    calls: list[object] = []
    for expiry in expiries:
        for i in (1, 2):
            strike = spot + i
            calls.append(
                make_contract(
                    symbol=f"SPY{expiry}C{int(strike * 1000):08d}",
                    underlying="SPY",
                    expiry=expiry,
                    strike=strike,
                    right="call",
                    ask=1.40,
                )
            )
    return _chain("SPY", calls)


def test_arm_b_buys_the_nearest_expiry_when_the_chain_is_not_sorted_by_it(rules, series):
    """The regression this pins: expiries listed FAR-FIRST, nearest expiry last.

    ``contracts[0].expiry`` would have selected the far expiry and bought a 30-DTE
    contract while calling it 0DTE. The selector must find the nearest expiry wherever
    it sits in the list, and the contract it returns must expire today.
    """
    today = DAY.strftime("%Y%m%d")
    far = (DAY + dt.timedelta(days=30)).strftime("%Y%m%d")
    chain = _unsorted_chain(DAY, expiries=[far, today])
    assert chain.contracts[0].expiry == far, "fixture must lead with the DEFERRED expiry"
    picked = watch_loop._arm_b_contract(firing_cell(rules), _snapshot(series, chain))
    assert not isinstance(picked, watch_loop._ArmBRefusal), picked
    assert picked.expiry == today
    assert picked.dte == 0


def test_arm_b_refuses_a_chain_whose_nearest_expiry_is_not_today(rules, series):
    """No 0DTE listed → a named veto, not an n-DTE trade.

    The alternative reading — "buy the nearest expiry anyway" — is what made arm B's
    downstream claims (same-day exit, never holds to expiry, prediction 2's asset-class
    claim) silently false. The refusal carries the DTE so a reader can check it.
    """
    far = (DAY + dt.timedelta(days=30)).strftime("%Y%m%d")
    chain = _unsorted_chain(DAY, expiries=[far])
    refusal = watch_loop._arm_b_contract(firing_cell(rules), _snapshot(series, chain))
    assert isinstance(refusal, watch_loop._ArmBRefusal)
    assert refusal.governor == "not_zero_dte"
    assert refusal.checks["dte"] == 30
    assert refusal.checks["nearest_expiry"] == far
    assert "0DTE" in refusal.reason


def test_arm_b_ignores_expired_expiries_in_the_chain(rules, series):
    """A chain can carry last week's contracts. The nearest LIVE expiry is the answer,
    and a yesterday-expiry contract must never be selected."""
    today = DAY.strftime("%Y%m%d")
    yesterday = (DAY - dt.timedelta(days=1)).strftime("%Y%m%d")
    chain = _unsorted_chain(DAY, expiries=[yesterday, today])
    picked = watch_loop._arm_b_contract(firing_cell(rules), _snapshot(series, chain))
    assert not isinstance(picked, watch_loop._ArmBRefusal), picked
    assert picked.expiry == today and picked.dte == 0


def test_arm_b_with_no_chain_refuses_rather_than_raising(rules, series):
    refusal = watch_loop._arm_b_contract(firing_cell(rules), _snapshot(series, None))
    assert isinstance(refusal, watch_loop._ArmBRefusal)
    assert refusal.governor == "no_candidate"


def test_a_nearest_expiry_that_is_not_zero_dte_is_a_veto_in_the_tick(rules, series, monkeypatch):
    """End-to-end: the refusal reaches the veto stream with its own governor, so the
    NO-SHOT/veto ledger distinguishes 'no 0DTE listed' from 'nothing at the level'."""
    patch_reverification(monkeypatch, rules, fire=True)
    far = (DAY + dt.timedelta(days=30)).strftime("%Y%m%d")
    result = run_once(
        provider=provider(
            rules, series, spot=626.5, chain=_unsorted_chain(DAY, expiries=[far])
        ),
        rules=rules,
        state=state_for(rules, plan_for(rules)),
        now=at(10, 30),
    )
    assert result.entry_actions == ()
    assert [v.governor for v in result.vetoes] == ["not_zero_dte"]
    assert result.vetoes[0].checks["dte"] == 30


def _snapshot(series, chain, *, spot: float = 626.5):
    return watch_loop.WatchSnapshot(
        fetched_at=at(10, 30), symbol="SPY", spot=spot, daily=series, chain=chain
    )


# ---------------------------------------------------------------------------
# T4's run-rate branch reaches the live loop (outsider review 4.3)
# ---------------------------------------------------------------------------


def _run_rate_intraday(day: dt.date, *, sessions: int = 21, today_volume: float = 780_000.0):
    """21 sessions of 15-minute bars; today prints 2x a normal session."""
    from executor.alpaca_client import Bar, BarSeries

    bars = []
    for i in range(sessions):
        session_day = day - dt.timedelta(days=sessions - 1 - i)
        is_today = i == sessions - 1
        volume = today_volume if is_today else 390_000.0
        for j in range(26):
            stamp = dt.datetime.combine(
                session_day, dt.time(9, 30), tzinfo=ET
            ) + dt.timedelta(minutes=15 * j)
            bars.append(
                Bar(
                    t=stamp.astimezone(dt.UTC),
                    o=625.0, h=626.0, l=624.0, c=625.0,
                    v=volume / 26,
                )
            )
    return BarSeries(symbol="SPY", timeframe="15Min", feed="iex", bars=bars)


def test_the_live_loop_scores_t4_against_the_run_rate(rules, series):
    """End to end: the snapshot the loop re-verifies against carries a run rate, and T4
    reads it. Without ``intraday=`` on the snapshot there was nothing for the branch to
    read and the live path could never use it."""
    cell = firing_cell(rules)
    snapshot = watch_loop.WatchSnapshot(
        fetched_at=at(10, 30), symbol="SPY", spot=626.5, daily=series,
        chain=zero_dte_chain(day=DAY), intraday=_run_rate_intraday(DAY),
    )
    with_run_rate = watch_loop.reverify_cell(cell, snapshot=snapshot, rules=rules)
    # Same live data, no intraday series: T4 falls back to relvol only, as it always did.
    without = dataclasses.replace(snapshot, intraday=None)
    without_run_rate = watch_loop.reverify_cell(cell, snapshot=without, rules=rules)
    assert "run-rate" in with_run_rate.conditions["T4"].detail
    assert "run-rate unavailable" in without_run_rate.conditions["T4"].detail


def test_a_run_rate_can_carry_t4_on_its_own(rules, series):
    """T4 now names BOTH branches in its verdict, whichever way it went."""
    cell = firing_cell(rules)
    snapshot = watch_loop.WatchSnapshot(
        fetched_at=at(10, 30), symbol="SPY", spot=626.5, daily=series,
        chain=zero_dte_chain(day=DAY), intraday=_run_rate_intraday(DAY),
    )
    result = watch_loop.reverify_cell(cell, snapshot=snapshot, rules=rules)
    detail = result.conditions["T4"].detail
    assert "intraday run-rate" in detail


def test_a_run_rate_carries_t4_when_relvol_is_pressed(rules, series, monkeypatch):
    """The case the frozen rule was written for, pinned with a forced-low relvol.

    ``build_relvol`` divides the newest daily bar by the mean of 20 COMPLETED sessions.
    Intraday that bar is partial, so at 10:30 the relvol reading is structurally low and
    T4 could not pass on that branch alone — the whole reason the brief wrote the
    parenthetical.
    """
    import executor.snapshot_builder as sb

    real = sb.build_relvol

    def partial_day_relvol(*a, **k):
        value, reason, feed = real(*a, **k)
        return (0.6, reason, feed) if value is not None else (value, reason, feed)

    monkeypatch.setattr(sb, "build_relvol", partial_day_relvol)
    cell = firing_cell(rules)
    snapshot = watch_loop.WatchSnapshot(
        fetched_at=at(10, 30), symbol="SPY", spot=626.5, daily=series,
        chain=zero_dte_chain(day=DAY), intraday=_run_rate_intraday(DAY),
    )
    result = watch_loop.reverify_cell(cell, snapshot=snapshot, rules=rules)
    t4 = result.conditions["T4"]
    assert t4.status == "PASS"
    assert "PASS via intraday run-rate" in t4.detail


# REGRESSION 2026-10-03: the bars route answers a WINDOW, not a limit
# ---------------------------------------------------------------------------

BARS_PATH = "/v2/stocks/SPY/bars"
SNAPSHOT_PATH = "/v1beta1/options/snapshots/SPY"


def _windowed_only(payload: dict):
    """A bars route that behaves like the live one: no window means no bars.

    MEASURED 2026-10-03, paper creds, ``/v2/stocks/{symbol}/bars``: a limit-only
    ``1Day``/sip/``limit=400`` read and a limit-only ``1Min``/iex/``limit=5`` read BOTH
    answered HTTP 200 with an empty series, and the same reads with an explicit window
    answered 400 bars and 5 bars. A mock that always answers bars is precisely why this
    reached production, so this one refuses to.
    """

    def route(params: dict) -> dict:
        if not params.get("start"):
            return {"bars": [], "symbol": "SPY", "next_page_token": None}
        return payload

    return route


def _windowed_truncating(payload: dict):
    """The same route, but honouring ``limit`` the way Alpaca does.

    In the route's default ascending order the cap falls on the **newest** end, so a
    window holding more sessions than ``limit`` answers with the oldest ones. Measured:
    a 640-day window with ``limit=400`` came back with its newest bar dated 2026-08-07
    for a window ending 2026-10-03. ``sort=desc`` moves the cap to the oldest end.
    """
    bars = payload["bars"]

    def route(params: dict) -> dict:
        if not params.get("start"):
            return {"bars": [], "symbol": "SPY", "next_page_token": None}
        limit = int(params.get("limit", len(bars)))
        chosen = bars[-limit:] if params.get("sort") == "desc" else bars[:limit]
        return {"bars": list(chosen), "symbol": "SPY", "next_page_token": None}

    return route


def _spy_client(routes: dict):
    from executor.alpaca_client import AlpacaClient, MockTransport

    transport = MockTransport(routes)
    client = AlpacaClient(transport=transport, key="k-not-real", secret="s-not-real")
    return client, transport


def test_the_live_provider_asks_for_a_window_of_daily_bars():
    """``daily_series`` must send start+end — the assertion that failed for 2 days.

    REGRESSION 2026-10-03 — ``HuntPlanError: daily series has no bars — cannot build a
    hunt plan``. The provider read ``limit=400`` and no window, and the live route
    answers that with nothing. Transport-level on purpose: a stub client with a
    ``**kwargs`` catch-all would happily accept a call that never names a window, so the
    assertion has to be on the params that actually went out.
    """
    from executor.watch_loop import AlpacaWatchData

    payload = synthetic_daily_payload(count=LONG)
    client, transport = _spy_client({BARS_PATH: _windowed_only(payload)})
    series = AlpacaWatchData(client).daily_series("SPY")
    assert series.bars, "the mock's windowed route should have answered"

    _, params = transport.calls[-1]
    assert params["start"] and params["end"], f"no window on the wire: {params}"
    assert int(params["limit"]) == 400
    start = dt.datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
    assert start < end
    # The end must sit off the SIP recency gate ("subscription does not permit querying
    # recent SIP data" on the free tier), not at "now".
    assert end <= dt.datetime.now(dt.UTC) - dt.timedelta(minutes=watch_loop.SIP_END_BACKOFF_MINUTES)
    # And the window must be wide enough to hold the sessions the limit asks for, since
    # a narrow window quietly returns a shorter series than the caller requested.
    assert (end - start).days >= 400, f"window of {(end - start).days}d cannot hold 400 sessions"


def test_the_daily_signal_bar_survives_the_limit():
    """The newest bar in the series must be the newest bar in the window.

    The trap behind the trap: widening the window is not enough on its own. Alpaca caps
    the *newest* end in the default sort order, so a wide window with ``limit=400``
    answers with the 400 OLDEST sessions in it and the plan builds itself on a
    two-month-old signal bar without raising anything.
    """
    from executor.watch_loop import AlpacaWatchData

    payload = synthetic_daily_payload(count=700)
    client, _ = _spy_client({BARS_PATH: _windowed_truncating(payload)})
    series = AlpacaWatchData(client, daily_limit=400).daily_series("SPY")
    assert len(series.bars) == 400
    assert series.bars[-1].t == series_from(payload).bars[-1].t, (
        "the cap fell on the newest end — the plan's signal bar would be months stale"
    )


def test_watch_snapshot_asks_for_a_window_of_intraday_bars():
    """``1Min``/iex/``limit=5`` with no window returns 0 bars live — same bug, same route.

    Every poll would have raised ``WatchLoopError: no 1Min bars for SPY``, and the
    run-rate series below would have silently degraded to the relative-volume branch,
    which reads like a passing run rather than a missing read.
    """
    from executor.watch_loop import AlpacaWatchData

    payload = synthetic_daily_payload(count=5)
    client, transport = _spy_client(
        {BARS_PATH: _windowed_only(payload), SNAPSHOT_PATH: {"snapshots": {}}}
    )
    snapshot = AlpacaWatchData(client).watch_snapshot("SPY")
    assert snapshot.spot > 0

    reads = [params for url, params in transport.calls if url.endswith(BARS_PATH)]
    assert reads, "no reads recorded"
    for params in reads:
        assert params.get("start"), f"a windowless bars read went out: {params}"
        assert params["sort"] == "desc", f"the newest-end truncation is unhandled: {params}"
    # IEX is realtime and free, so it must not be pinned to the SIP end-clamp; a 20-minute
    # old "spot" is a stale spot.
    assert "end" not in reads[0], "IEX was asked for a window ending 20 minutes ago"


def test_a_run_rate_series_built_live_is_not_silently_empty():
    """A limit-only run-rate read degrades T4 silently, so it is worth pinning."""
    from executor.watch_loop import AlpacaWatchData

    payload = synthetic_daily_payload(count=300)
    client, _ = _spy_client({BARS_PATH: _windowed_only(payload), SNAPSHOT_PATH: {"snapshots": {}}})
    provider = AlpacaWatchData(client)
    snapshot = provider.watch_snapshot("SPY")
    assert snapshot.intraday is not None and snapshot.intraday.bars
    assert not any("run-rate series unavailable" in note for note in snapshot.notes)


def test_hunt_plan_over_a_windowed_mock_writes_a_plan(tmp_path, monkeypatch):
    """The dead seam, end to end and offline: the exact error the live run died with."""
    from executor.alpaca_client import AlpacaClient
    from executor.main import build_parser, cmd_hunt_plan

    payload = synthetic_daily_payload(count=LONG)
    client, _ = _spy_client(
        {
            BARS_PATH: _windowed_only(payload),
            SNAPSHOT_PATH: {"snapshots": {}, "next_page_token": None},
        }
    )
    monkeypatch.setattr(AlpacaClient, "from_env", classmethod(lambda _cls: client))
    out = tmp_path / "plan.json"
    args = build_parser().parse_args(
        [
            "hunt-plan",
            "--out",
            str(out),
            "--ivrank",
            str(tmp_path / "absent.db"),
            "--flow-gate",
            "none",
        ]
    )
    assert cmd_hunt_plan(args, at(8, 0)) == 0
    assert load_plan_file(out).arms


def test_bar_window_answers_only_what_a_free_tier_can():
    """The window policy itself: a floor that holds the ask, and an end off the gate."""
    now = dt.datetime(2026, 10, 2, 13, 5, tzinfo=dt.UTC)

    sip = watch_loop.bar_window(timeframe="1Day", feed="sip", limit=400, now=now)
    assert sip["sort"] == "desc"
    assert dt.datetime.fromisoformat(sip["end"].replace("Z", "+00:00")) == now - dt.timedelta(
        minutes=watch_loop.SIP_END_BACKOFF_MINUTES
    )
    iex = watch_loop.bar_window(timeframe="15Min", feed="iex", limit=700, now=now)
    assert "end" not in iex, "a realtime feed must not be pinned off the recency gate"
    # Each floor must hold the sessions its own limit asks for, plus the weekend padding.
    for window, limit, timeframe in ((sip, 400, "1Day"), (iex, 700, "15Min")):
        start = dt.datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
        per_session = watch_loop.BARS_PER_SESSION[timeframe]
        sessions_wanted = math.ceil(limit / per_session)
        assert (now - start).days * watch_loop.SESSIONS_PER_CALENDAR_DAY >= sessions_wanted, (
            f"{timeframe} floor is too narrow for {sessions_wanted} sessions"
        )
        assert (now - start).days >= watch_loop.WEEKEND_HOLIDAY_PADDING_DAYS
