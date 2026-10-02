"""Tests for the weekly rollup, the counterfactual ledger and the scorecard.

Three properties matter more than the arithmetic here, and each has a test that fails
if it breaks:

1. **Exact numbers.** The canned two-week journal has hand-computed expected P&L,
   counts, histogram and counterfactual totals (``fixtures_journal.EXPECTED``). If the
   analysis changes a number, a test says so.
2. **Insufficient data is a value, not a crash.** An empty journal must return explicit
   "not scorable" states everywhere, never a zero masquerading as a result.
3. **Bases never get mixed.** Exception-path P&L is not in the A/B/C comparison; the
   proxy counterfactual is not added to the unmodelled one; PENDING is not a veto; and
   the T3 OR-group counts once per row, not twice.

Python 3.12+, stdlib + pytest.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fixtures_journal import (
    EXPECTED,
    WEEK1_END,
    WEEK1_START,
    WEEK_END,
    WEEK_START,
    canned_journal,
    empty_journal,
)

from analysis.rollup import (
    CONDITION_GROUPS,
    EXPECTED_TRADES_PER_MONTH,
    arm_pnl,
    checklist_failure_histogram,
    counterfactual_delta,
    monthly_review_bullets,
    prediction_scorecard,
    trade_counts,
    weekly_rollup_text,
)
from journal.store import Position, close_position, open_position, set_meta


@pytest.fixture()
def store():
    return canned_journal()


# ---------------------------------------------------------------------------
# 1. arm_pnl
# ---------------------------------------------------------------------------


def test_arm_pnl_matches_hand_computed_positions(store):
    result = arm_pnl(store, "2026-03-02", "2026-03-13")

    assert result["sufficient"] is True
    for arm, expected in EXPECTED["pnl"].items():
        assert result["arms"][arm]["realized_pnl"] == pytest.approx(expected)
    for arm, expected in EXPECTED["closed"].items():
        assert result["arms"][arm]["closed"] == expected
    for arm, expected in EXPECTED["open"].items():
        assert result["arms"][arm]["open"] == expected


def test_arm_pnl_reports_relative_to_control(store):
    result = arm_pnl(store, "2026-03-02", "2026-03-13")
    assert result["control_arm"] == "A"
    for arm, expected in EXPECTED["vs_control"].items():
        assert result["arms"][arm]["vs_control"] == pytest.approx(expected)
    assert result["arms"]["A"]["return_on_bankroll"] == pytest.approx(0.02)
    assert result["arms"]["B"]["return_on_bankroll"] == pytest.approx(-0.0005)


def test_exception_path_is_a_separate_bucket_never_summed_in(store):
    result = arm_pnl(store, "2026-03-02", "2026-03-13")
    exc = result["exception_path"]

    # It is present, with its own P&L...
    assert exc["realized_pnl"] == pytest.approx(20.00)
    assert "SEPARATELY" in exc["note"]
    # ...and it is not inside the arms that get compared, nor folded into any total.
    assert result["arms"]["EXCEPTION"]["realized_pnl"] == pytest.approx(20.00)
    active = [result["arms"][a]["realized_pnl"] for a in ("A", "B", "C")]
    assert sum(active) == pytest.approx(195.00)  # 200 - 5 + 0, the exception excluded
    assert "total" not in result["arms"]


def test_arm_pnl_window_excludes_out_of_window_exits(store):
    # B's only position closed 2026-03-05; a window starting 2026-03-06 has no B P&L.
    result = arm_pnl(store, "2026-03-06", "2026-03-13")
    assert result["arms"]["B"]["realized_pnl"] == 0.0
    assert result["arms"]["B"]["closed"] == 0
    assert result["arms"]["A"]["realized_pnl"] == pytest.approx(200.00)


def test_arm_pnl_missing_pnl_is_counted_not_zeroed():
    store = empty_journal()
    set_meta(store, "arm_bankroll", {"A": 10_000.0})
    pid = open_position(
        store,
        Position(arm="A", symbol="SPY", entry_ts="2026-03-02T13:30:00Z",
                 entry_price=500.0, qty=10),
    )
    close_position(store, pid, exit_ts="2026-03-13T19:00:00Z", exit_price=510.0, pnl=0.0)
    # positions are a convenience view, not the immutable ledger, so a NULL pnl is
    # reachable (e.g. a settlement pass that wrote no figure). It must be counted as
    # missing rather than folded into the sum as a zero.
    store.execute("UPDATE positions SET pnl = NULL WHERE id = ?", (pid,))
    store.commit()

    result = arm_pnl(store, "2026-03-02", "2026-03-13")
    assert result["arms"]["A"]["realized_pnl"] == 0.0
    assert result["arms"]["A"]["closed"] == 1
    assert result["arms"]["A"]["missing_pnl"] == 1


# ---------------------------------------------------------------------------
# 2. trade_counts
# ---------------------------------------------------------------------------


def test_trade_counts_per_arm(store):
    result = trade_counts(store, "2026-03-02", "2026-03-13")
    for arm, expected in EXPECTED["counts"].items():
        got = {k: v for k, v in result["by_arm"][arm].items() if k != "arm"}
        assert got == expected


def test_combined_active_arms_and_frequency(store):
    result = trade_counts(store, "2026-03-02", "2026-03-13")
    combined = result["combined_active_arms"]
    assert combined["trades"] == EXPECTED["combined_trades"]
    assert combined["noshots"] == 4
    assert combined["days"] == EXPECTED["window_days"]
    assert combined["trades_per_month"] == pytest.approx(EXPECTED["trades_per_month"])
    # 3 trades in 12 days is a weekly-ish cadence: over the pre-registered bar.
    assert combined["frequency_under_two_per_month"] is False


def test_trade_counts_on_empty_journal_is_not_a_crash():
    result = trade_counts(empty_journal())
    assert result["sufficient"] is False
    assert result["combined_active_arms"]["trades"] == 0
    assert result["combined_active_arms"]["trades_per_month"] is None
    assert result["combined_active_arms"]["frequency_under_two_per_month"] is None


# ---------------------------------------------------------------------------
# 3. checklist-failure histogram
# ---------------------------------------------------------------------------


def test_histogram_counts_match_hand_computed_vetoes(store):
    result = checklist_failure_histogram(store, "2026-03-02", "2026-03-13")
    assert result["by_condition"] == EXPECTED["histogram"]
    assert result["by_arm"] == EXPECTED["histogram_by_arm"]
    assert result["sources"] == {"no_trade_decisions": 4, "noshots": 4}


def test_t3_group_is_counted_once_per_row(store):
    """T3 is an OR group; four rows carry a T3 group veto, and it must be four."""
    import json

    result = checklist_failure_histogram(store, "2026-03-02", "2026-03-13")
    assert result["by_condition"]["T3"] == 4

    # And the raw rows really do contain both T3a and T3b — the collapse is the
    # analysis's doing, not an artefact of the fixture. Three NO_TRADE decisions
    # carry a blocking T3a FAIL (the group veto) plus one noshot row does.
    raw = store.execute(
        "SELECT checklist_state FROM decisions WHERE kind = 'NO_TRADE'"
    ).fetchall()
    group_vetoes = 0
    for (blob,) in raw:
        conditions = json.loads(blob)["conditions"]
        t3a = conditions["T3a"]
        if t3a["status"] == "FAIL" and t3a["blocking"]:
            group_vetoes += 1
        assert "T3b" in conditions  # both ids are in every stored snapshot
    assert group_vetoes == 3
    assert CONDITION_GROUPS["T3a"] == CONDITION_GROUPS["T3b"] == "T3"
    # 3 decision rows + 1 noshot row = 4, which is what the histogram reports.
    assert result["by_condition"]["T3"] == group_vetoes + 1


def test_pending_is_not_a_veto(store):
    result = checklist_failure_histogram(store, "2026-03-02", "2026-03-13")
    # T6 was PENDING on arm B's 03-02, 03-03 and 03-06 plans. An uncalibrated gate
    # blinded the system; it did not veto a setup, so it gets its own column.
    assert "T6" not in result["by_condition"]
    assert result["pending_by_condition"]["T6"] == 3
    assert result["pending_by_condition"]["T5"] == 1
    assert result["pending_by_condition"]["T3"] == 1


def test_histogram_folds_t3_via_sibling_only():
    """A row where T3a fails but T3b passed must NOT count as a T3 veto."""
    from analysis.rollup import _veto_ids

    conditions = {
        "T3a": {"status": "FAIL", "blocking": False, "detail": "satisfied by T3b"},
        "T3b": {"status": "PASS", "blocking": True, "detail": "band rejection"},
        "T4": {"status": "FAIL", "blocking": True, "detail": "rvol low"},
    }
    assert _veto_ids(conditions, None) == {"T4"}


def test_histogram_on_empty_journal():
    result = checklist_failure_histogram(empty_journal())
    assert result["sufficient"] is False
    assert result["by_condition"] == {}
    assert result["reason"]


# ---------------------------------------------------------------------------
# 4. counterfactual ledger
# ---------------------------------------------------------------------------


def test_counterfactual_buckets_are_separate_and_labelled(store):
    result = counterfactual_delta(store, "2026-03-02", "2026-03-13")

    proxy = result["proxy_delta_weighted"]
    assert proxy["count"] == 1
    assert proxy["total_usd"] == pytest.approx(EXPECTED["counterfactual_proxy_usd"])
    assert proxy["basis"] == "underlying_delta_proxy"
    assert "APPROXIMATION" in proxy["note"]

    unmod = result["unmodelled"]
    assert unmod["count"] == EXPECTED["unmodelled_rows"]
    assert unmod["directional"]["sessions_closing_beyond_projection"] == 1
    assert unmod["directional"]["sessions_closing_inside_projection"] == 1
    assert all(row["option_pnl_modelled"] is False for row in unmod["rows"])
    assert not any(k in unmod for k in ("total_usd", "net_total_usd"))

    # The headline delta is the proxy against REALIZED pnl of the same arms
    # (arm C realized 0.00), and it is not the sum of the buckets.
    assert result["delta_usd"] == pytest.approx(EXPECTED["counterfactual_delta_usd"])
    assert result["verdict"].startswith("PROXY BASIS:")
    assert "costing us" in result["verdict"]


def test_counterfactual_never_sums_proxy_with_unmodelled(store):
    result = counterfactual_delta(store, "2026-03-02", "2026-03-13")
    # There is no combined dollar figure anywhere in the payload.
    blob = repr(result)
    assert "combined_usd" not in blob
    assert result["real"]["count"] == 0
    assert result["real"]["total_usd"] == 0.0
    assert "never summed together" in result["honesty_note"]


def test_unfilled_counterfactual_row_is_counted_in_nothing(store):
    result = counterfactual_delta(store, "2026-03-02", "2026-03-13")
    dates = [r["date"] for r in result["proxy_delta_weighted"]["rows"]]
    dates += [r["date"] for r in result["unmodelled"]["rows"]]
    assert "2026-03-12" not in dates  # the sighting with no outcome bar yet
    # ...but it IS a sighting in the counts and a veto in the histogram.
    assert trade_counts(store, "2026-03-02", "2026-03-13")["by_arm"]["B"]["noshots"] == 3
    assert checklist_failure_histogram(store, "2026-03-02", "2026-03-13")[
        "by_condition"]["T2b"] == 1


def test_counterfactual_on_empty_journal():
    result = counterfactual_delta(empty_journal())
    assert result["sufficient"] is False
    assert result["delta_usd"] is None
    assert result["verdict"] == "insufficient data"


def test_counterfactual_with_no_arm_c_pnl_gives_no_delta(store):
    """Proxy rows exist but arm C has no closed P&L: no delta, stated not guessed.

    Comparing a +$200 hypothetical against $0 of realized P&L would be a delta of
    +$200 built on the absence of a control, so the function returns None instead.
    """
    result = counterfactual_delta(store, "2026-03-02", "2026-03-09")
    assert result["proxy_delta_weighted"]["count"] == 1
    assert result["realized_closed_same_arms"] == 0
    assert result["delta_usd"] is None
    assert result["verdict"] == "insufficient data"


# ---------------------------------------------------------------------------
# 5. prediction scorecard
# ---------------------------------------------------------------------------


def test_scorecard_scores_all_four_from_data(store):
    card = prediction_scorecard(store)
    assert card["window"]["since"] == "2026-03-02"   # from meta.window_start
    assert card["window"]["until"] == "2026-03-13"   # latest journal date
    statuses = {p["id"]: p["status"] for p in card["predictions"]}
    assert statuses == EXPECTED["scorecard"]


def test_prediction_1_is_scored_from_arm_a_only(store):
    p1 = prediction_scorecard(store)["predictions"][0]
    assert p1["numbers"]["A_realized_pnl"] == pytest.approx(200.00)
    assert p1["numbers"]["A_return_on_bankroll"] == pytest.approx(0.02)
    assert "$200.00" in p1["evidence"]


def test_prediction_2_falsifies_loudly_when_b_beats_a():
    """The brief: 'If B beats A over the window, that is a genuine finding'."""
    store = empty_journal()
    set_meta(store, "window_start", "2026-03-02")
    set_meta(store, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0})
    for arm, entry, exit_, pnl in (("A", 500.0, 510.0, 100.0), ("B", 1.0, 2.0, 150.0)):
        pid = open_position(
            store,
            Position(arm=arm, symbol="SPY", entry_ts="2026-03-02T14:00:00Z",
                     entry_price=entry, qty=1),
        )
        close_position(store, pid, exit_ts="2026-03-13T19:00:00Z", exit_price=exit_, pnl=pnl)
    from journal.store import DecisionEntry, append_decision

    append_decision(
        store,
        DecisionEntry(ts="2026-03-05T14:00:00Z", arm="B", kind="TRADE", symbol="SPY",
                      checklist_snapshot={}, checklist_state={"fire": True},
                      reasoning="synthetic winner", strategy_version="v1"),
    )
    p2 = prediction_scorecard(store)["predictions"][1]
    assert p2["status"] == "falsified"
    assert p2["numbers"]["B_minus_A"] == pytest.approx(50.0)
    assert "BEAT A" in p2["evidence"]


def test_prediction_3_carries_the_shadow_roll_hook(store):
    p3 = prediction_scorecard(store)["predictions"][2]
    assert p3["numbers"]["C_minus_A"] == pytest.approx(-200.00)
    # No shadow leg in the canned journal yet: the hook reports that, it does not
    # silently score "TA entries beat the roll".
    assert p3["shadow_roll"]["sufficient"] is False
    assert p3["numbers"]["ta_entries_beat_fixed_roll"] is None
    assert "not yet comparable" in p3["evidence"]


def test_prediction_4_uses_months_from_the_window(store):
    p4 = prediction_scorecard(store)["predictions"][3]
    assert p4["numbers"]["B_plus_C_trades"] == 3
    assert p4["numbers"]["months"] == pytest.approx(EXPECTED["months"])
    assert p4["numbers"]["trades_per_month"] == pytest.approx(EXPECTED["trades_per_month"])
    assert p4["status"] == "falsified"
    assert f"< {EXPECTED_TRADES_PER_MONTH:.0f}/month" in p4["evidence"]


def test_scorecard_on_empty_journal_is_all_insufficient_data():
    card = prediction_scorecard(empty_journal())
    assert card["sufficient"] is False
    assert [p["status"] for p in card["predictions"]] == ["insufficient_data"] * 4
    assert card["tally"] == {"supported": 0, "falsified": 0, "insufficient_data": 4}


def test_scorecard_with_data_but_no_trades_never_defaults_to_supported():
    """A window with an arm A mark but zero B/C trades: #2/#3 unscoreable, not passed."""
    store = empty_journal()
    set_meta(store, "window_start", "2026-03-02")
    set_meta(store, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0})
    pid = open_position(
        store,
        Position(arm="A", symbol="SPY", entry_ts="2026-03-02T14:00:00Z",
                 entry_price=500.0, qty=10),
    )
    close_position(store, pid, exit_ts="2026-03-13T19:00:00Z", exit_price=505.0)
    card = prediction_scorecard(store)
    statuses = {p["id"]: p["status"] for p in card["predictions"]}
    assert statuses["1"] == "supported"      # A is +50
    assert statuses["2"] == "insufficient_data"
    assert statuses["3"] == "insufficient_data"
    # Zero trades literally is < 2/month, and the evidence line says so out loud.
    assert statuses["4"] == "supported"
    assert "0 trade(s)" in card["predictions"][3]["evidence"]


# ---------------------------------------------------------------------------
# 6. the rollup text
# ---------------------------------------------------------------------------


def test_weekly_rollup_text_renders_every_section(store):
    text = weekly_rollup_text(store, WEEK_END)
    assert text.startswith(f"# Weekly rollup — {WEEK_START.isoformat()} → {WEEK_END.isoformat()}")
    for heading in (
        "## P&L per arm",
        "## Trade / no-trade counts",
        "## Checklist-failure histogram",
        "## Counterfactual ledger",
        "## Pre-registered predictions",
        "## Shadow roll",
    ):
        assert heading in text


def test_weekly_rollup_numbers_match_the_canned_journal(store):
    """The seven days ending 2026-03-13 (the journal's second week)."""
    text = weekly_rollup_text(store, WEEK_END)
    assert "| A | $200.00 | $0.00 | 1 | 0 | +2.00% |" in text
    assert "| B | $0.00 | $-200.00 | 0 | 0 | +0.00% |" in text
    assert "| C | $0.00 | $-200.00 | 2 | 1 | +0.00% |" in text
    assert "Exception path (tracked separately, never summed in): $20.00" in text
    for cid, count in EXPECTED["week2_histogram"].items():
        assert f"| {cid} | {count} |" in text
    assert f"{EXPECTED['week2_unmodelled']} row(s) — " in text
    assert "Delta (proxy basis only): $200.00" in text
    # The scorecard always covers the whole experiment window, so its frequency line is
    # the 3-trades-in-12-days number, not the week's.
    assert "3 trade(s) across B+C in 0.39 month(s) = 7.61/month" in text


def test_weekly_rollup_week_one_window(store):
    """The seven days ending 2026-03-08: only arm B's 0DTE closed inside it."""
    text = weekly_rollup_text(store, WEEK1_END)
    assert text.startswith(
        f"# Weekly rollup — {WEEK1_START.isoformat()} → {WEEK1_END.isoformat()}"
    )
    for arm, expected in EXPECTED["week1_pnl"].items():
        formatted = f"${expected:,.2f}"
        assert f"| {arm} | {formatted} |" in text
    assert "| B | $-5.00 | $-5.00 | 1 | 0 | -0.05% |" in text


def test_weekly_rollup_scorecard_covers_the_whole_window_not_the_week(store):
    text = weekly_rollup_text(store, WEEK_END)
    # The week's window is 7 days; the scorecard window is the experiment's 12.
    assert "Window: 2026-03-02 → 2026-03-13 (12 day(s)" in text
    assert "2 supported, 2 falsified, 0 not yet scorable" in text


def test_weekly_rollup_on_empty_journal_says_so_instead_of_printing_zeros():
    text = weekly_rollup_text(empty_journal(), "2026-03-13")
    assert "No closed positions in this window" in text
    assert "no window_start in meta" in text
    assert "No shadow leg has been opened yet" in text
    assert "$0.00" not in text  # nothing is invented


def test_weekly_rollup_accepts_a_date_or_a_string(store):
    a = weekly_rollup_text(store, dt.date(2026, 3, 13))
    b = weekly_rollup_text(store, "2026-03-13")
    assert a == b


def test_monthly_review_bullets(store):
    bullets = monthly_review_bullets(store)
    joined = "\n".join(bullets)
    assert "A $200.00 · B $-5.00 · C $0.00 · exception path $20.00 (separate)" in joined
    assert "2 supported, 2 falsified" in joined
    assert "Prediction 3 — falsified" in joined
    assert "Prediction 4 — falsified" in joined
