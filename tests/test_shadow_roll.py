"""Tests for the fixed-quarterly-roll shadow sim (prediction #3's comparison arm).

The things worth pinning down:

* the calendar is mechanical — third Friday of Mar/Jun/Sep/Dec, no TA, no drift;
* lineage is the roll: a new leg supersedes the old one, and nothing is ever updated;
* the ledger's append-only claim is enforced by triggers here too, not by convention;
* and the honest limits: an empty shadow ledger is reported as "no comparison arm",
  never as a zero that would make arm C look like a winner.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
from fixtures_journal import EXPECTED, canned_journal, empty_journal

from analysis.rollup import prediction_scorecard
from analysis.shadow_roll import (
    MARK_BASIS,
    SHARES_PER_UNIT,
    ShadowRollError,
    compare_with_arm_c,
    is_quarterly_expiry,
    leg_pnl,
    mark_leg,
    next_quarterly_expiry,
    open_leg,
    quarterly_expiry,
    score_shadow_vs_arm_c,
    shadow_roll_status,
)
from journal.store import set_meta


@pytest.fixture()
def store():
    return empty_journal()


# ---------------------------------------------------------------------------
# the calendar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "quarter", "expected"),
    [
        (2026, 1, dt.date(2026, 3, 20)),   # Fridays: 6, 13, 20
        (2026, 2, dt.date(2026, 6, 19)),   # Fridays: 5, 12, 19
        (2026, 3, dt.date(2026, 9, 18)),   # Fridays: 4, 11, 18
        (2026, 4, dt.date(2026, 12, 18)),  # Fridays: 4, 11, 18
        (2025, 1, dt.date(2025, 3, 21)),
    ],
)
def test_quarterly_expiry_is_the_third_friday(year, quarter, expected):
    got = quarterly_expiry(year, quarter)
    assert got == expected
    assert got.weekday() == 4
    assert is_quarterly_expiry(got)


def test_quarterly_expiry_rejects_a_bad_quarter():
    with pytest.raises(ShadowRollError):
        quarterly_expiry(2026, 5)


def test_next_quarterly_expiry_is_strictly_after():
    assert next_quarterly_expiry(dt.date(2026, 3, 19)) == dt.date(2026, 3, 20)
    # On the expiry itself the leg still runs through it, so the next one is ahead.
    assert next_quarterly_expiry(dt.date(2026, 3, 20)) == dt.date(2026, 6, 19)
    assert next_quarterly_expiry(dt.date(2026, 12, 31)) == dt.date(2027, 3, 19)


def test_next_quarterly_expiry_rolls_the_year():
    assert next_quarterly_expiry(dt.date(2026, 12, 19)) == dt.date(2027, 3, 19)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def test_open_leg_defaults_to_the_next_quarterly_expiry(store):
    rows_before = store.execute("SELECT * FROM shadow_roll_legs").fetchall()
    leg_id = open_leg(
        store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
        strategy_version="v1.0.0",
    )
    row = store.execute("SELECT * FROM shadow_roll_legs WHERE id = ?", (leg_id,)).fetchone()
    # Opened 02 Mar: the next quarterly expiry is the 20 Mar, and the leg holds through it.
    assert row["expiry"] == "2026-03-20"
    assert row["supersedes_leg_id"] is None
    assert rows_before == []  # nothing before this
    # A leg opened after the March expiry rolls into June.
    later = open_leg(
        store, opened_on="2026-04-01", underlying_close=500.0, qty=0.2,
        strategy_version="v1.0.0",
    )
    assert store.execute(
        "SELECT expiry FROM shadow_roll_legs WHERE id = ?", (later,)
    ).fetchone()["expiry"] == "2026-06-19"


def test_mark_leg_prices_the_underlying(store):
    leg_id = open_leg(
        store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
        strategy_version="v1.0.0",
    )
    mark_leg(store, leg_id, date="2026-03-13", underlying_close=510.0)
    row = store.execute("SELECT * FROM shadow_roll_marks").fetchone()
    # (510 - 500) * 100 shares * 0.2 units = +200.00
    assert row["leg_pnl"] == pytest.approx(200.00)
    assert row["basis"] == MARK_BASIS
    assert row["date"] == "2026-03-13"


def test_leg_pnl_matches_the_notional_convention():
    assert leg_pnl(500.0, 510.0, 0.2) == pytest.approx(200.0)
    assert leg_pnl(500.0, 490.0, 0.2) == pytest.approx(-200.0)
    assert SHARES_PER_UNIT == 100.0


def test_open_leg_rejects_nonsense(store):
    with pytest.raises(ShadowRollError):
        open_leg(store, opened_on="2026-03-02", underlying_close=0.0, qty=1,
                 strategy_version="v1")
    with pytest.raises(ShadowRollError):
        open_leg(store, opened_on="2026-03-02", underlying_close=500.0, qty=-1,
                 strategy_version="v1")
    with pytest.raises(ShadowRollError):
        open_leg(store, opened_on="2026-06-19", underlying_close=500.0, qty=1,
                 expiry="2026-03-20", strategy_version="v1")
    with pytest.raises(ShadowRollError):
        open_leg(store, opened_on="2026-03-02", underlying_close=500.0, qty=1,
                 supersedes_leg_id=99, strategy_version="v1")


def test_mark_leg_needs_a_real_leg(store):
    with pytest.raises(ShadowRollError):
        mark_leg(store, 7, date="2026-03-13", underlying_close=510.0)


# ---------------------------------------------------------------------------
# the append-only claim
# ---------------------------------------------------------------------------


def test_legs_and_marks_are_immutable(store):
    leg_id = open_leg(
        store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
        strategy_version="v1",
    )
    mark_leg(store, leg_id, date="2026-03-13", underlying_close=510.0)

    with pytest.raises(sqlite3.IntegrityError, match="append-only|may not be"):
        store.execute("UPDATE shadow_roll_legs SET qty = 99 WHERE id = ?", (leg_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only|may not be"):
        store.execute("DELETE FROM shadow_roll_legs WHERE id = ?", (leg_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only|may not be"):
        store.execute("UPDATE shadow_roll_marks SET leg_pnl = 0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only|may not be"):
        store.execute("DELETE FROM shadow_roll_marks")


# ---------------------------------------------------------------------------
# lineage + status
# ---------------------------------------------------------------------------


def test_a_roll_is_a_new_leg_that_supersedes_the_old_one(store):
    first = open_leg(
        store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
        strategy_version="v1",
    )
    mark_leg(store, first, date="2026-03-20", underlying_close=520.0)
    second = open_leg(
        store, opened_on="2026-03-20", underlying_close=520.0, qty=0.2,
        strategy_version="v1", supersedes_leg_id=first,
    )
    status = shadow_roll_status(store)
    legs = {leg["leg_id"]: leg for leg in status["legs"]}

    assert legs[first]["realized"] is True
    assert legs[first]["closed_by_leg_id"] == second
    assert legs[first]["pnl_usd"] == pytest.approx(400.0)   # (520-500)*100*0.2
    assert legs[second]["realized"] is False
    assert status["open_leg_id"] == second
    assert status["leg_count"] == 2
    assert status["realized_legs"] == 1


def test_status_totals_and_return_on_capital(store):
    set_meta(store, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0})
    leg = open_leg(
        store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
        strategy_version="v1",
    )
    mark_leg(store, leg, date="2026-03-13", underlying_close=510.0)
    status = shadow_roll_status(store)

    # notional = 0.2 units * 100 shares * $500 = $10,000 (the arm bankroll)
    assert status["capital_deployed_usd"] == pytest.approx(10_000.0)
    assert status["total_pnl_usd"] == pytest.approx(200.0)
    assert status["return_on_capital"] == pytest.approx(EXPECTED["shadow_return_on_capital"])
    assert status["last_mark_date"] == "2026-03-13"
    assert status["basis"] == MARK_BASIS


def test_unmarked_leg_reports_zero_not_a_guess(store):
    open_leg(store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
             strategy_version="v1")
    status = shadow_roll_status(store)
    assert status["sufficient"] is True
    assert status["total_pnl_usd"] == 0.0        # no mark yet: zero, not a mark
    assert status["last_mark_date"] is None
    assert status["legs"][0]["last_close"] is None


# ---------------------------------------------------------------------------
# the comparison, and its honest failures
# ---------------------------------------------------------------------------


def test_empty_shadow_ledger_means_no_comparison_arm(store):
    status = shadow_roll_status(store)
    assert status["sufficient"] is False
    assert status["legs"] == []
    comparison = compare_with_arm_c(store)
    assert comparison["sufficient"] is False
    assert comparison["ta_entries_beat_fixed_roll"] is None
    assert "no shadow leg" in comparison["arm_c"]["reason"]


def test_score_shadow_vs_arm_c_without_arm_c_pnl_is_not_comparable(store):
    set_meta(store, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0})
    leg = open_leg(store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
                   strategy_version="v1")
    mark_leg(store, leg, date="2026-03-13", underlying_close=510.0)
    result = score_shadow_vs_arm_c(store, None)
    assert result["ta_entries_beat_fixed_roll"] is None
    assert "no closed P&L" in result["arm_c"]["reason"]


def test_score_shadow_vs_arm_c_compares_return_on_capital(store):
    set_meta(store, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0})
    leg = open_leg(store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
                   strategy_version="v1")
    mark_leg(store, leg, date="2026-03-13", underlying_close=510.0)
    result = score_shadow_vs_arm_c(store, 0.05)
    assert result["ta_entries_beat_fixed_roll"] is True
    assert result["delta_return_on_capital"] == pytest.approx(0.03)

    result = score_shadow_vs_arm_c(store, 0.0)
    assert result["ta_entries_beat_fixed_roll"] is False
    assert result["delta_return_on_capital"] == pytest.approx(-0.02)


def test_missing_bankroll_is_reported_not_assumed(store):
    leg = open_leg(store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
                   strategy_version="v1")
    mark_leg(store, leg, date="2026-03-13", underlying_close=510.0)
    result = score_shadow_vs_arm_c(store, 0.05)
    assert result["ta_entries_beat_fixed_roll"] is None
    assert "arm_bankroll" in result["arm_c"]["reason"]


# ---------------------------------------------------------------------------
# integration with the scorecard
# ---------------------------------------------------------------------------


def test_scorecard_prediction_3_scores_the_shadow_comparison():
    store = canned_journal()
    # Before any shadow leg: prediction 3 is falsified on "tracks A" and the roll
    # question is explicitly not yet comparable.
    p3 = prediction_scorecard(store)["predictions"][2]
    assert p3["status"] == "falsified"
    assert p3["numbers"]["ta_entries_beat_fixed_roll"] is None

    # A shadow leg that made +2% on $10k deployed while arm C made nothing: the
    # scorecard must say the TA entries TRAILED the fixed roll.
    leg = open_leg(store, opened_on="2026-03-02", underlying_close=500.0, qty=0.2,
                   strategy_version="v1.0.0")
    mark_leg(store, leg, date="2026-03-13", underlying_close=510.0)
    p3 = prediction_scorecard(store)["predictions"][2]
    assert p3["numbers"]["shadow_roll_return_on_capital"] == pytest.approx(0.02)
    assert p3["numbers"]["ta_entries_beat_fixed_roll"] is False
    assert p3["numbers"]["delta_return_on_capital"] == pytest.approx(
        EXPECTED["delta_return_on_capital"]
    )
    assert "TA entries trail" in p3["evidence"]


def test_scorecard_window_ends_on_the_latest_shadow_leg_date():
    """The shadow ledger is part of the experiment window (journal/schema.sql union)."""
    store = canned_journal()
    leg = open_leg(store, opened_on="2026-04-10", underlying_close=520.0, qty=0.2,
                   strategy_version="v1.0.0")
    mark_leg(store, leg, date="2026-04-24", underlying_close=530.0)
    card = prediction_scorecard(store)
    assert card["window"]["until"] == "2026-04-24"
