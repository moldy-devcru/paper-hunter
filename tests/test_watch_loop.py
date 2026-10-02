"""Phase 3b — the intraday tick: stale data, exit precedence, dry run, the loop itself.

Offline by construction. Every test injects :class:`StaticWatchData` (no I/O) and
either :class:`DryRunRouter` (records, sends nothing) or a live router wired to a
:class:`MockTradingTransport` (which is a dict of canned responses, not a socket). No
test in this file can place a real order.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

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
from tests.fixtures_synthetic import series_from, synthetic_daily_payload, zero_dte_chain

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


def test_a_tick_with_no_trigger_does_nothing_and_says_so(rules, series):
    plan = plan_for(rules)
    result = run_once(
        provider=provider(rules, series, spot=600.0),
        rules=rules,
        state=state_for(rules, plan),
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
    assert results[1].quiet is True and results[2].quiet is True


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
    assert sink.records[-1].kind == "STOP"
