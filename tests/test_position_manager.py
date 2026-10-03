"""Phase 3b — the frozen governors, asserted as arithmetic.

Every test here states one number from ``config/rules.example.yaml`` and one number
from a position's state, and asserts the rule's verdict. Nothing reads the clock, the
network, or the journal's SQLite: ``now`` is an argument and the journal is a list.
"""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from config.loader import DEFAULT_RULES_PATH, load_example, load_rules_text
from executor.position_manager import (
    ArmState,
    ManagedPosition,
    MemoryJournalSink,
    OrderLeg,
    PositionManager,
    Veto,
    arm_a_entry,
    arm_b_entry_vetoes,
    arm_b_exits,
    arm_b_position_after,
    arm_c_exit_all_needed,
    arm_c_exits,
    arm_c_roll_replacement,
    arm_c_roll_trigger,
    entry_window_open,
    et_week_key,
    half_quantity,
    premium_within_cap,
)

ET = dt.timezone(dt.timedelta(hours=-4))


def at(hh: int, mm: int = 0, second: int = 0, day: int = 2) -> dt.datetime:
    """An aware ET timestamp on 2026-10-02 (a Friday)."""
    return dt.datetime(2026, 10, day, hh, mm, second, tzinfo=ET)


@pytest.fixture(scope="module")
def rules():
    return load_example()


def b_position(*, qty: int = 1, entry_price: float = 1.50, entry_ts: dt.datetime | None = None,
               profit_taken: bool = False) -> ManagedPosition:
    return ManagedPosition(
        arm="B",
        symbol=f"SPY261002C00630{'0'}00",
        qty=qty,
        entry_price=entry_price,
        entry_ts=entry_ts or at(10, 0),
        kind="option",
        right="call",
        expiry="20261002",
        strike=630.0,
        profit_taken=profit_taken,
    )


def c_position(*, qty: int = 1, entry_price: float = 18.0, delta: float = 0.85,
               expiry: str = "20270115") -> ManagedPosition:
    return ManagedPosition(
        arm="C",
        symbol="SPY270115C00580000",
        qty=qty,
        entry_price=entry_price,
        entry_ts=at(10, 0),
        kind="option",
        right="call",
        expiry=expiry,
        strike=580.0,
        delta=delta,
    )


# ---------------------------------------------------------------------------
# entry window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (9, 44, False),
        (9, 45, True),
        (11, 30, True),
        (14, 0, True),
        (14, 1, False),
        (15, 30, False),
    ],
)
def test_entry_window_is_0945_to_1400_et_inclusive(rules, hh, mm, expected):
    assert entry_window_open(at(hh, mm), rules) is expected


def test_entry_window_reads_et_not_utc(rules):
    """14:30 UTC is 10:30 ET — inside the window. A UTC-only read would say no."""
    assert entry_window_open(dt.datetime(2026, 10, 2, 14, 30, tzinfo=dt.UTC), rules) is True
    assert entry_window_open(dt.datetime(2026, 10, 2, 18, 1, tzinfo=dt.UTC), rules) is False


def test_a_naive_timestamp_is_refused_rather_than_assumed_et():
    with pytest.raises(ValueError):
        entry_window_open(dt.datetime(2026, 10, 2, 10, 0), load_example())


# ---------------------------------------------------------------------------
# premium cap
# ---------------------------------------------------------------------------


def test_premium_cap_blocks_a_250_contract(rules):
    """$250 > the $200 hard cap AND > 2% of a $10k bankroll."""
    assert premium_within_cap(250.0, rules) is False
    vetoes = arm_b_entry_vetoes(
        now=at(10, 30), premium_usd=250.0, rules=rules, state=ArmState()
    )
    assert [v.governor for v in vetoes] == ["premium_cap"]
    assert "$200.00 hard cap" in vetoes[0].reason


def test_premium_at_exactly_the_cap_is_allowed(rules):
    assert premium_within_cap(200.0, rules) is True
    assert premium_within_cap(200.01, rules) is False


# ---------------------------------------------------------------------------
# arm B entry governors
# ---------------------------------------------------------------------------


def test_weekly_governor_blocks_the_fourth_entry(rules):
    state = ArmState(entries_this_week=2, week_key=et_week_key(at(10, 30)))
    third = arm_b_entry_vetoes(now=at(10, 30), premium_usd=150.0, rules=rules, state=state)
    assert third == ()
    fourth = arm_b_entry_vetoes(
        now=at(11, 0),
        premium_usd=150.0,
        rules=rules,
        state=ArmState(entries_this_week=3, week_key=et_week_key(at(11, 0))),
    )
    assert [v.governor for v in fourth] == ["max_entries_per_week"]
    assert "anti-tilt governor" in fourth[0].reason


def test_the_weekly_counter_resets_on_the_iso_week(rules):
    state = ArmState(entries_this_week=3, week_key=dt.date(2026, 9, 28))
    rolled = state.with_week_rolled(at(10, 0, day=5))  # Monday of the next ISO week
    assert rolled.entries_this_week == 0
    assert rolled.week_key == dt.date(2026, 10, 5)
    same_week = state.with_week_rolled(at(10, 0))  # still 2026-09-28's week
    assert same_week.entries_this_week == 3
    assert et_week_key(dt.datetime(2026, 10, 5, 12, 0, tzinfo=ET)) == dt.date(2026, 10, 5)
    assert et_week_key(dt.datetime(2026, 10, 2, 12, 0, tzinfo=ET)) == dt.date(2026, 9, 28)


def test_max_one_concurrent_position(rules):
    state = ArmState(position=b_position())
    vetoes = arm_b_entry_vetoes(now=at(10, 30), premium_usd=100.0, rules=rules, state=state)
    assert [v.governor for v in vetoes] == ["max_concurrent"]


def test_every_veto_is_reported_not_just_the_first(rules):
    """The journal's failure histogram needs all of them, so all are collected."""
    state = ArmState(position=b_position(), entries_this_week=3, week_key=et_week_key(at(15, 0)))
    vetoes = arm_b_entry_vetoes(now=at(15, 0), premium_usd=999.0, rules=rules, state=state)
    assert {v.governor for v in vetoes} == {
        "entry_window",
        "premium_cap",
        "max_concurrent",
        "max_entries_per_week",
    }


# ---------------------------------------------------------------------------
# arm B time stop
# ---------------------------------------------------------------------------


def test_time_stop_fires_exactly_at_two_hours_when_under_thirty_percent(rules):
    position = b_position(entry_ts=at(10, 0))
    assert arm_b_exits(now=at(11, 59), price=1.50, position=position, rules=rules) == ()
    fired = arm_b_exits(now=at(12, 0), price=1.50, position=position, rules=rules)
    assert len(fired) == 1
    action = fired[0]
    assert action.kind == "STOP"
    assert action.legs[0].side == "sell" and action.legs[0].qty == 1
    assert "time stop" in action.reason


def test_time_stop_does_not_fire_at_plus_thirty_percent(rules):
    position = b_position(entry_ts=at(10, 0))
    # +30% on a $1.50 entry is $1.95; "not >= +30%" means exactly +30% holds.
    assert arm_b_exits(now=at(12, 0), price=1.95, position=position, rules=rules) == ()
    assert arm_b_exits(now=at(12, 0), price=1.94, position=position, rules=rules)


# ---------------------------------------------------------------------------
# arm B profit take
# ---------------------------------------------------------------------------


def test_profit_take_closes_half_at_plus_one_hundred_percent(rules):
    position = b_position(qty=4, entry_price=1.00, entry_ts=at(10, 0))
    fired = arm_b_exits(now=at(10, 30), price=2.00, position=position, rules=rules)
    assert len(fired) == 1
    action = fired[0]
    assert action.kind == "STOP"
    assert action.legs[0].qty == 2  # exactly half of four
    assert "profit take" in action.reason
    assert half_quantity(4) == 2


def test_profit_take_on_a_single_contract_rounds_up_to_one(rules):
    """A 0DTE arm that could hold 1 lot must still execute the rule."""
    position = b_position(qty=1, entry_price=1.00, entry_ts=at(10, 0))
    action = arm_b_exits(now=at(10, 30), price=2.00, position=position, rules=rules)[0]
    assert action.gov_checks["threshold"] == 1.0
    assert action.legs[0].qty == 1  # ceil(0.5) — the rule fires rather than rounding to 0
    assert arm_b_exits(now=at(10, 30), price=1.99, position=position, rules=rules) == ()
    assert arm_b_exits(now=at(10, 30), price=1.00, position=position, rules=rules) == ()


def test_the_profit_take_cannot_fire_twice(rules):
    position = b_position(qty=4, entry_price=1.00, entry_ts=at(10, 0), profit_taken=True)
    assert arm_b_exits(now=at(10, 30), price=3.00, position=position, rules=rules) == ()


def test_the_remainder_is_governed_by_the_time_stop_and_the_hard_close(rules):
    """``trail_remainder: true`` with no trail mechanism defined anywhere.

    The remainder is not left unmanaged: the time stop and the 15:30 hard close still
    apply, and this test is the record of that choice for the operator's ratification.
    """
    position = b_position(qty=4, entry_price=1.00, entry_ts=at(10, 0))
    take = arm_b_exits(now=at(10, 30), price=2.00, position=position, rules=rules)[0]
    remaining = arm_b_position_after(take, position)
    assert remaining is not None and remaining.qty == 2
    stopped = arm_b_exits(now=at(12, 0), price=1.20, position=remaining, rules=rules)[0]
    assert "time stop" in stopped.reason and stopped.legs[0].qty == 2
    hard = arm_b_exits(now=at(15, 30), price=1.20, position=remaining, rules=rules)[0]
    assert hard.legs[0].qty == 2 and "hard close" in hard.reason


def test_the_trail_gap_is_visible_in_the_journal_payload(rules):
    position = b_position(qty=2, entry_price=1.00, entry_ts=at(10, 0))
    action = arm_b_exits(now=at(10, 30), price=2.00, position=position, rules=rules)[0]
    assert "no trail mechanism is defined" in action.reason
    assert action.state["profit_take_trail_remainder"] is True


# ---------------------------------------------------------------------------
# arm B hard close
# ---------------------------------------------------------------------------


def test_hard_close_at_1530_overrides_everything(rules):
    """Even a position up 400% with the time stop long since passed closes in full."""
    position = b_position(qty=3, entry_price=1.00, entry_ts=at(10, 0), profit_taken=True)
    # 15:29:59 with +400% and the take already spent: nothing but the hard close is left,
    # and it has not arrived yet.
    assert arm_b_exits(now=at(15, 29, 59), price=5.00, position=position, rules=rules) == ()
    fired = arm_b_exits(now=at(15, 30), price=5.00, position=position, rules=rules)
    assert len(fired) == 1
    action = fired[0]
    assert action.legs[0].qty == 3  # ALL of it
    assert action.gov_checks == {"hard_close_et": "15:30"}
    assert "no exceptions" in action.reason


def test_hard_close_wins_over_the_profit_take(rules):
    position = b_position(qty=2, entry_price=1.00, entry_ts=at(10, 0))
    action = arm_b_exits(now=at(15, 30), price=3.00, position=position, rules=rules)[0]
    assert action.legs[0].qty == 2
    assert "hard close" in action.reason and "profit take" not in action.reason


def test_hard_close_also_wins_over_the_time_stop(rules):
    position = b_position(qty=2, entry_price=1.00, entry_ts=at(10, 0))
    action = arm_b_exits(now=at(15, 45), price=1.10, position=position, rules=rules)[0]
    assert "hard close" in action.reason


def test_nothing_is_left_open_at_expiry(rules):
    """``hold_to_expiry: false`` — at 16:00 ET there is no arm B position left."""
    position = b_position(qty=1, entry_price=1.00, entry_ts=at(10, 0))
    for _minute in (15 * 60 + 29, 15 * 60 + 30, 15 * 60 + 59, 16 * 60):
        fired = arm_b_exits(now=at(16, 0), price=1.00, position=position, rules=rules)
        assert fired and fired[0].kind == "STOP"
    assert rules.arms.B.exits.hold_to_expiry is False


# ---------------------------------------------------------------------------
# arm C rolls
# ---------------------------------------------------------------------------


def test_roll_triggers_on_delta_alone(rules):
    assert arm_c_roll_trigger(delta=0.69, dte=120, rules=rules) == "delta"


def test_roll_triggers_on_dte_alone(rules):
    assert arm_c_roll_trigger(delta=0.95, dte=44, rules=rules) == "dte"


def test_roll_triggers_when_both_are_true_and_names_both(rules):
    assert arm_c_roll_trigger(delta=0.50, dte=10, rules=rules) == "delta"
    action = arm_c_exits(
        now=at(11, 0),
        rules=rules,
        state=ArmState(position=c_position()),
        price=18.0,
        delta=0.50,
        dte=10,
        replacement=OrderLeg("SPY270715C00580000", 1, "buy", limit_price=17.5),
    )[0]
    assert action.kind == "ROLL"
    assert "delta 0.50 < 0.7" in action.reason and "dte 10 < 45" in action.reason
    assert [leg.side for leg in action.legs] == ["sell", "buy"]


def test_no_roll_at_the_thresholds(rules):
    assert arm_c_roll_trigger(delta=0.70, dte=45, rules=rules) is None


def test_a_missing_delta_is_blindness_not_a_roll_trigger(rules):
    assert arm_c_roll_trigger(delta=None, dte=120, rules=rules) is None
    assert arm_c_roll_trigger(delta=0.9, dte=None, rules=rules) is None


def test_a_roll_without_a_replacement_contract_does_not_close_the_position(rules):
    assert (
        arm_c_exits(
            now=at(11, 0),
            rules=rules,
            state=ArmState(position=c_position()),
            price=18.0,
            delta=0.60,
            dte=120,
            replacement=None,
        )
        == ()
    )


def test_roll_is_not_discretionary(rules):
    assert rules.arms.C.exits.roll.discretionary is False


# ---------------------------------------------------------------------------
# arm C roll replacement (RULED 2026-10-03, R1)
# ---------------------------------------------------------------------------


DAY = dt.date(2026, 10, 2)


def _c(dte: int, strike: float, delta: float | None, price: float, *, right: str = "call"):
    """One arm-C-shaped contract, ``dte`` calendar days out.

    ``price`` is the ASK, because that is what a buy leg pays and the premium cap is
    checked against what the roll would actually spend.
    """
    from executor.alpaca_client import Greeks, OptionContract, OptionQuote, OptionTrade

    return OptionContract(
        symbol=f"SPY{DAY + dt.timedelta(days=dte):%Y%m%d}{'C' if right == 'call' else 'P'}"
        f"{strike * 1000:08.0f}",
        underlying="SPY",
        expiry=f"{DAY + dt.timedelta(days=dte):%Y%m%d}",
        right=right,
        strike=strike,
        greeks=Greeks(delta=delta),
        implied_volatility=0.20,
        latest_quote=OptionQuote(bid=price - 0.05, ask=price),
        latest_trade=OptionTrade(p=price),
    )


def _chain(*contracts):
    from executor.alpaca_client import OptionChain

    return OptionChain(underlying="SPY", feed="opra", contracts=list(contracts))


def test_r1_picks_the_nearest_delta_080_strike_on_the_next_qualifying_expiry(rules):
    """R1: next expiry in the 90-180 band, strike nearest delta 0.80, same cap."""
    chain = _chain(
        # A nearer expiry than 90 DTE is OUTSIDE the band and must be ignored.
        _c(60, 580.0, 0.80, 2.00),
        # The next QUALIFYING expiry (100 DTE), several deltas around.
        _c(100, 560.0, 0.72, 6.00),
        _c(100, 570.0, 0.81, 7.00),  # <- nearest to 0.80
        _c(100, 580.0, 0.88, 8.00),
        _c(100, 590.0, 0.60, 9.00),
        # A further qualifying expiry must not be chosen while 100 DTE qualifies.
        _c(150, 570.0, 0.80, 11.00),
    )
    selection = arm_c_roll_replacement(
        chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
    )
    assert selection is not None
    assert selection.expiry == f"{DAY + dt.timedelta(days=100):%Y%m%d}"
    assert selection.strike == 570.0
    assert selection.delta == pytest.approx(0.81)
    assert selection.leg.side == "buy" and selection.leg.qty == 1


def test_r1_target_delta_is_read_from_the_rulebook_not_hardcoded(rules):
    """One number in the rulebook, so R1 and the entry cannot drift apart."""
    chain = _chain(_c(100, 570.0, 0.81, 7.00))
    selection = arm_c_roll_replacement(
        chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
    )
    assert selection is not None
    assert selection.target_delta == rules.arms.C.entry.delta_min


def test_r1_refuses_a_replacement_over_the_premium_cap(rules):
    """The band is the cash buffer; a roll that spends it is not this trade."""
    cap = rules.arms.C.entry.premium_pct_of_bankroll_max * rules.arms.C.bankroll_usd
    chain = _chain(_c(100, 570.0, 0.81, cap / 100.0 + 1.0))
    assert (
        arm_c_roll_replacement(
            chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
        )
        is None
    )


def test_r1_refuses_when_no_contract_in_the_band_exists(rules):
    chain = _chain(_c(30, 570.0, 0.81, 7.00), _c(250, 570.0, 0.81, 7.00))
    assert (
        arm_c_roll_replacement(
            chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
        )
        is None
    )


def test_r1_refuses_when_the_nearest_delta_still_fails_the_entry_floor(rules):
    """Rolling into a sub-0.80 contract would re-arm the roll trigger immediately."""
    chain = _chain(_c(100, 570.0, 0.65, 7.00))
    assert (
        arm_c_roll_replacement(
            chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
        )
        is None
    )


def test_r1_refuses_to_guess_when_no_greeks_are_readable(rules):
    """No delta means no way to honour "nearest delta 0.80" — so it waits."""
    chain = _chain(_c(100, 570.0, None, 7.00), _c(100, 580.0, None, 8.00))
    assert (
        arm_c_roll_replacement(
            chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
        )
        is None
    )


def test_r1_ignores_puts_even_when_they_are_a_closer_delta_match(rules):
    """Arm C is calls-only; the ruling says nothing about changing that."""
    chain = _chain(
        _c(100, 570.0, 0.80, 7.00, right="put"),  # the exact target delta...
        _c(100, 580.0, 0.82, 7.00, right="call"),  # ...but arm C is calls-only
    )
    selection = arm_c_roll_replacement(
        chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
    )
    assert selection is not None and selection.strike == 580.0


def test_a_fired_roll_trigger_with_no_replacement_now_reports_itself(rules):
    """The silence case is the one that hid a stuck position.

    Before this, an unfillable roll and a healthy position both returned an empty
    action tuple, so a position whose roll could never be satisfied was invisible.
    """
    manager = PositionManager(rules)
    evaluation = manager.evaluate_exits(
        "C",
        now=at(11, 0),
        state=ArmState(position=c_position()),
        price=18.0,
        delta=0.60,
        dte=120,
        replacement=None,
    )
    assert evaluation.actions == ()
    assert evaluation.notes
    assert "roll trigger fired" in evaluation.notes[0]
    assert "HELD, not rolled" in evaluation.notes[0]


def test_a_position_that_does_not_need_a_roll_says_nothing(rules):
    manager = PositionManager(rules)
    evaluation = manager.evaluate_exits(
        "C",
        now=at(11, 0),
        state=ArmState(position=c_position()),
        price=18.0,
        delta=0.90,
        dte=120,
        replacement=None,
    )
    assert evaluation.actions == ()
    assert evaluation.notes == ()


def test_the_roll_action_carries_r1_provenance_into_the_journal(rules):
    chain = _chain(_c(100, 570.0, 0.81, 7.00))
    selection = arm_c_roll_replacement(
        chain=chain, spot=600.0, day=DAY, rules=rules, state=ArmState(position=c_position())
    )
    action = arm_c_exits(
        now=at(11, 0),
        rules=rules,
        state=ArmState(position=c_position()),
        price=18.0,
        delta=0.60,
        dte=120,
        replacement=selection.leg,
        selection=selection,
    )[0]
    assert action.kind == "ROLL"
    assert action.gov_checks["selection"]["strike"] == 570.0
    assert "next qualifying expiry" in action.reason
    assert [leg.side for leg in action.legs] == ["sell", "buy"]


# ---------------------------------------------------------------------------
# arm C exit-all
# ---------------------------------------------------------------------------


def test_exit_all_fires_on_the_third_consecutive_eod_below_the_50ema(rules):
    assert arm_c_exit_all_needed(2, rules) is False
    assert arm_c_exit_all_needed(3, rules) is True
    assert arm_c_exit_all_needed(5, rules) is True


def test_exit_all_liquidates_to_cash_with_no_replacement_leg(rules):
    state = ArmState(position=c_position(qty=2), below_ema50_streak=3)
    action = arm_c_exits(
        now=at(15, 45), rules=rules, state=state, price=18.0, delta=0.9, dte=100
    )[0]
    assert action.kind == "STOP"
    assert [leg.side for leg in action.legs] == ["sell"]
    assert action.legs[0].qty == 2
    assert "liquidate to cash" in action.reason


def test_exit_all_outranks_a_roll(rules):
    """A position that has lost T1 does not get rolled first."""
    state = ArmState(position=c_position(), below_ema50_streak=3)
    action = arm_c_exits(
        now=at(15, 45),
        rules=rules,
        state=state,
        price=18.0,
        delta=0.50,
        dte=10,
        replacement=OrderLeg("SPY270715C00580000", 1, "buy"),
    )[0]
    assert action.kind == "STOP" and "exit-all" in action.reason


# ---------------------------------------------------------------------------
# arm A
# ---------------------------------------------------------------------------


def test_arm_a_buys_the_bankroll_once_in_whole_shares(rules):
    actions = arm_a_entry(now=at(9, 31), price=625.13, rules=rules, state=ArmState(
        bankroll_usd=10_000.0
    ))
    assert len(actions) == 1
    leg = actions[0].legs[0]
    assert leg.symbol == "SPY" and leg.side == "buy"
    assert leg.qty == int(10_000 // 625.13) == 15
    assert leg.asset_class == "us_equity"
    assert leg.qty * 625.13 <= 10_000


def test_arm_a_does_nothing_once_it_holds(rules):
    state = ArmState(
        bankroll_usd=10_000.0,
        position=ManagedPosition(
            arm="A", symbol="SPY", qty=15, entry_price=625.13, entry_ts=at(9, 31),
            kind="equity",
        ),
    )
    assert arm_a_entry(now=at(11, 0), price=630.0, rules=rules, state=state) == ()


# ---------------------------------------------------------------------------
# the manager: journal vocabulary + state transitions
# ---------------------------------------------------------------------------


def test_a_b_trade_is_journaled_as_a_trade_decision(rules):
    sink = MemoryJournalSink()
    manager = PositionManager(rules, journal=sink)
    evaluation = manager.evaluate_entry(
        "B",
        now=at(10, 30),
        state=ArmState(bankroll_usd=10_000.0),
        price=625.0,
        symbol="SPY261002C00632000",
        premium_usd=150.0,
    )
    action = evaluation.actions[0]
    assert action.kind == "TRADE"
    manager.journal_action(action, now=at(10, 30), journal=sink)
    record = sink.records[0]
    assert record.kind == "TRADE"
    assert record.arm == "B"
    assert record.ts == "2026-10-02T14:30:00Z"
    assert record.state["gov_checks"]["premium_usd_hard_cap"] == 200.0
    assert evaluation.next_state is not None
    assert evaluation.next_state.entries_this_week == 1
    assert evaluation.next_state.position is not None
    assert evaluation.next_state.position.entry_price == 1.50  # $150 / 100


def test_a_governor_veto_is_journaled_as_a_veto_decision(rules):
    sink = MemoryJournalSink()
    manager = PositionManager(rules, journal=sink)
    evaluation = manager.evaluate_entry(
        "B",
        now=at(15, 30),
        state=ArmState(bankroll_usd=10_000.0),
        price=625.0,
        symbol="SPY261002C00632000",
        premium_usd=150.0,
    )
    veto = evaluation.vetoes[0]
    manager.journal_veto(veto, now=at(15, 30), journal=sink)
    assert sink.records[0].kind == "VETO"
    assert sink.records[0].reasoning.startswith("entry_window:")
    assert evaluation.actions == ()


def test_every_enforcement_kind_maps_onto_the_store_vocabulary():
    import typing

    from journal.store import DecisionKind

    allowed = set(typing.get_args(DecisionKind))
    assert {"TRADE", "VETO", "STOP", "ROLL"} <= allowed


def test_the_execution_stack_journals_exits_without_touching_sqlite(rules):
    sink = MemoryJournalSink()
    manager = PositionManager(rules, journal=sink)
    state = ArmState(position=b_position(qty=2, entry_price=1.0, entry_ts=at(10, 0)))
    evaluation = manager.evaluate_exits("B", now=at(15, 30), price=1.1, state=state)
    action = evaluation.actions[0]
    manager.journal_action(action, now=at(15, 30), journal=sink)
    assert sink.records[0].kind == "STOP"
    assert sink.records[0].snapshot["enforcement"]["reference_price"] == 1.1
    assert sink.records[0].state["state"]["gain_pct"] == pytest.approx(0.1)


def test_pnl_uses_the_option_multiplier():
    position = b_position(qty=2, entry_price=1.50)
    assert position.pnl_usd(1.80) == pytest.approx(60.0)  # 0.30 * 2 * 100
    assert position.cost_usd() == pytest.approx(300.0)


def test_arm_c_entry_requires_a_selected_contract(rules):
    manager = PositionManager(rules)
    evaluation = manager.evaluate_entry(
        "C", now=at(11, 0), state=ArmState(bankroll_usd=10_000.0), price=625.0, symbol=""
    )
    assert evaluation.actions == ()
    assert [v.governor for v in evaluation.vetoes] == ["no_candidate"]


def test_a_calibrated_rulebook_does_not_change_the_governors(rules):
    """Governors read their numbers from the rulebook; T6 calibration is irrelevant."""
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": 2.0, "calibration_pending": False, "reason": "test",
    }
    calibrated = load_rules_text(yaml.safe_dump(data))
    position = b_position(entry_price=1.0, entry_ts=at(10, 0))
    assert arm_b_exits(now=at(12, 0), price=1.1, position=position, rules=calibrated) == (
        arm_b_exits(now=at(12, 0), price=1.1, position=position, rules=rules)
    )


def test_veto_to_dict_is_a_journal_state_payload():
    veto = Veto("B", "premium_cap", "too big", {"premium_usd": 250.0}, {"bankroll_usd": 10_000.0})
    payload = veto.to_dict()
    assert payload["kind"] == "VETO"
    assert payload["governor"] == "premium_cap"
    assert payload["state"]["bankroll_usd"] == 10_000.0
