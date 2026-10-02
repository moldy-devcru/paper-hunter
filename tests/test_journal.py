"""Tests for the append-only journal.

The important ones here are the negative tests: they assert the immutability triggers
actually fire. A schema comment claiming append-only is worth nothing if UPDATE and
DELETE quietly succeed.
"""

from __future__ import annotations

import sqlite3

import pytest

from journal.store import (
    DecisionEntry,
    NoshotEntry,
    Position,
    append_decision,
    append_noshot,
    close_position,
    get_decision,
    get_meta,
    get_position,
    init_db,
    list_decisions,
    list_noshots,
    list_positions,
    open_position,
    set_counterfactual_outcome,
    set_meta,
    utcnow,
)

SNAPSHOT = {
    "close": 671.25,
    "ema50": 668.10,
    "ema200": 655.40,
    "rsi14": 61.3,
    "macd_hist": 0.42,
    "bb_bandwidth_pct": 11.8,
    "rel_volume": 1.72,
    "iv_rank": 34.0,
    "otm_flow_ratio": 3.1,
}

STATE = {
    "T1": {"pass": True},
    "T2": {"pass": True},
    "T4": {"pass": False, "reason": "rel_volume 1.72 >= 1.5 ok"},  # sanity: mixed payload
    "T5": {"pass": True, "reason": "iv_rank 34 < 50"},
}


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "journal.db")
    yield connection
    connection.close()


def _decision(**overrides) -> DecisionEntry:
    base = dict(
        ts=utcnow(),
        arm="B",
        kind="NO_TRADE",
        symbol="SPY",
        checklist_snapshot=SNAPSHOT,
        checklist_state=STATE,
        reasoning="T5 flat: IV rank too low for premium buying.",
        conviction=4,
        strategy_version="v0.1.0",
    )
    base.update(overrides)
    return DecisionEntry(**base)


# --- schema init -----------------------------------------------------------


def test_init_db_is_idempotent(tmp_path):
    conn = init_db(tmp_path / "journal.db")
    conn.close()
    conn = init_db(tmp_path / "journal.db")
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"decisions", "noshots", "positions", "meta"} <= tables
    conn.close()


# --- decisions: append + roundtrip -----------------------------------------


def test_append_decision_roundtrip(conn):
    new_id = append_decision(conn, _decision())
    assert isinstance(new_id, int)

    stored = get_decision(conn, new_id)
    assert stored is not None
    assert stored.arm == "B"
    assert stored.kind == "NO_TRADE"
    assert stored.conviction == 4
    # full snapshot survives, not just the deciding conditions
    assert stored.checklist_snapshot == SNAPSHOT
    assert stored.checklist_state["T5"]["pass"] is True
    assert stored.references == []
    assert stored.created_at is not None


def test_decision_correction_is_a_new_row_referencing_the_old(conn):
    original_id = append_decision(conn, _decision(reasoning="original take"))
    correction_id = append_decision(
        conn,
        _decision(kind="VETO", reasoning="correcting myself", references=[original_id]),
    )
    assert correction_id != original_id

    correction = get_decision(conn, correction_id)
    assert correction.references == [original_id]
    # and the original is untouched, still readable as its own row
    assert get_decision(conn, original_id).reasoning == "original take"
    assert len(list_decisions(conn)) == 2


def test_list_decisions_filters(conn):
    append_decision(conn, _decision(arm="B", kind="NO_TRADE"))
    append_decision(conn, _decision(arm="C", kind="TRADE"))
    assert [d.arm for d in list_decisions(conn, arm="C")] == ["C"]
    assert [d.kind for d in list_decisions(conn, kind="NO_TRADE")] == ["NO_TRADE"]


def test_conviction_bounds_are_validated():
    with pytest.raises(ValueError):
        _decision(conviction=11)
    with pytest.raises(ValueError):
        _decision(conviction=0)


# --- append-only enforcement ----------------------------------------------


def test_update_on_decisions_raises(conn):
    new_id = append_decision(conn, _decision())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decisions SET reasoning = 'rewriting history' WHERE id = ?", (new_id,))
    conn.rollback()
    assert get_decision(conn, new_id).reasoning != "rewriting history"


def test_delete_on_decisions_raises(conn):
    new_id = append_decision(conn, _decision())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM decisions WHERE id = ?", (new_id,))
    conn.rollback()
    assert get_decision(conn, new_id) is not None


# --- noshots ---------------------------------------------------------------


def _noshot(**overrides) -> NoshotEntry:
    base = dict(
        ts=utcnow(),
        date="2026-09-30",
        instrument_hypothesis={
            "arm": "B",
            "side": "call",
            "strike": "SPY260930C00672000",
            "premium_cap": 200.0,
        },
        failed_conditions={"T6": "deep-OTM flow 1.1x < Nx baseline"},
        indicator_values=SNAPSHOT,
        counterfactual_entry_ref=None,
    )
    base.update(overrides)
    return NoshotEntry(**base)


def test_append_noshot_roundtrip(conn):
    decision_id = append_decision(conn, _decision())
    shot_id = append_noshot(conn, _noshot(counterfactual_entry_ref=decision_id))

    stored = list_noshots(conn, date="2026-09-30")
    assert len(stored) == 1
    entry = stored[0]
    assert entry.id == shot_id
    assert entry.counterfactual_entry_ref == decision_id
    assert entry.instrument_hypothesis["strike"] == "SPY260930C00672000"
    assert entry.indicator_values == SNAPSHOT
    assert entry.counterfactual_outcome is None


def test_counterfactual_outcome_may_be_filled_once(conn):
    shot_id = append_noshot(conn, _noshot())
    set_counterfactual_outcome(conn, shot_id, {"pnl": 140.0, "would_have_hit": True})

    entry = list_noshots(conn)[0]
    assert entry.counterfactual_outcome == {"pnl": 140.0, "would_have_hit": True}

    # second fill is rejected: the one exception is a single transition
    with pytest.raises(sqlite3.IntegrityError):
        set_counterfactual_outcome(conn, shot_id, {"pnl": 999.0})
    conn.rollback()
    assert list_noshots(conn)[0].counterfactual_outcome["pnl"] == 140.0


def test_other_noshot_columns_stay_immutable(conn):
    shot_id = append_noshot(conn, _noshot())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE noshots SET failed_conditions = '{}' WHERE id = ?", (shot_id,))
    conn.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE noshots SET indicator_values = '{}', counterfactual_outcome = '{}' "
            "WHERE id = ?",
            (shot_id,),
        )
    conn.rollback()
    assert list_noshots(conn)[0].failed_conditions["T6"].startswith("deep-OTM")


def test_delete_on_noshots_raises(conn):
    shot_id = append_noshot(conn, _noshot())
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM noshots WHERE id = ?", (shot_id,))
    conn.rollback()
    assert len(list_noshots(conn)) == 1


# --- positions -------------------------------------------------------------


def test_open_and_close_position(conn):
    position_id = open_position(
        conn,
        Position(
            arm="C",
            symbol="SPY",
            contract="SPY261218C00650000",
            entry_ts=utcnow(),
            entry_price=412.50,
            qty=1,
        ),
    )
    assert get_position(conn, position_id).status == "OPEN"

    close_position(conn, position_id, exit_ts=utcnow(), exit_price=455.00)

    closed = get_position(conn, position_id)
    assert closed.status == "CLOSED"
    assert closed.pnl == pytest.approx(42.50)
    assert list_positions(conn, status="OPEN") == []
    assert len(list_positions(conn, arm="C")) == 1


def test_cannot_close_a_position_twice(conn):
    position_id = open_position(
        conn,
        Position(
            arm="B",
            symbol="SPY",
            contract="SPY260930C00672000",
            entry_ts=utcnow(),
            entry_price=2.15,
            qty=2,
        ),
    )
    close_position(conn, position_id, exit_ts=utcnow(), exit_price=4.30)
    with pytest.raises(ValueError):
        close_position(conn, position_id, exit_ts=utcnow(), exit_price=9.00)


def test_close_position_computes_pnl_for_multi_qty(conn):
    position_id = open_position(
        conn,
        Position(arm="B", symbol="SPY", entry_ts=utcnow(), entry_price=10.0, qty=3),
    )
    close_position(conn, position_id, exit_ts=utcnow(), exit_price=8.0)
    assert get_position(conn, position_id).pnl == pytest.approx(-6.0)


# --- meta ------------------------------------------------------------------


def test_meta_upsert_and_read(conn):
    set_meta(conn, "window_start", "2026-10-01")
    set_meta(conn, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0})
    set_meta(conn, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 9_500.0})

    assert get_meta(conn, "window_start") == "2026-10-01"
    assert get_meta(conn, "arm_bankroll")["C"] == 9_500.0
    assert get_meta(conn, "absent_key", default="fallback") == "fallback"
