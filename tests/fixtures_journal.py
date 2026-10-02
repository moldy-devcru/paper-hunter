"""A canned two-week journal with hand-computed expected numbers.

Shared by ``test_rollup.py`` and ``test_shadow_roll.py`` so both score the *same*
ledger — the weekly rollup and the scorecard are two views of one dataset, and two
hand-built datasets would let the views disagree for the wrong reason.

The journal is dated 2026-03-02 → 2026-03-13 (two full weeks, five sessions) with
every expected number written out in ``EXPECTED`` at the bottom of this file. Tests
assert against those constants rather than re-deriving them, so a change in the
analysis code that moves a number shows up as a failure instead of as a test that
quietly recomputed the wrong answer.

Nothing here touches the network or the real runtime journal: every connection is
in-memory SQLite created from ``journal/schema.sql``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from journal.store import (
    DecisionEntry,
    NoshotEntry,
    Position,
    append_decision,
    append_noshot,
    close_position,
    init_db,
    open_position,
    set_counterfactual_outcome,
    set_meta,
)

SYMBOL = "SPY"
STRATEGY_VERSION = "v1.0.0"
WINDOW_START = "2026-03-02"
WINDOW_END = "2026-03-13"


def _ts(date: str, time: str = "12:00:00") -> str:
    return f"{date}T{time}Z"


# ---------------------------------------------------------------------------
# positions — hand-computed P&L
#
#   A  buy 20 @ 500.00, sell @ 510.00  -> (510-500)*20 =  +200.00
#   B  0DTE 5 @ 1.50, exit  @ 0.50     -> (0.50-1.50)*5 =   -5.00
#   C  20 @ 12.00 -> 12.50             -> (12.50-12.00)*20 = +10.00
#   C  20 @ 12.20 -> 11.70             -> (11.70-12.20)*20 = -10.00   (net C = 0.00)
#   EXC 100 @ 0.10 -> 0.30             -> (0.30-0.10)*100 = +20.00
#   C  20 @ 13.00                      -> still OPEN, contributes nothing
# ---------------------------------------------------------------------------
POSITIONS: list[dict[str, Any]] = [
    {
        "arm": "A", "symbol": SYMBOL, "contract": None,
        "entry_ts": _ts(WINDOW_START, "13:30"), "entry_price": 500.00, "qty": 20,
        "exit_ts": _ts(WINDOW_END, "19:00"), "exit_price": 510.00, "notes": "buy and hold",
    },
    {
        "arm": "B", "symbol": SYMBOL, "contract": "SPY260305C00502000",
        "entry_ts": _ts("2026-03-05", "14:05"), "entry_price": 1.50, "qty": 5,
        "exit_ts": _ts("2026-03-05", "19:25"), "exit_price": 0.50, "notes": "time stop",
    },
    {
        "arm": "C", "symbol": SYMBOL, "contract": "SPY260619C00480000",
        "entry_ts": _ts("2026-03-10", "14:00"), "entry_price": 12.00, "qty": 20,
        "exit_ts": _ts("2026-03-10", "19:00"), "exit_price": 12.50, "notes": None,
    },
    {
        "arm": "C", "symbol": SYMBOL, "contract": "SPY260619C00495000",
        "entry_ts": _ts("2026-03-12", "14:00"), "entry_price": 12.20, "qty": 20,
        "exit_ts": _ts("2026-03-12", "19:00"), "exit_price": 11.70, "notes": "T1 break",
    },
    {
        "arm": "EXCEPTION", "symbol": SYMBOL, "contract": "SPY260311C00510000",
        "entry_ts": _ts("2026-03-11", "13:20"), "entry_price": 0.10, "qty": 100,
        "exit_ts": _ts("2026-03-11", "19:20"), "exit_price": 0.30, "notes": "catalyst clause",
    },
    {
        "arm": "C", "symbol": SYMBOL, "contract": "SPY260619C00510000",
        "entry_ts": _ts("2026-03-09", "14:00"), "entry_price": 13.00, "qty": 20,
        "open": True,
    },
]


# ---------------------------------------------------------------------------
# decisions — the counts and the veto histogram
# ---------------------------------------------------------------------------
def _cond(status: str, blocking: bool = True, detail: str = "") -> dict[str, Any]:
    return {"id": detail.split(" ")[0] or status, "status": status,
            "blocking": blocking, "detail": detail}


def _state(conditions: dict[str, Any], failed: list[str], pending: list[str]) -> dict[str, Any]:
    return {
        "fire": False,
        "decision_kind": "NO_TRADE",
        "conditions": conditions,
        "failed_conditions": failed,
        "pending_conditions": pending,
        "veto_reasons": [f"{cid}: veto — {body['detail']}" for cid, body in conditions.items()
                         if body["blocking"] and body["status"] != "PASS"],
        "conviction": 5,
    }


TRADE_DECISIONS: list[tuple[str, str, str]] = [
    ("B", "2026-03-05", "0DTE call, time-stopped out"),
    ("C", "2026-03-10", "deep ITM call entry"),
    ("C", "2026-03-12", "deep ITM call entry"),
    ("EXCEPTION", "2026-03-11", "catalyst-clause proposal approved by operator"),
]

NO_TRADE_DECISIONS: list[dict[str, Any]] = [
    {
        "date": "2026-03-02", "arm": "B",
        "conditions": {
            "T1": _cond("PASS", True, "T1 OK: close > EMA50 > EMA200"),
            "T2": _cond("PASS", True, "T2 OK"),
            "T2b": _cond("PASS", True, "T2b OK"),
            "T3a": _cond("FAIL", True, "T3 group veto: neither arm satisfied"),
            "T3b": _cond("FAIL", False, "T3b folded into T3 group veto (carried by T3a)"),
            "T4": _cond("FAIL", True, "T4 NOT: rvol 1.12 < 1.5"),
            "T5": _cond("PASS", True, "T5 OK"),
            "T6": _cond("PENDING", True, "T6 pending: multiplier not calibrated"),
        },
        "failed": ["T3a", "T4"], "pending": ["T6"],
    },
    {
        "date": "2026-03-03", "arm": "B",
        "conditions": {
            "T1": _cond("PASS", True, "T1 OK"),
            "T2": _cond("FAIL", True, "T2 NOT: rsi 78.2 > 75 exhausted"),
            "T2b": _cond("PASS", True, "T2b OK"),
            "T3a": _cond("PASS", True, "T3a PASS"),
            "T3b": _cond("FAIL", False, "T3 group satisfied by T3a squeeze-release"),
            "T4": _cond("PASS", True, "T4 OK"),
            "T5": _cond("PASS", True, "T5 OK"),
            "T6": _cond("PENDING", True, "T6 pending: no chain"),
        },
        "failed": ["T2"], "pending": ["T6"],
    },
    {
        "date": "2026-03-04", "arm": "C",
        "conditions": {
            "T1": _cond("PASS", True, "T1 OK"),
            "T2": _cond("PASS", True, "T2 OK"),
            "T2b": _cond("PASS", True, "T2b OK"),
            "T3a": _cond("FAIL", True, "T3 group veto: neither arm satisfied"),
            "T3b": _cond("FAIL", False, "T3b folded into T3 group veto (carried by T3a)"),
            "T4": _cond("PASS", True, "T4 OK"),
            "T5": _cond("PENDING", True, "T5 pending: arm C IV band not calibrated"),
            "T6": _cond("SKIPPED", False, "T6 does not apply to arm C"),
        },
        "failed": ["T3a"], "pending": ["T5"],
    },
    {
        "date": "2026-03-06", "arm": "B",
        "conditions": {
            "T1": _cond("FAIL", True, "T1 NOT: ema stack inverted for a put"),
            "T2": _cond("PASS", True, "T2 OK"),
            "T2b": _cond("PASS", True, "T2b OK"),
            "T3a": _cond("FAIL", True, "T3 group veto: neither arm satisfied"),
            "T3b": _cond("FAIL", False, "T3b folded into T3 group veto (carried by T3a)"),
            "T4": _cond("FAIL", True, "T4 NOT: rvol 0.94 < 1.5"),
            "T5": _cond("PASS", True, "T5 OK"),
            "T6": _cond("PENDING", True, "T6 pending: no chain"),
        },
        "failed": ["T1", "T3a", "T4"], "pending": ["T6"],
    },
]


# ---------------------------------------------------------------------------
# noshots — the sighting/counterfactual ledger
# ---------------------------------------------------------------------------
NOSHOTS: list[dict[str, Any]] = [
    {
        "date": "2026-03-05", "arm": "B",
        "failed": {
            "T3a": {"status": "FAIL", "blocking": True, "detail": "T3 group veto"},
            "T3b": {"status": "FAIL", "blocking": False, "detail": "folded into T3a"},
            "T4": {"status": "FAIL", "blocking": True, "detail": "rvol 1.12 < 1.5"},
        },
        "outcome": {
            "basis": "underlying_session",
            "move_from_open_pct": 0.012,
            "max_favourable_excursion_pct": 0.020,
            "max_adverse_excursion_pct": -0.005,
            "close_beyond_projection": True,
            "option_pnl_modelled": False,
        },
    },
    {
        "date": "2026-03-09", "arm": "C",
        "failed": {
            "T3a": {"status": "PENDING", "blocking": True, "detail": "bollinger history short"},
            "T5": {"status": "FAIL", "blocking": True, "detail": "earnings day veto"},
        },
        # delta 0.85, premium 1.20, 4 contracts, underlying +2.00:
        #   gross = 0.85 * 100 * 2.00 = +170.00 ; cost = 1.20 * 100 = 120.00
        #   net   = +50.00 per contract ; total = +200.00
        "outcome": {
            "basis": "underlying_delta_proxy",
            "delta_at_sighting": 0.85,
            "premium_per_contract_usd": 1.20,
            "contracts": 4,
            "gross_per_contract_usd": 170.00,
            "net_per_contract_usd": 50.00,
            "net_total_usd": 200.00,
        },
    },
    {
        "date": "2026-03-11", "arm": "B",
        "failed": {
            "T4": {"status": "FAIL", "blocking": True, "detail": "rvol 1.31 < 1.5"},
        },
        "outcome": {
            "basis": "underlying_session",
            "move_from_open_pct": -0.008,
            "max_favourable_excursion_pct": 0.001,
            "max_adverse_excursion_pct": -0.010,
            "close_beyond_projection": False,
            "option_pnl_modelled": False,
        },
    },
    {
        # Sighting whose counterfactual has NOT been filled yet (window still open /
        # no outcome bars): it must appear in the counts and the histogram and must NOT
        # appear in any counterfactual bucket.
        "date": "2026-03-12", "arm": "B",
        "failed": {
            "T2b": {"status": "FAIL", "blocking": True, "detail": "histogram contracting"},
        },
        "outcome": None,
    },
]


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def build_journal(conn: Any) -> Any:
    """Populate ``conn`` (already initialised) with the canned two-week journal."""
    set_meta(conn, "strategy_version", STRATEGY_VERSION)
    set_meta(conn, "window_start", WINDOW_START)
    set_meta(conn, "arm_bankroll", {"A": 10_000.0, "B": 10_000.0, "C": 10_000.0})

    for spec in POSITIONS:
        if spec.get("open"):
            open_position(
                conn,
                Position(
                    arm=spec["arm"], symbol=spec["symbol"], contract=spec["contract"],
                    entry_ts=spec["entry_ts"], entry_price=spec["entry_price"],
                    qty=spec["qty"], notes=spec.get("notes"),
                ),
            )
            continue
        pid = open_position(
            conn,
            Position(
                arm=spec["arm"], symbol=spec["symbol"], contract=spec["contract"],
                entry_ts=spec["entry_ts"], entry_price=spec["entry_price"],
                qty=spec["qty"],
            ),
        )
        # pnl left to the store's own convention: (exit - entry) * qty
        close_position(
            conn, pid,
            exit_ts=spec["exit_ts"], exit_price=spec["exit_price"], notes=spec.get("notes"),
        )

    for arm, date, reasoning in TRADE_DECISIONS:
        append_decision(
            conn,
            DecisionEntry(
                ts=_ts(date, "14:00"), arm=arm, kind="TRADE", symbol=SYMBOL,
                checklist_snapshot={"close": 500.0},
                checklist_state={"fire": True, "decision_kind": "TRADE"},
                reasoning=reasoning, conviction=7, strategy_version=STRATEGY_VERSION,
            ),
        )

    for spec in NO_TRADE_DECISIONS:
        append_decision(
            conn,
            DecisionEntry(
                ts=_ts(spec["date"], "12:30"), arm=spec["arm"], kind="NO_TRADE", symbol=SYMBOL,
                checklist_snapshot={"close": 500.0, "rsi": 61.0, "relvol": 1.1},
                checklist_state=_state(spec["conditions"], spec["failed"], spec["pending"]),
                reasoning="checklist did not fire at plan time", conviction=4,
                strategy_version=STRATEGY_VERSION,
            ),
        )

    for spec in NOSHOTS:
        nid = append_noshot(
            conn,
            NoshotEntry(
                ts=_ts(spec["date"], "19:30"), date=spec["date"],
                instrument_hypothesis={"arm": spec["arm"], "direction": "call"},
                failed_conditions=spec["failed"],
                indicator_values={"close": 500.0},
            ),
        )
        if spec["outcome"] is not None:
            set_counterfactual_outcome(conn, nid, spec["outcome"])
    return conn


def canned_journal() -> Any:
    """A fresh in-memory journal, fully populated. THE dataset for both test modules."""
    return build_journal(init_db(":memory:"))


def empty_journal() -> Any:
    """A fresh in-memory journal with the schema and NOTHING in it."""
    return init_db(":memory:")


# ---------------------------------------------------------------------------
# expected numbers (hand-computed; see the docstring comments above)
# ---------------------------------------------------------------------------
EXPECTED = {
    "window_days": 12,          # 2026-03-02 .. 2026-03-13 inclusive
    "months": 12 / (365.2425 / 12.0),   # 0.394182...
    "pnl": {"A": 200.00, "B": -5.00, "C": 0.00, "EXCEPTION": 20.00},
    "closed": {"A": 1, "B": 1, "C": 2, "EXCEPTION": 1},
    "open": {"A": 0, "B": 0, "C": 1, "EXCEPTION": 0},
    "vs_control": {"A": 0.00, "B": -205.00, "C": -200.00, "EXCEPTION": -180.00},
    "return_on_bankroll": {"A": 0.02, "B": -0.0005, "C": 0.0, "EXCEPTION": 0.002},
    "counts": {
        "A": {"trades": 0, "no_trades": 0, "proposals": 0, "vetoes": 0, "rolls": 0,
              "stops": 0, "noshots": 0},
        "B": {"trades": 1, "no_trades": 3, "proposals": 0, "vetoes": 0, "rolls": 0,
              "stops": 0, "noshots": 3},
        "C": {"trades": 2, "no_trades": 1, "proposals": 0, "vetoes": 0, "rolls": 0,
              "stops": 0, "noshots": 1},
        "EXCEPTION": {"trades": 1, "no_trades": 0, "proposals": 0, "vetoes": 0, "rolls": 0,
                      "stops": 0, "noshots": 0},
    },
    "combined_trades": 3,
    "trades_per_month": 3 / (12 / (365.2425 / 12.0)),   # 7.6114...
    "histogram": {"T4": 4, "T3": 4, "T2": 1, "T1": 1, "T5": 1, "T2b": 1},
    "histogram_by_arm": {
        "B": {"T4": 4, "T3": 3, "T2": 1, "T1": 1, "T2b": 1},
        "C": {"T3": 1, "T5": 1},
    },
    "pending": {"T6": 3, "T5": 1, "T3": 1},
    "counterfactual_proxy_usd": 200.00,     # arm C delta-weighted net
    "counterfactual_delta_usd": 200.00,     # proxy 200.00 - realized C 0.00
    "unmodelled_rows": 2,
    "unmodelled_beyond": 1,
    "unmodelled_inside": 1,
    "unmodelled_sum_move_pct": 0.012 - 0.008,      # 0.004
    "unmodelled_sum_mfe_pct": 0.020 + 0.001,        # 0.021
    "unmodelled_sum_mae_pct": -0.005 - 0.010,       # -0.015
    "scorecard": {"1": "supported", "2": "supported", "3": "falsified", "4": "falsified"},
    "shadow_return_on_capital": 0.02,       # 0.2 units * 100 sh * $500 notional, +$200
    "delta_return_on_capital": -0.02,       # arm C 0.00% vs shadow +2.00%
    # The seven days ending on WEEK_END (2026-03-13) cover 2026-03-07..13, i.e. the
    # second week of the canned journal. B's only fill closed on the 5th, so it is
    # outside this window; A and the exception path closed on the 13th.
    "week2_pnl": {"A": 200.00, "B": 0.00, "C": 0.00, "EXCEPTION": 20.00},
    "week2_vs_control": {"A": 0.00, "B": -200.00, "C": -200.00, "EXCEPTION": -180.00},
    "week2_histogram": {"T4": 1, "T2b": 1, "T5": 1},
    "week2_unmodelled": 1,
    # The seven days ending 2026-03-08 cover the first week (03-02..03-08): only B's
    # 0DTE closed inside it.
    "week1_pnl": {"A": 0.00, "B": -5.00, "C": 0.00, "EXCEPTION": 0.00},
}

WEEK_END = dt.date(2026, 3, 13)
WEEK_START = dt.date(2026, 3, 7)
WEEK1_END = dt.date(2026, 3, 8)
WEEK1_START = dt.date(2026, 3, 2)
