"""Session-start state seeding (executor/state_seed.py).

Born 2026-11-19 from the pre-window ops check: the live CLI started every
session with empty ArmState, so under v2's fire frequency arm C could stack
duplicate multi-day positions and arm B's weekly anti-tilt counter reset every
morning. These tests pin the broker-truth classification and the journal-backed
counter, including every fail-closed refusal.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from config.loader import load_rules
from executor.alpaca_client import Position
from executor.state_seed import StateSeedError, seed_arm_states, seeding_summary
from journal.store import DecisionEntry, append_decision, init_db

ET = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 11, 19, 10, 0, tzinfo=ET)  # a Thursday


def occ(days_out: int, right: str = "C", strike: int = 650_000) -> str:
    expiry = (NOW.date() + dt.timedelta(days=days_out)).strftime("%y%m%d")
    return f"SPY{expiry}{right}{strike:08d}"


def broker_pos(symbol: str, *, qty: float = 1, side: str = "long",
               entry: float | None = 5.0, asset: str = "us_option") -> Position:
    return Position(
        symbol=symbol, qty=qty, side=side, market_value=qty * (entry or 0) * 100,
        cost_basis=None, avg_entry_price=entry, current_price=entry,
        unrealized_pl=0.0, asset_class=asset,
    )


@pytest.fixture()
def rules():
    return load_rules()


def test_flat_broker_seeds_empty_states(rules):
    arms = seed_arm_states(broker_positions=[], rules=rules, now=NOW)
    assert all(arms[a].position is None for a in ("A", "B", "C"))
    assert arms["B"].entries_this_week == 0
    assert arms["A"].bankroll_usd == rules.arms.A.bankroll_usd


def test_spy_shares_seed_arm_a(rules):
    arms = seed_arm_states(
        broker_positions=[broker_pos("SPY", qty=13, entry=757.0, asset="us_equity")],
        rules=rules, now=NOW,
    )
    pos = arms["A"].position
    assert pos is not None and pos.kind == "equity" and pos.qty == 13
    assert pos.entry_price == 757.0
    assert arms["B"].position is None and arms["C"].position is None


def test_zero_dte_option_seeds_arm_b(rules):
    arms = seed_arm_states(
        broker_positions=[broker_pos(occ(0), qty=2, entry=1.25)], rules=rules, now=NOW
    )
    pos = arms["B"].position
    assert pos is not None and pos.kind == "option" and pos.qty == 2
    assert pos.right == "call" and pos.strike == 650.0
    assert pos.expiry == NOW.strftime("%Y%m%d")
    assert arms["C"].position is None


def test_long_dated_option_seeds_arm_c_with_parsed_fields(rules):
    arms = seed_arm_states(
        broker_positions=[broker_pos(occ(120, right="P", strike=800_000), entry=42.0)],
        rules=rules, now=NOW,
    )
    pos = arms["C"].position
    assert pos is not None and pos.right == "put" and pos.strike == 800.0
    assert pos.entry_price == 42.0
    assert pos.delta is None  # live Greeks come from the chain per tick
    assert arms["B"].position is None


def test_dte_in_the_gap_is_refused(rules):
    with pytest.raises(StateSeedError, match="gap"):
        seed_arm_states(broker_positions=[broker_pos(occ(10))], rules=rules, now=NOW)


def test_unexpected_equity_is_refused(rules):
    with pytest.raises(StateSeedError, match="unexpected equity"):
        seed_arm_states(
            broker_positions=[broker_pos("AAPL", asset="us_equity")], rules=rules, now=NOW
        )


def test_option_on_another_underlying_is_refused(rules):
    bad = occ(120).replace("SPY", "QQQ", 1)
    with pytest.raises(StateSeedError, match="not SPY"):
        seed_arm_states(broker_positions=[broker_pos(bad)], rules=rules, now=NOW)


def test_a_short_position_is_refused(rules):
    with pytest.raises(StateSeedError, match="long-only"):
        seed_arm_states(
            broker_positions=[broker_pos(occ(120), side="short")], rules=rules, now=NOW
        )


def test_fractional_qty_is_refused(rules):
    with pytest.raises(StateSeedError, match="fractional"):
        seed_arm_states(
            broker_positions=[broker_pos(occ(120), qty=1.5)], rules=rules, now=NOW
        )


def test_missing_entry_price_is_refused(rules):
    with pytest.raises(StateSeedError, match="avg_entry_price"):
        seed_arm_states(
            broker_positions=[broker_pos(occ(120), entry=None)], rules=rules, now=NOW
        )


def test_two_positions_in_one_arm_is_refused(rules):
    with pytest.raises(StateSeedError, match="more than one"):
        seed_arm_states(
            broker_positions=[broker_pos(occ(120)), broker_pos(occ(150))],
            rules=rules, now=NOW,
        )


def test_unknown_asset_class_is_refused(rules):
    with pytest.raises(StateSeedError, match="asset_class"):
        seed_arm_states(
            broker_positions=[broker_pos("BTCUSD", asset="crypto")], rules=rules, now=NOW
        )


def _decision(ts: dt.datetime, kind: str = "TRADE", arm: str = "B") -> DecisionEntry:
    return DecisionEntry(
        ts=ts.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        arm=arm, kind=kind, symbol="SPY", checklist_snapshot={}, checklist_state={},
        reasoning="test", strategy_version="2.0.0", references=[],
    )


def test_weekly_counter_counts_this_weeks_b_trades_only(rules, tmp_path):
    conn = init_db(tmp_path / "j.db")
    monday = dt.datetime(2026, 11, 16, 9, 45, tzinfo=ET)
    append_decision(conn, _decision(monday))                      # this week, counts
    append_decision(conn, _decision(NOW))                         # this week, counts
    append_decision(conn, _decision(monday, kind="ROLL"))         # roll, not an entry
    append_decision(conn, _decision(monday, arm="C"))             # other arm
    append_decision(conn, _decision(dt.datetime(2026, 11, 13, 10, 0, tzinfo=ET)))  # last week
    arms = seed_arm_states(broker_positions=[], rules=rules, now=NOW, conn=conn)
    assert arms["B"].entries_this_week == 2
    assert arms["B"].week_key == dt.date(2026, 11, 16)


def test_no_conn_means_no_counter(rules):
    arms = seed_arm_states(broker_positions=[], rules=rules, now=NOW, conn=None)
    assert arms["B"].entries_this_week == 0
    assert arms["B"].week_key is None


def test_seeding_summary_names_held_positions(rules):
    arms = seed_arm_states(
        broker_positions=[
            broker_pos("SPY", qty=13, entry=757.0, asset="us_equity"),
            broker_pos(occ(120), qty=1),
        ],
        rules=rules, now=NOW,
    )
    line = seeding_summary(arms)
    assert "A:SPYx13" in line and "C:SPY" in line and "B entries this week: 0" in line
    assert "flat" in seeding_summary(
        seed_arm_states(broker_positions=[], rules=rules, now=NOW)
    )
