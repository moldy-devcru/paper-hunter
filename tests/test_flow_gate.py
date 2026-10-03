"""The T6 flow-gate reader (`executor/flow_gate.py`) and the loop's forwarding of it.

Two things are pinned here, and they are the two ways this seam can quietly break the
experiment:

1. **The loop must actually use what it is handed.** ``run_loop`` accepted ``iv_rank``
   and ``flow_gate`` and did not pass them to ``run_once`` (fixed 2026-10-03), so every
   CLI session re-verified T5/T6 as PENDING — blocking conditions that no market could
   satisfy. A dropped parameter is a silent strategy halt, so the forwarding is tested
   directly rather than inferred.
2. **The gate says which session it read.** T6 is ``EOD_only``, so an intraday read can
   only be the previous session's confirmed row, and a bare ratio in a journal row is
   not auditable without that date.

Offline: temp SQLite journals, no network, no credentials, nothing under ``/opt``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from config.loader import DEFAULT_RULES_PATH, load_rules_text
from executor.checklist import FlowGate
from executor.flow_gate import FlowGateError, flow_gate_from_journal
from executor.hunt_plan import ArmPlan, HuntPlan, StaticMarketData, build_hunt_plan
from executor.position_manager import MemoryJournalSink
from executor.watch_loop import DryRunRouter, StaticWatchData, WatchState, run_loop, run_once
from journal.store import FlowBaselineRow, append_flow_baseline, init_db

DAY = dt.date(2026, 10, 2)


def washout_series():
    """The rehearsal's deterministic signal bar — see ``executor.rehearsal.FIRING_PARAMS``."""
    from executor.rehearsal import firing_series

    return firing_series()


def make_row(
    date: dt.date,
    *,
    ratio_call: float | None,
    ratio_put: float | None,
    baseline_days: int = 20,
    lookback: int = 20,
) -> FlowBaselineRow:
    return FlowBaselineRow(
        date=date.isoformat(),
        underlying="SPY",
        feed="indicative",
        is_delayed=True,
        session_spot=625.0,
        deep_otm_threshold_pct=3.0,
        deep_otm_call_volume=1000.0,
        deep_otm_put_volume=2000.0,
        deep_otm_total_volume=3000.0,
        call_volume_by_distance={"4": 1000.0},
        put_volume_by_distance={"4": 2000.0},
        baseline_lookback_days=lookback,
        baseline_days=baseline_days,
        baseline_call_mean=500.0,
        baseline_put_mean=1000.0,
        ratio_call=ratio_call,
        ratio_put=ratio_put,
        strategy_version="v1.0.0+test",
        ts=f"{date.isoformat()}T21:00:00Z",
    )


@pytest.fixture()
def conn(tmp_path: Path):
    connection = init_db(tmp_path / "journal.db")
    yield connection
    connection.close()


def test_gate_reads_the_newest_confirmed_session_before_the_trading_day(conn) -> None:
    append_flow_baseline(conn, make_row(dt.date(2026, 9, 30), ratio_call=1.4, ratio_put=2.2))
    append_flow_baseline(conn, make_row(dt.date(2026, 10, 1), ratio_call=2.6, ratio_put=0.4))

    read = flow_gate_from_journal(
        conn, as_of=DAY, side="call", multiplier=2.0
    )

    assert read.source_date == "2026-10-01"
    assert read.gate.ratio == pytest.approx(2.6)
    assert read.gate.status == "pass"  # 2.6 >= 2.0
    assert read.gate.eod_confirmed is True
    assert "2026-10-01" in read.summary_line()


def test_the_same_read_fails_on_the_put_side_which_is_the_whole_point_of_a_side(conn) -> None:
    """t6_flow.side is trade_direction: one multiplier, two populations. The reader
    must not quietly hand the call ratio to a put cell."""
    append_flow_baseline(conn, make_row(dt.date(2026, 10, 1), ratio_call=2.6, ratio_put=0.4))

    call = flow_gate_from_journal(conn, as_of=DAY, side="call", multiplier=2.0)
    put = flow_gate_from_journal(conn, as_of=DAY, side="put", multiplier=2.0)

    assert (call.gate.status, put.gate.status) == ("pass", "fail")
    assert put.gate.ratio == pytest.approx(0.4)


def test_a_ratio_over_a_short_baseline_is_pending_not_a_verdict(conn) -> None:
    """A 3-session ratio is not the ratified 20-session ratio, so the gate refuses to
    arbitrate on it — the same exclusion the calibration reader applies."""
    append_flow_baseline(conn, make_row(dt.date(2026, 10, 1), ratio_call=9.0, ratio_put=9.0,
        baseline_days=3))

    read = flow_gate_from_journal(conn, as_of=DAY, side="call", multiplier=2.0)

    assert read.gate.status == "pending"
    assert read.gate.ratio is None
    assert "3-session baseline" in read.reason


def test_a_null_ratio_is_pending_with_the_reason(conn) -> None:
    append_flow_baseline(conn, make_row(dt.date(2026, 10, 1), ratio_call=None, ratio_put=None))
    read = flow_gate_from_journal(conn, as_of=DAY, side="call", multiplier=2.0)
    assert read.gate.status == "pending" and "no ratio_call" in read.reason


def test_no_rows_at_all_is_pending_not_unknown_or_pass(conn) -> None:
    read = flow_gate_from_journal(conn, as_of=DAY, side="call", multiplier=2.0)
    assert read.gate.status == "pending"
    assert read.source_date is None
    assert "has not written a session" in read.reason


def test_an_unfrozen_multiplier_cannot_pass_even_with_a_great_ratio(conn) -> None:
    """The shipped rulebook has N = null. A readable ratio with no threshold has not
    evaluated anything, and saying 'pass' here would invent a calibration."""
    append_flow_baseline(conn, make_row(dt.date(2026, 10, 1), ratio_call=50.0, ratio_put=50.0))

    read = flow_gate_from_journal(conn, as_of=DAY, side="call", multiplier=None)

    assert read.gate.status == "pending"
    assert read.gate.ratio == pytest.approx(50.0)
    assert "calibration_pending" in read.reason


def test_a_same_session_row_is_not_read_unless_asked(conn) -> None:
    """Looking into the same session's flow row is looking into the future."""
    append_flow_baseline(conn, make_row(DAY, ratio_call=3.0, ratio_put=3.0))
    assert flow_gate_from_journal(conn, as_of=DAY, side="call", multiplier=2.0).source_date is None
    assert (
        flow_gate_from_journal(
            conn, as_of=DAY, side="call", multiplier=2.0, include_session=True
        ).source_date
        == DAY.isoformat()
    )


def test_an_impossible_side_is_refused_rather_than_defaulted(conn) -> None:
    with pytest.raises(FlowGateError):
        flow_gate_from_journal(conn, as_of=DAY, side="sideways", multiplier=2.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the loop forwards what it is given
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def calibrated_rules():
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": 2.0,
        "calibration_pending": False,
        "reason": "test fixture: calibrated multiplier",
    }
    return load_rules_text(yaml.safe_dump(data))


def _firing_plan(rules, series) -> HuntPlan:
    """A plan whose B/call cell carries a watch level for the loop to verify.

    The washout bar is the fixture the rehearsal uses (see ``executor.rehearsal``):
    the cell is NO_TRADE on the close but carries the T1 level the reclaim has to
    reach. A cell that passed everything would be useless here — ``compute_triggers``
    only emits levels for blocking conditions that have not passed, so a green cell has
    nothing to watch and the loop never re-verifies it. That property is a rehearsal
    finding, not something to work around silently: the loop-forwarding test needs a
    cell that is actually armed, so it builds one the way production would.
    """
    provider = StaticMarketData(series=series, chain=None)
    plan = build_hunt_plan(
        data=provider,
        rules=rules,
        day=series.bars[-1].t.date() + dt.timedelta(days=3),
        spot=series.bars[-1].c,
    )
    armed = [
        c
        for c in plan.arms
        if c.arm == "B"
        and c.direction == "call"
        and any(t.actionable for t in c.triggers)
    ]
    assert armed, "fixture stopped producing an entry-capable cell — see the rehearsal report"
    return plan


def test_run_loop_forwards_iv_rank_and_flow_gate_into_every_tick(calibrated_rules) -> None:
    """The regression test for the dropped parameters: with the plan's own cell
    carrying an IV rank and the gate handed in, the loop's re-verification must SEE
    them. Before the fix both arrived as ``None``, T5/T6 went PENDING, and no cell
    could ever fire."""
    series = washout_series()
    plan = _firing_plan(calibrated_rules, series)
    # The loop only re-verifies a cell whose price trigger has been REACHED, so the
    # spot is pushed through the armed cell's level (the reclaim side of the washout).
    levels = [
        t.level
        for c in plan.arms
        if c.arm == "B" and c.direction == "call"
        for t in c.triggers
        if t.actionable and t.level is not None
    ]
    assert levels, "no actionable level to reach"
    # The tick must fall on the plan's own session, inside arm B's entry window, and the
    # snapshot must be stamped relative to it: a clock from another day (or an unstamped
    # snapshot) makes the loop skip the session entirely, which would make this test pass
    # vacuously — it would assert "nothing was re-verified" for the wrong reason.
    when = dt.datetime.combine(
        plan.day, dt.time(10, 20), tzinfo=dt.timezone(dt.timedelta(hours=-4))
    ).astimezone(dt.UTC)
    provider = StaticWatchData(
        spot=max(levels) + 1.0, daily=series, fetched_at=when - dt.timedelta(seconds=5)
    )
    router = DryRunRouter()
    state = WatchState.initial(day=plan.day, plan=plan, rules=calibrated_rules)
    iv_rank = 31.5
    gate = FlowGate(status="pass", ratio=3.0, eod_confirmed=True)

    captured: list[tuple[float | None, object]] = []
    import executor.watch_loop as wl

    original = wl.reverify_cell

    def spy(cell, **kwargs):  # type: ignore[no-untyped-def]
        captured.append((kwargs.get("iv_rank"), kwargs.get("flow_gate")))
        return original(cell, **kwargs)

    wl.reverify_cell = spy  # type: ignore[assignment]
    try:
        results = run_loop(
            provider=provider,
            rules=calibrated_rules,
            state=state,
            router=router,
            journal=MemoryJournalSink(),
            max_ticks=1,
            sleep=lambda _s: None,
            clock=lambda: when,
            iv_rank=iv_rank,
            flow_gate=gate,
        )
    finally:
        wl.reverify_cell = original  # type: ignore[assignment]

    assert len(results) == 1
    # Either a trigger fired and the cell was re-verified with our values, or nothing
    # fired at all — in which case there is nothing to assert about the forwarding and
    # the test would be vacuous, so require the re-verification to have happened.
    assert captured, "no cell was re-verified; the test did not exercise the seam"
    assert all(rank == iv_rank for rank, _ in captured)
    assert all(g == gate for _, g in captured)


def test_run_once_defaults_both_to_none_so_a_bare_loop_still_reports_pending(
    calibrated_rules,
) -> None:
    """Documenting the default: an unwired loop gets PENDING, not a guess. The rehearsal
    report is where that turns into a NO-GO item; this test is where it stays true."""
    series = washout_series()
    plan = _firing_plan(calibrated_rules, series)
    when = dt.datetime.combine(
        plan.day, dt.time(10, 20), tzinfo=dt.timezone(dt.timedelta(hours=-4))
    ).astimezone(dt.UTC)
    levels = [
        t.level
        for c in plan.arms
        if c.arm == "B" and c.direction == "call"
        for t in c.triggers
        if t.actionable and t.level is not None
    ]
    provider = StaticWatchData(
        spot=max(levels) + 1.0, daily=series, fetched_at=when - dt.timedelta(seconds=5)
    )
    state = WatchState.initial(day=plan.day, plan=plan, rules=calibrated_rules)

    result = run_once(
        provider=provider,
        rules=calibrated_rules,
        state=state,
        now=when,
    )

    for cell_result in result.reverified.values():
        arm = cell_result.arm
        assert cell_result.conditions["T5"].status == "PENDING"
        # T6 is arm B's gate; on arm C it is legitimately SKIPPED, and a loop handed no
        # gate at all must not turn that into a pass.
        assert cell_result.conditions["T6"].status == (
            "PENDING" if arm == "B" else "SKIPPED"
        )
        assert not cell_result.fire
    # ArmPlan import is used by the type checkers only; referenced here to keep ruff
    # honest about the module's public surface in this file.
    assert ArmPlan is not None
