"""The U3 frontend's pure logic, executed for real.

U3 put the parts worth scoring — the NO-SHOT join, the counterfactual sentence, the
veto-histogram model, the checklist grid, the ledger's sort/pagination/reference rules —
in ``ui/static/model.js`` and ``format.js`` as pure ES modules with no DOM access. This
module runs them under **Node** and compares the results against hand-written
expectations, so the assertions are about behaviour rather than about a substring
appearing in a file.

Why Node rather than a browser: jsdom is a dependency we would have to vendor to test
code that deliberately has no DOM, and a headless Chromium is a dependency we would have
to install on every machine that runs the suite. The modules under test import nothing
but each other, which is exactly the property that makes ``node --input-type=module``
enough. Each test below is skipped (not failed) when Node is absent, and
``test_ui_static.py`` still pins the DOM wiring statically — so a machine without Node
loses behavioural coverage and keeps structural coverage, which is the honest trade.

The split, stated plainly because it is a judgement call:

* **Here (behavioural):** pure functions. A wrong counterfactual sentence is a wrong
  number on the page.
* **In test_ui_static.py (structural):** that the click handler calls the popup, that the
  popup closes on Escape and outside-click, that the sort headers are wired. Those need a
  document to be meaningful and asserting them behaviourally would mean a browser.

Offline: no server, no network, no fixtures beyond dicts written inline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "ui" / "static"
NODE = shutil.which("node")

PREAMBLE = """
import * as M from "./ui/static/model.js";
import * as F from "./ui/static/format.js";
import * as P from "./ui/static/markers.js";
"""


def run_js(expression: str):
    """Evaluate ``expression`` in a Node module scope with M and F imported."""
    if NODE is None:  # pragma: no cover - guarded by the module-level skip
        pytest.skip("node is not installed")
    script = (
        PREAMBLE
        + f"\nconst out = await ({expression});\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "NODE_OPTIONS": ""},
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed:\n{result.stderr.strip()[:2000]}")
    return json.loads(result.stdout or "null")


def js(source: str) -> str:
    """Turn a JSON literal into a JavaScript literal, plus the module import path."""
    return json.dumps(source)


pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to execute the ES modules")


# ---------------------------------------------------------------------------
# 1. the NO-SHOT join
# ---------------------------------------------------------------------------

# The canned journal's four sightings, as /api/noshots returns them. Row #2 references
# decision #7, which is the whole point: the sighting names the row that said no.
NOSHOTS = [
    {
        "id": 1, "ts": "2026-03-05T19:30:00Z", "date": "2026-03-05",
        "instrument_hypothesis": {"arm": "B", "direction": "call"},
        "failed_conditions": {
            "T3a": {"status": "FAIL", "blocking": True, "detail": "T3 group veto"},
            "T4": {"status": "FAIL", "blocking": True, "detail": "rvol 1.12 < 1.5"},
        },
        "counterfactual_entry_ref": 7,
        "counterfactual_outcome": {
            "basis": "underlying_session", "move_from_open_pct": 0.012,
            "max_favourable_excursion_pct": 0.020, "max_adverse_excursion_pct": -0.005,
            "close_beyond_projection": True, "option_pnl_modelled": False,
        },
        "has_outcome": True, "price": 501.25, "price_source": "snapshot.close",
    },
    {
        "id": 2, "ts": "2026-03-09T19:30:00Z", "date": "2026-03-09",
        "instrument_hypothesis": {"arm": "C", "direction": "call"},
        "failed_conditions": {
            "T5": {"status": "FAIL", "blocking": True, "detail": "earnings day veto"}
        },
        "counterfactual_entry_ref": None,
        "counterfactual_outcome": {
            "basis": "underlying_delta_proxy", "delta_at_sighting": 0.85,
            "premium_per_contract_usd": 1.20, "contracts": 4,
            "gross_per_contract_usd": 170.00, "net_per_contract_usd": 50.00,
            "net_total_usd": 200.00,
        },
        "has_outcome": True, "price": 502.0, "price_source": "bar",
    },
    {
        "id": 3, "ts": "2026-03-12T19:30:00Z", "date": "2026-03-12",
        "instrument_hypothesis": {"arm": "B", "direction": "call"},
        "failed_conditions": {
            "T2b": {"status": "FAIL", "blocking": True, "detail": "histogram contracting"}
        },
        "counterfactual_entry_ref": None, "counterfactual_outcome": None, "has_outcome": False,
        "price": None, "price_source": "none",
    },
]

SIGNAL = {
    "journal_id": 7, "t": "2026-03-05T14:00:00Z", "price": 500.5, "price_source": "snapshot.close",
    "arm": "B", "kind": "TRADE", "symbol": "SPY", "conviction": 7,
    "reasoning": "0DTE call, time-stopped out",
}


def test_noshot_index_resolves_both_keys() -> None:
    """A sighting is reachable by its decision ref AND by its own id."""
    out = run_js(
        "(() => { const i = M.indexNoshots(" + js(NOSHOTS) + ");"
        " return {byRef: M.noshotsForDecision(i, 7).map(r => r.id),"
        " byId: M.noshotsForDecision(i, null).length,"
        " missing: M.noshotsForDecision(i, 999).length}; })()"
    )
    assert out["byRef"] == [1], "decision #7 must find the sighting that references it"
    assert out["missing"] == 0, "an unlinked decision must resolve to nothing, not to a row"
    assert out["byId"] == 0


def test_noshot_index_keeps_unlinked_rows_reachable() -> None:
    """Sighting #2 references no decision. It must still be drawable on the chart."""
    out = run_js(
        "(() => { const i = M.indexNoshots(" + js(NOSHOTS) + ");"
        " return i.get('n:2').map(r => r.id).concat(i.get('n:3').map(r => r.id)); })()"
    )
    assert out == [2, 3], "an unlinked sighting is still a sighting"


def test_marker_popup_joins_decision_and_sighting() -> None:
    """Clicking a decision marker shows the sighting that references it, and its counterfactual."""
    out = run_js(
        "(() => { const i = M.indexNoshots(" + js(NOSHOTS) + ");"
        " return M.markerPopup(" + js(SIGNAL) + ", M.noshotsForDecision(i, 7)); })()"
    )
    assert out["title"] == "B · TRADE"
    assert out["subtitle"] == "journal #7"
    assert {f["k"]: f["v"] for f in out["fields"]}["price"] == "500.50"
    assert out["attached"] == 1
    # This one is an underlying-session basis with no delta: no premium math is possible
    # and the UI must say that rather than inventing an option P&L.
    assert "NOT modelled" in out["counterfactual"]["headline"]
    assert out["counterfactual"]["state"] == "unmodelled"
    assert out["counterfactual"]["tone"] == "good"
    assert [f["id"] for f in out["failed"]] == ["T3a", "T4"]
    assert out["failed"][1]["detail"] == "rvol 1.12 < 1.5"


def test_marker_popup_without_a_decision_is_the_noshot_case() -> None:
    """A hollow marker has no decision of its own; the popup must still be complete."""
    out = run_js("M.markerPopup(null, [" + js(NOSHOTS[1]) + "])")
    assert out["title"] == "NO-SHOT · C"
    assert out["subtitle"] == "noshot #2"
    fields = {f["k"]: f["v"] for f in out["fields"]}
    assert fields["kind"] == "NO_SHOT"
    assert fields["underlying"] == "502.00", "the sighting's own price, with its provenance"
    assert "would have made" in out["counterfactual"]["headline"]
    assert "+$200.00" in out["counterfactual"]["headline"]


def test_marker_popup_escapes_journal_text() -> None:
    """Reasoning is operator-authored prose rendered into innerHTML. The pure renderer is
    called directly and the tag must come out as text, not as markup."""
    out = run_js(
        "P.renderPopupHtml(M.markerPopup("
        + js({**SIGNAL, "reasoning": '<img src=x onerror="boom">'})
        + ", []))"
    )
    assert "<img" not in out, "journal text must never be emitted as markup"
    assert "&lt;img" in out
    assert "onerror" in out, "the text is shown, just not executed"


# ---------------------------------------------------------------------------
# 2. counterfactuals — the sentence the whole page exists for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "state", "tone", "contains"),
    [
        (
            {"basis": "underlying_delta_proxy", "net_total_usd": 200.0, "contracts": 4,
             "premium_per_contract_usd": 1.2, "net_per_contract_usd": 50.0,
             "delta_at_sighting": 0.85},
            "modelled", "good", "would have made +$200.00",
        ),
        (
            {"basis": "underlying_delta_proxy", "net_total_usd": -75.5, "contracts": 2,
             "premium_per_contract_usd": 0.9, "net_per_contract_usd": -37.75,
             "delta_at_sighting": 0.4},
            "modelled", "bad", "would have lost -$75.50",
        ),
        (
            {"basis": "underlying_delta_proxy", "net_total_usd": 0.0, "contracts": 1,
             "premium_per_contract_usd": 1.0, "net_per_contract_usd": 0.0,
             "delta_at_sighting": 0.5},
            "modelled", "flat", "would have made $0.00",
        ),
        (
            {"basis": "underlying_session", "move_from_open_pct": -0.008,
             "max_favourable_excursion_pct": 0.001, "max_adverse_excursion_pct": -0.01,
             "close_beyond_projection": False},
            "unmodelled", "bad", "NOT modelled",
        ),
        (None, "pending", "pending", "pending fill"),
    ],
    ids=["made", "lost", "flat", "unmodelled-down", "pending"],
)
def test_counterfactual_sentences(
    outcome: dict | None, state: str, tone: str, contains: str
) -> None:
    out = run_js("M.counterfactual(" + js(outcome) + ")")
    assert out["state"] == state
    assert out["tone"] == tone
    assert contains in out["headline"]


def test_pending_is_not_flat() -> None:
    """A null outcome means 'not known yet'. Rendering it as 'no edge' is the lie this
    page exists to prevent, so it gets its own state and its own wording."""
    out = run_js("M.counterfactual(null)")
    assert out["tone"] == "pending"
    assert 'not the same as "no edge"' in out["detail"]


# ---------------------------------------------------------------------------
# 3. the veto histogram
# ---------------------------------------------------------------------------

WEEKS = [
    {"week_start": "2026-03-02", "week_end": "2026-03-08",
     "by_condition": {"T4": 2, "T3": 2, "T2": 1, "T1": 1, "T2b": 1},
     "pending_by_condition": {"T6": 3}},
    {"week_start": "2026-03-09", "week_end": "2026-03-15",
     "by_condition": {"T4": 2, "T3": 2, "T5": 1},
     "pending_by_condition": {"T5": 1}},
]


def test_veto_histogram_orders_conditions_by_the_rulebook_not_by_count() -> None:
    """T4 vetoed 4 times and T3 4 times, but the axis reads T1,T2,T2b,T3,T4 in rulebook
    order — a chart sorted by frequency makes the checklist look like a leaderboard."""
    out = run_js("M.vetoHistogram(" + js(WEEKS) + ")")
    assert [c["id"] for c in out["conditions"]] == ["T1", "T2", "T2b", "T3", "T4", "T5"]
    assert {c["id"]: c["total"] for c in out["conditions"]} == {
        "T1": 1, "T2": 1, "T2b": 1, "T3": 4, "T4": 4, "T5": 1,
    }
    assert out["max"] == 7, "week 1 stacks T1+T2+T2b+T3(2)+T4(2) = 7"
    assert out["empty"] is False


def test_veto_histogram_folds_t3a_and_t3b_into_the_t3_group() -> None:
    """`analysis.rollup` scores the T3 group; the bar chart must not double-count T3a and
    T3b as two separate vetoes when the group fired once."""
    weeks = [{"week_start": "2026-03-02", "week_end": "2026-03-08",
              "by_condition": {"T3a": 1, "T3b": 1, "T4": 2}}]
    out = run_js("M.vetoHistogram(" + js(weeks) + ")")
    assert {c["id"]: c["total"] for c in out["conditions"]} == {"T3": 2, "T4": 2}
    assert out["bars"][0]["total"] == 4


def test_veto_histogram_drops_zero_segments_and_keeps_pending_separate() -> None:
    out = run_js("M.vetoHistogram(" + js(WEEKS) + ")")
    first, second = out["bars"]
    assert [s["condition"] for s in first["segments"]] == ["T1", "T2", "T2b", "T3", "T4"]
    assert [s["condition"] for s in second["segments"]] == ["T3", "T4", "T5"]
    assert sum(s["value"] for s in first["segments"]) == first["total"]
    assert first["pending"]["T6"] == 3, "pending is carried, not counted as a veto"


def test_veto_histogram_empty_state_is_empty_not_zero() -> None:
    out = run_js("M.vetoHistogram([])")
    assert out["empty"] is True and out["max"] == 0 and out["conditions"] == []


# ---------------------------------------------------------------------------
# 4. the hunt plan grid
# ---------------------------------------------------------------------------

CELLS = [
    {"arm": "B", "direction": "call", "kind": "PROPOSAL", "fire": True, "journal_id": 1,
     "conditions": {"T1": {"status": "PASS", "blocking": True, "detail": "T1 OK"},
                    "T2": {"status": "PASS", "blocking": True, "detail": "T2 OK"},
                    "T4": {"status": "PASS", "blocking": True, "detail": "rvol 1.9"}},
     "failed_conditions": [], "pending_conditions": [], "veto_reasons": []},
    {"arm": "B", "direction": "put", "kind": "NO_TRADE", "fire": False, "journal_id": 2,
     "conditions": {"T1": {"status": "FAIL", "blocking": True, "detail": "T1 NOT: stack inverted"},
                    "T3a": {"status": "FAIL", "blocking": True, "detail": "T3 group veto"},
                    "T3b": {"status": "FAIL", "blocking": False, "detail": "folded into T3a"},
                    "T6": {"status": "PENDING", "blocking": True, "detail": "T6 pending"}},
     "failed_conditions": ["T1", "T3a"], "pending_conditions": ["T6"],
     "veto_reasons": ["T1: veto — T1 NOT: stack inverted", "T3a: veto — T3 group veto"]},
    # Arm C has no CALL cell in the journal at all, and no conditions map either — the
    # two shapes the grid has to carry: a hole, and a row that only has veto reasons.
    {"arm": "C", "direction": "put", "kind": "NO_TRADE", "fire": False, "journal_id": 3,
     "conditions": {}, "failed_conditions": ["T5"], "pending_conditions": [],
     "veto_reasons": ["T5: veto — earnings day veto"]},
]


def test_hunt_grid_is_direction_by_arm_with_holes_marked() -> None:
    out = run_js("M.huntGrid(" + js(CELLS) + ")")
    assert out["arms"] == ["B", "C"]
    assert [row["direction"] for row in out["rows"]] == ["call", "put"]
    # call/C is absent from the journal; the grid must carry an explicit null so the
    # renderer can print "no cell" instead of silently omitting the column.
    assert out["rows"][0]["arms"][0]["journal_id"] == 1
    assert out["rows"][0]["arms"][1] is None
    assert out["rows"][1]["arms"][1]["journal_id"] == 3
    assert out["empty"] is False
    assert len(out["vetoed"]) == 2


def test_condition_chips_cover_every_id_in_rulebook_order() -> None:
    chips = run_js("M.conditionChips(" + js(CELLS[1]) + ")")
    assert [c["id"] for c in chips] == ["T1", "T3a", "T3b", "T6"], "T2/T2b/T4/T5 do not apply here"
    by_id = {c["id"]: c for c in chips}
    assert by_id["T1"]["tone"] == "fail" and by_id["T1"]["blocking"] is True
    assert by_id["T3b"]["tone"] == "soft", "a non-blocking failure is advisory, not a veto"
    assert by_id["T6"]["tone"] == "pending"
    assert by_id["T1"]["reason"] == "T1 NOT: stack inverted", "the veto reason rides on hover"


def test_condition_chips_fall_back_to_failed_ids_without_a_conditions_map() -> None:
    chips = run_js("M.conditionChips(" + js(CELLS[2]) + ")")
    assert [c["id"] for c in chips] == ["T5"]
    assert chips[0]["reason"].startswith("T5: veto"), "the veto reason is all we have"


def test_event_veto_banner_never_claims_clearance_it_did_not_earn() -> None:
    unknown = run_js("M.eventVetoBanner({available: false})")
    assert unknown["level"] == "unknown" and "UNKNOWN" in unknown["text"]
    active = run_js(
        "M.eventVetoBanner({available: true, active: true,"
        " kinds: ['fomc'], reason: 'T5 NOT: FOMC'})"
    )
    assert active["level"] == "bad" and "FOMC" in active["text"]
    clear = run_js("M.eventVetoBanner({available: true, active: false, kinds: []})")
    assert clear["level"] == "ok" and "none" in clear["text"]


# ---------------------------------------------------------------------------
# 5. arms cards
# ---------------------------------------------------------------------------

ARM = {
    "arm": "C", "bankroll": 10000.0, "deployed": 260.0, "cash": 9740.0,
    "realized_pnl": 0.0, "vs_control": -200.0, "return_on_bankroll": 0.0,
    "closed_positions": 2, "open_positions": [
        {"id": 9, "symbol": "SPY", "contract": "SPY260619C00510000", "status": "OPEN",
         "qty": 20, "entry_price": 13.0, "entry_ts": "2026-03-09T14:00:00Z", "exit_ts": None,
         "exit_price": None, "pnl": None, "notional": 260.0},
    ],
    "equity_curve": [{"t": "2026-03-10T19:00:00Z", "equity": 10.0},
                     {"t": "2026-03-12T19:00:00Z", "equity": 0.0}],
    "shadow_roll": {"legs": [{"id": 1, "expiry": "2026-06-19", "pnl": 200.0}], "total_pnl": 200.0},
}


def test_arm_summary_never_marks_to_market() -> None:
    """The journal holds no marks. `last`/`unrealized` must be null, and the row must
    carry the reason, so a blank cell is a stated fact rather than a rendering bug."""
    out = run_js("M.armSummary(" + js(ARM) + ")")
    position = out["positions"][0]
    assert position["last"] is None and position["unrealized"] is None
    assert "not computed" in position["notes"]
    assert out["openCount"] == 1 and out["closed"] == 2
    assert out["isControl"] is False


def test_arm_summary_calls_an_arm_with_no_history_a_closed_window_not_a_flat_curve() -> None:
    out = run_js(
        "M.armSummary("
        + js({**ARM, "equity_curve": [], "closed_positions": 0, "open_positions": []})
        + ")"
    )
    assert out["windowOpen"] is False
    assert out["curve"] == []


def test_mismatches_group_by_arm() -> None:
    out = run_js(
        "(() => { const m = M.mismatchesByArm([{arm: 'C', code: 'no_bankroll'},"
        " {arm: 'C', code: 'over_allocated'}, {arm: 'B', code: 'missing_pnl'}]);"
        " return [...m.entries()].map(([arm, flags]) => [arm, flags.length]); })()"
    )
    assert out == [["C", 2], ["B", 1]]


# ---------------------------------------------------------------------------
# 6. ledger: sorting, pagination, immutable references
# ---------------------------------------------------------------------------


def test_sorting_flips_direction_and_resets_on_a_new_column() -> None:
    out = run_js(
        "(() => { const c = {sort: 'ts'}, s = {sort: 'ts'}, a = {sort: 'arm'};"
        " return [M.nextSort(c, 'ts', 'asc'), M.nextSort(c, 'ts', 'desc'),"
        " M.nextSort(a, 'ts', 'desc'), M.nextSort({sort: null}, 'ts', 'asc')]; })()"
    )
    assert out[0] == {"sort": "ts", "order": "desc"}, "clicking the sorted column flips it"
    assert out[1] == {"sort": "ts", "order": "asc"}
    assert out[2] == {"sort": "arm", "order": "asc"}, "a new column starts ascending"
    assert out[3] == {"sort": "ts", "order": "asc"}, "an unsortable column changes nothing"


def test_sort_indicator_only_marks_the_active_column() -> None:
    out = run_js(
        "(() => [M.sortIndicator({sort: 'ts'}, 'ts', 'desc'),"
        " M.sortIndicator({sort: 'ts'}, 'arm', 'desc'),"
        " M.sortIndicator({sort: null}, 'ts', 'desc')])()"
    )
    assert out[0] == " ▼" and out[1] == "" and out[2] == ""


def test_ledger_columns_declare_what_is_sortable() -> None:
    """Only seven columns exist and `reasoning` is explicitly not sortable. A header that
    looks clickable and is not is worse than one that says so."""
    columns = run_js("M.LEDGER_COLUMNS")
    keys = [c["key"] for c in columns]
    assert keys == ["id", "ts", "arm", "kind", "symbol", "conviction", "reasoning", "created_at"]
    sortable = {c["key"] for c in columns if c["sort"]}
    assert sortable == {"id", "ts", "arm", "kind", "symbol", "conviction", "created_at"}


def test_reference_chain_follows_rows_it_can_see_and_flags_the_rest() -> None:
    """#9 corrects #8, #8 corrects #7, and #7 is not on this page. The chain is followed
    as far as the loaded rows allow and the unloaded end is marked, not dropped."""
    rows = {
        9: {"id": 9, "references": [8]},
        8: {"id": 8, "references": [7]},
    }
    out = run_js(
        "M.referenceChain({id: 9, references: [8]}, new Map("
        + js([[k, v] for k, v in rows.items()])
        + "))"
    )
    assert [c["id"] for c in out] == [8, 7]
    assert out[0]["known"] is True and out[0]["depth"] == 0
    assert out[1]["known"] is False, "a reference to an unloaded row is not a link"
    assert out[1]["from"] == 8


def test_reference_chain_terminates_on_a_cycle() -> None:
    out = run_js(
        "M.referenceChain({id: 1, references: [2]}, new Map([[2, {id: 2, references: [1]}]]))"
    )
    assert [c["id"] for c in out] == [2], "a self-referencing pair must not loop"


def test_page_info_is_honest_about_zero_rows() -> None:
    out = run_js("M.pageInfo({total: 0, page: 1, per_page: 50, pages: 0})")
    assert out["empty"] is True and out["first"] == 0 and out["last"] == 0
    assert out["hasPrev"] is False and out["hasNext"] is False
    mid = run_js("M.pageInfo({total: 137, page: 2, per_page: 50, pages: 3})")
    assert (mid["first"], mid["last"], mid["hasPrev"], mid["hasNext"]) == (51, 100, True, True)
    last = run_js("M.pageInfo({total: 137, page: 3, per_page: 50, pages: 3})")
    assert (last["first"], last["last"], last["hasNext"]) == (101, 137, False)


# ---------------------------------------------------------------------------
# 7. formatting
# ---------------------------------------------------------------------------


def test_formatters_keep_missing_and_zero_apart() -> None:
    out = run_js(
        "({zero: F.usd(0), missing: F.usd(null), pos: F.usd(200), neg: F.usd(-5),"
        " pxMissing: F.px(null), pctZero: F.pct(0), pctNeg: F.pct(-0.0005)})"
    )
    assert out["zero"] == "$0.00" and out["missing"] == "—", "a dash and a zero are different facts"
    assert out["pos"] == "+$200.00" and out["neg"] == "-$5.00"
    assert out["pxMissing"] == "—" and out["pctZero"] == "0.00%"
    assert out["pctNeg"] == "-0.05%"


def test_truncate_prefers_a_word_boundary() -> None:
    out = run_js(
        "[F.truncate('one two three four five', 12), F.truncate('short', 40),"
        " F.truncate(null, 10)]"
    )
    assert out[0] == "one two…" and out[1] == "short" and out[2] == ""


def test_noshot_index_survives_an_empty_payload() -> None:
    """Before the window opens every endpoint answers with an empty body. Nothing here
    may throw on one."""
    out = run_js(
        "(() => { const i = M.indexNoshots([]); return {found: M.noshotsForDecision(i, 1).length,"
        " hist: M.vetoHistogram([]).empty, grid: M.huntGrid([]).empty,"
        " pop: M.markerPopup(null, []).counterfactual.state}; })()"
    )
    assert out == {"found": 0, "hist": True, "grid": True, "pop": "pending"}
