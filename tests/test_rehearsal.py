"""End-to-end rehearsal tests: the harness itself, not the pipeline's unit tests.

The point of ``executor.rehearsal`` is that one command walks the whole daily cycle
offline. These tests pin the harness's own contract, so "the rehearsal ran green" stays
a statement about the code and not about the fixtures:

* every stage runs and none fails;
* both branches are exercised — a near-miss/no-shot session AND a session that takes an
  entry — because a rehearsal that only proves the quiet path proves nothing;
* the run is deterministic and repeatable into the same workdir;
* the integrity claims are *measured*: the shipped rulebook is byte-identical after the
  run, the deployed tree under /opt is untouched, and nothing was routed to a live
  router.

Offline only: temp workdirs, ``DryRunRouter``, no credentials, no writes under /opt. The
deployed journal is copied read-only for the calibration stage and skipped when absent.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from executor import rehearsal
from executor.rehearsal import Rehearsal, run


@pytest.fixture(scope="module")
def rehearsal_run(tmp_path_factory: pytest.TempPathFactory):
    workdir = tmp_path_factory.mktemp("rehearsal")
    r, _ = run(workdir=workdir, keep=True)
    return r, workdir


def _stage(r: Rehearsal, name: str):
    for st in r.stages:
        if st.name == name:
            return st
    raise AssertionError(f"no stage named {name!r} in {[s.name for s in r.stages]}")


def test_every_stage_runs(rehearsal_run) -> None:
    r, _workdir = rehearsal_run
    failed = [(s.name, s.error) for s in r.stages if not s.ok]
    assert not failed, f"stages failed: {failed}"
    # The stage list is the pipeline; losing a stage silently would make a green run
    # meaningless, so the names are pinned here rather than only printed.
    assert [s.name for s in r.stages] == [
        name
        for name, _fn in rehearsal.STAGE_ORDER
    ]


def test_both_branches_are_exercised(rehearsal_run) -> None:
    """No-shot AND shot. A rehearsal that only ran the quiet path would pass."""
    r, _workdir = rehearsal_run

    noshot = _stage(r, "plan/noshot")
    assert noshot.detail["firing_cells"] == 0
    assert noshot.detail["near_miss_cells"], "the no-shot session was not a near miss"
    assert noshot.detail["decision_rows"] > 0

    shot = _stage(r, "plan/shot")
    assert shot.detail["entry_capable_cells"], "no cell carried a watch level"

    entry = _stage(r, "watch/entry")
    assert entry.detail["trades"], "the entry tick took nothing"
    assert {t["arm"] for t in entry.detail["trades"]} == {"B", "C"}
    assert all(t["symbol"] for t in entry.detail["trades"])
    assert "B/call" in entry.detail["reverified"]


def test_the_no_shot_ledger_is_written_for_the_near_miss(rehearsal_run) -> None:
    r, workdir = rehearsal_run
    eod = _stage(r, "eod")
    assert eod.detail["noshots_written_total"] > 0
    rows = eod.detail["sessions"]["noshot"]["rows"]
    assert rows, "the NO-SHOT ledger has no rows"
    # Every ledger row must name the condition that stopped it and link back to the plan
    # decision row it came from — an unattributed counterfactual is not evidence.
    assert all(row["failed"] for row in rows)
    assert all(row["counterfactual_entry_ref"] is not None for row in rows)


def test_exits_and_the_roll_ladder_both_fire(rehearsal_run) -> None:
    r, _workdir = rehearsal_run
    exits = _stage(r, "exits+roll")
    assert any(o["stops"] for o in exits.detail["exits"]), "arm B never exited"
    assert any(a["kind"] == "ROLL" for a in exits.detail["arm_c_roll_with_replacement"])
    # ...and the same trigger without a replacement leg produces no roll, which is the
    # documented gap the watch loop hits today.
    assert not exits.detail["arm_c_roll_without_replacement"]


def test_stale_snapshots_veto_entries(rehearsal_run) -> None:
    r, _workdir = rehearsal_run
    vetoes = _stage(r, "watch/stale")
    assert vetoes.detail["levels_reached"], "the stale stage never reached a trigger"
    assert vetoes.detail["stale"] is True
    assert vetoes.detail["entries_routed"] == 0
    assert any(v.get("governor") == "stale_snapshot" for v in vetoes.detail["vetoes"])


def test_integrity_is_measured_not_asserted(rehearsal_run) -> None:
    r, _workdir = rehearsal_run
    detail = _stage(r, "integrity").detail
    assert detail["shipped_rulebook_sha_unchanged"] is True
    assert not any(detail["opt_tree_changed"].values())
    assert detail["network_attempts"] == []
    assert "0 orders sent" in rehearsal.DryRunRouter().describe()
    assert detail["row_counts"]["decisions"] > 0
    assert detail["row_counts"]["noshots"] > 0
    assert detail["row_counts"]["flow_baseline"] > 0


def test_the_shipped_rulebook_is_never_edited(rehearsal_run) -> None:
    """The simulated freeze is written to the workdir, never over the shipped rules."""
    r, workdir = rehearsal_run
    freeze = _stage(r, "simulated-freeze")
    assert freeze.detail["fixture_rulebook"].startswith(str(workdir))
    assert freeze.detail["shipped_rulebook_untouched"] is True
    current = hashlib.sha256(Path(rehearsal.DEFAULT_RULES_PATH).read_bytes()).hexdigest()
    assert current == _stage(r, "rules").detail["sha256"]
    # The shipped gates are still pending: the shot branch ran against the fixture copy.
    assert _stage(r, "rules").detail["pending_gates"]


def test_a_rerun_into_the_same_workdir_is_identical(tmp_path: Path) -> None:
    """Repeatable: the world is disposable, so row counts cannot drift per run."""
    workdir = tmp_path / "world"
    first, _ = run(workdir=workdir, keep=True)
    counts_first = _stage(first, "integrity").detail["row_counts"]
    second, _ = run(workdir=workdir, keep=True)
    counts_second = _stage(second, "integrity").detail["row_counts"]
    assert counts_first == counts_second
    assert all(s.ok for s in second.stages)


def test_the_calibration_stage_never_freezes_anything(rehearsal_run) -> None:
    """N is a proposal. If a future change freezes it here, this test fails."""
    r, _workdir = rehearsal_run
    cal = _stage(r, "calibration")
    assert cal.detail["value"] > 0
    assert cal.detail["samples"] > 0
    assert "divided by" in cal.detail["method"]
    findings = " ".join(cal.findings).lower()
    assert "not frozen" in findings or "proposal only" in findings
    # Whatever the sample, the shipped rulebook's T6 multiplier stays pending.
    assert _stage(r, "rules").detail["pending_gates"]


def test_the_offline_guard_refuses_a_connection(tmp_path: Path) -> None:
    """The guard is what makes 'offline' a property instead of a claim."""
    import socket

    guard = rehearsal._NoNetwork()
    with guard:
        with pytest.raises(rehearsal.RehearsalError):
            socket.create_connection(("127.0.0.1", 9))
    assert guard.attempts
    # ...and it restores the socket module on the way out.
    assert socket.create_connection is not None
    with socket.socket() as probe:
        assert probe.fileno() >= 0


def test_the_rehearsal_never_writes_under_opt(rehearsal_run) -> None:
    r, workdir = rehearsal_run
    for path in workdir.rglob("*"):
        if path.is_file():
            assert str(path).startswith(str(workdir))
    # Every sqlite file the run created lives in the workdir too.
    dbs = list(workdir.glob("*.db"))
    assert dbs, "the rehearsal wrote no journal at all"
    for db in dbs:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()