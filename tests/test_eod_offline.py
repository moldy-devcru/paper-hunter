"""The EOD pass: a trade is not a skip, and a counterfactual knows its decision.

The full-cycle rehearsal of 2026-10-03 found the bug these tests pin shut: ``cmd_eod``
called ``build_noshots`` without ``taken`` and without ``decision_ids``. The first meant a
session that both saw the setup *and* traded still got a row worded as a skip; the second
meant every counterfactual landed with ``counterfactual_entry_ref = NULL``, severed from
the decision that produced it.

The other half of the fix is ``--offline``: the EOD command built a live
``AlpacaClient`` unconditionally, so it could not be run in a rehearsal at all. These
tests drive the *real* ``executor.main.main(["eod", ...])`` over a fixture transport, so
the command under test is the shipped one — parser, client construction, streak, ledger
and journal close included. No credential is read and no socket is opened.

The cells under test are ``B/call`` and ``C/call``: both are one condition (T4) from
firing on the same snapshot, so both are sightings and both would be recorded — which is
what makes a missing ``taken`` or a missing link visible. (Arm B's put cell cannot be
used: the same snapshot that arms the call breaks T1/T2/T3a for the put, so the put is
not a sighting and nothing would be written for it either way.)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest
import yaml
from fixtures_synthetic import series_from, synthetic_daily_payload, zero_dte_chain

from config.loader import DEFAULT_RULES_PATH, load_rules_text
from executor.checklist import BollingerState, FlowGate, IndicatorSnapshot
from executor.hunt_plan import ArmPlan, HuntPlan, make_arm_plan
from executor.main import _occ_right, _taken_keys, build_parser, main, write_plan_file
from executor.rehearsal import _offline_routes
from executor.snapshot_builder import SnapshotResult
from journal.store import (
    DecisionEntry,
    append_decision,
    get_decision,
    init_db,
    list_decisions,
    list_noshots,
)

UTC = dt.UTC
DAY = dt.date(2025, 6, 2)
SPOT = 625.0
SESSION_BAR = dict(o=624.0, h=627.5, low=623.0, c=626.0, v=55_000_000.0, n=480_000)
PLAN_TS = "2025-06-02T12:35:00Z"  # 08:35 ET: the pre-market plan row
TRADE_TS = "2025-06-02T14:20:00Z"  # 10:20 ET: mid-session


@pytest.fixture(scope="module")
def calibrated():
    """T6's multiplier calibrated, so a cell can get one condition from firing."""
    return load_rules_text(yaml.safe_dump(_calibrated_yaml()))


@pytest.fixture
def rules_path(tmp_path: Path) -> Path:
    """The rulebook on disk, which is the only form ``--rules`` accepts."""
    path = tmp_path / "rules.test.json"
    path.write_text(yaml.safe_dump(_calibrated_yaml()), encoding="utf-8")
    return path


def _calibrated_yaml() -> dict:
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": 2.0,
        "calibration_pending": False,
        "reason": "test fixture: calibrated multiplier",
    }
    return data


# ---------------------------------------------------------------------------
# fixtures: bars, cells, plan, journal rows
# ---------------------------------------------------------------------------


def _bars_payload(day: dt.date = DAY) -> dict:
    """260 synthetic daily bars whose *last* bar is the session under test."""
    payload = synthetic_daily_payload(count=260)
    payload["bars"][-1] = {
        "t": dt.datetime.combine(day, dt.time(21, 0), tzinfo=UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        **SESSION_BAR,
        "vw": SESSION_BAR["c"] - 0.01,
    }
    return payload


def _snapshot(**overrides) -> IndicatorSnapshot:
    """Everything passing except T4 (relvol 1.1 < 1.5): exactly one condition away."""
    base = IndicatorSnapshot(
        close=SPOT,
        ema_fast=620.0,
        ema_slow=560.0,
        rsi=58.0,
        macd_line=1.2,
        macd_signal=1.0,
        macd_hist=0.2,
        macd_hist_prev=0.1,
        macd_cross_direction=None,
        macd_cross_age_hours=None,
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
        relvol=1.1,
        iv_rank=40.0,
        is_event_day=False,
        event_kinds=(),
        flow_gate=FlowGate(status="pass", ratio=2.4, eod_confirmed=True),
    )
    return dataclasses.replace(base, **overrides)


def _cell(rules, *, arm: str = "B") -> ArmPlan:
    return make_arm_plan(
        arm=arm,
        direction="call",
        snapshot_result=SnapshotResult(snapshot=_snapshot(), pending_reasons=(), bars_used=260),
        rules=rules,
    )


def _plan(rules, cells=("B", "C")) -> HuntPlan:
    return HuntPlan(
        day=DAY,
        symbol=rules.strategy.symbol,
        generated_ts=PLAN_TS,
        strategy_version=rules.strategy_version,
        config_sha256=rules.config_sha256,
        arms=tuple(_cell(rules, arm=arm) for arm in cells),
        event_day_veto=False,
        event_reasons=(),
    )


def _plan_row(arm: str, *, ts: str = PLAN_TS, phase: str = "plan") -> dict:
    return {
        "ts": ts,
        "arm": arm,
        "kind": "NO_TRADE",
        "checklist_snapshot": {"indicators": {}},
        "checklist_state": {"phase": phase},
        "reasoning": "plan row for the session",
        "strategy_version": "test",
        "symbol": "SPY",
    }


def _trade_row(arm: str, direction: str = "call", *, ts: str = TRADE_TS, symbol: str | None = None):
    day = dt.date.fromisoformat(ts[:10]).strftime("%Y%m%d")
    right = "C" if direction == "call" else "P"
    occ = symbol if symbol is not None else f"SPY{day}{right}{int(SPOT * 1000):08d}"
    return {
        "ts": ts,
        "arm": arm,
        "kind": "TRADE",
        "checklist_snapshot": {"indicators": {}},
        "checklist_state": {"phase": "watch_entry", "direction": direction},
        "reasoning": f"entry taken for cell {arm}/{direction}",
        "strategy_version": "test",
        "symbol": occ,
    }


def _routes() -> dict:
    return _offline_routes(series_from(_bars_payload()), zero_dte_chain(day=DAY, spot=SPOT))


def _run_eod(tmp_path: Path, rules_path: Path, plan: HuntPlan, journal: list[dict],
             *, routes: dict | None = None) -> int:
    conn = init_db(tmp_path / "journal.db")
    for row in journal:
        append_decision(conn, DecisionEntry(**row))
    conn.close()

    plan_path = write_plan_file(plan, tmp_path / f"plan-{plan.day}.json")
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps({"routes": _routes() if routes is None else routes}), encoding="utf-8"
    )
    return main(
        [
            "--rules",
            str(rules_path),
            "--db",
            str(tmp_path / "journal.db"),
            "--now",
            "2025-06-02T17:05:00-04:00",  # 21:05Z, EOD
            "eod",
            "--plan",
            str(plan_path),
            "--date",
            DAY.isoformat(),
            "--offline",
            "--offline-fixture",
            str(fixture),
        ]
    )


def _rows(tmp_path: Path):
    conn = init_db(tmp_path / "journal.db")
    try:
        return list_noshots(conn, date=DAY.isoformat())
    finally:
        conn.close()


def _cells_of(rows) -> list[str]:
    return [
        f"{r.instrument_hypothesis['arm']}/{r.instrument_hypothesis['direction']}" for r in rows
    ]


def _close_decision(tmp_path: Path):
    conn = init_db(tmp_path / "journal.db")
    try:
        found = [
            d
            for d in list_decisions(conn)
            if (d.checklist_state or {}).get("phase") == "eod_close"
        ]
    finally:
        conn.close()
    assert len(found) == 1, f"expected one eod_close row, got {len(found)}"
    return found[0]


def _ids_by_phase(tmp_path: Path, phase: str) -> dict[str, int]:
    conn = init_db(tmp_path / "journal.db")
    try:
        return {
            d.arm: d.id
            for d in list_decisions(conn)
            if (d.checklist_state or {}).get("phase") == phase
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the bug from the rehearsal, pinned shut
# ---------------------------------------------------------------------------


def test_a_pure_skip_writes_a_row_for_every_sighting_cell(
    tmp_path, rules_path, calibrated, capsys
):
    rc = _run_eod(tmp_path, rules_path, _plan(calibrated), journal=[_plan_row("B"), _plan_row("C")])
    assert rc == 0, capsys.readouterr().out
    assert sorted(_cells_of(_rows(tmp_path))) == ["B/call", "C/call"]

    close = _close_decision(tmp_path)
    assert close.checklist_state["taken"] == []
    assert "nothing traded" in close.reasoning


def test_a_traded_cell_gets_no_counterfactual_row(tmp_path, rules_path, calibrated, capsys):
    """Both cells saw the setup; one of them traded. A trade is not a skip."""
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated),
        journal=[_plan_row("B"), _plan_row("C"), _trade_row("B")],
    )
    assert rc == 0, capsys.readouterr().out
    assert _cells_of(_rows(tmp_path)) == ["C/call"]  # only the cell that did not trade

    close = _close_decision(tmp_path)
    assert close.checklist_state["taken"] == ["B/call"]
    assert "traded ['B/call']" in close.reasoning


def test_a_session_that_traded_every_sighting_cell_writes_nothing(
    tmp_path, rules_path, calibrated, capsys
):
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated),
        journal=[_plan_row("B"), _plan_row("C"), _trade_row("B"), _trade_row("C")],
    )
    assert rc == 0, capsys.readouterr().out
    assert _rows(tmp_path) == []
    assert _close_decision(tmp_path).checklist_state["taken"] == ["B/call", "C/call"]


def test_a_trade_from_another_session_does_not_touch_todays_cells(
    tmp_path, rules_path, calibrated, capsys
):
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated),
        journal=[_plan_row("B"), _plan_row("C"), _trade_row("B", ts="2025-06-01T14:20:00Z")],
    )
    assert rc == 0, capsys.readouterr().out
    assert sorted(_cells_of(_rows(tmp_path))) == ["B/call", "C/call"]


def test_the_traded_cell_is_read_out_of_the_journal_not_the_broker(
    tmp_path, rules_path, calibrated, capsys
):
    """Offline there are no positions to ask, and none are asked for.

    ``--offline`` skips the broker position call entirely; the ledger's ``taken`` comes
    from the journal's TRADE rows, which is the only record that survives a position
    opened and closed inside the session.
    """
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated),
        journal=[_plan_row("B"), _plan_row("C"), _trade_row("B")],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "traded ['B/call']" in out
    assert "position " not in out  # no broker positions were printed


def test_an_unmappable_trade_is_reported_not_silently_believed(
    tmp_path, rules_path, calibrated, capsys
):
    """A TRADE row with no parsable OCC symbol cannot name a cell.

    The cell is then treated as *not* taken — so a row can still be written for a cell
    that traded. That is a data fault, so it is loud on stderr rather than a silent
    lie in the ledger.
    """
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated, cells=("B",)),
        journal=[_plan_row("B"), _trade_row("B", symbol="SPY")],
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "could not be mapped" in captured.err
    assert _cells_of(_rows(tmp_path)) == ["B/call"]


# ---------------------------------------------------------------------------
# taken is parsed from the contract, so arm B's two cells are told apart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("SPY20250602C00625000", "C"),
        ("SPY20250602P00625000", "P"),
        ("SPY", None),  # an equity row: no right letter to find
        ("SPY260102C00625000", "C"),  # a real expiry, just not this session's
        ("SPY20250602X00625000", None),  # not a right letter: refuses rather than guesses
        ("SPYC00625000", None),  # too short to carry an OCC suffix
        ("", None),
        (None, None),
    ],
)
def test_the_occ_right_is_parsed_or_refused(symbol, expected):
    assert _occ_right(symbol) == expected


def test_taken_keys_map_a_trade_onto_its_arm_and_direction():
    entries = [
        DecisionEntry(**_trade_row("B", "call")),
        DecisionEntry(**_trade_row("C", ts="2025-06-02T15:00:00Z")),
        DecisionEntry(**_trade_row("B", "put", ts="2025-06-02T15:05:00Z")),
        DecisionEntry(**_plan_row("B", ts="2025-06-02T12:35:00Z")),
    ]
    taken, unmapped = _taken_keys(entries)
    assert taken == {("B", "call"), ("B", "put"), ("C", "call")}
    assert unmapped == []  # a NO_TRADE row is not a trade, so it is not unmapped


def test_taken_keys_name_the_row_they_could_not_map():
    taken, unmapped = _taken_keys([DecisionEntry(**_trade_row("B", symbol="SPY"))])
    assert taken == set()
    assert len(unmapped) == 1 and "SPY" in unmapped[0]


# ---------------------------------------------------------------------------
# the counterfactual <-> decision link
# ---------------------------------------------------------------------------


def test_every_counterfactual_links_to_its_own_arms_plan_decision(
    tmp_path, rules_path, calibrated, capsys
):
    rc = _run_eod(tmp_path, rules_path, _plan(calibrated), journal=[_plan_row("B"), _plan_row("C")])
    assert rc == 0, capsys.readouterr().out
    plan_ids = _ids_by_phase(tmp_path, "plan")
    refs = {
        f"{r.instrument_hypothesis['arm']}/{r.instrument_hypothesis['direction']}": (
            r.counterfactual_entry_ref
        )
        for r in _rows(tmp_path)
    }
    assert refs == {"B/call": plan_ids["B"], "C/call": plan_ids["C"]}


def test_the_intraday_row_does_not_steal_the_link(tmp_path, rules_path, calibrated, capsys):
    """The watch loop journals its own rows for the same cell intraday.

    Linking to one of those would attribute a counterfactual to a re-verification rather
    than to the plan it came from, so the pre-open row wins.
    """
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated, cells=("B",)),
        journal=[
            _plan_row("B"),
            _plan_row("B", ts="2025-06-02T14:00:00Z", phase="watch"),
        ],
    )
    assert rc == 0, capsys.readouterr().out
    rows = _rows(tmp_path)
    assert len(rows) == 1
    plan_id = _ids_by_phase(tmp_path, "plan")["B"]
    watch_id = _ids_by_phase(tmp_path, "watch")["B"]
    assert rows[0].counterfactual_entry_ref == plan_id
    assert rows[0].counterfactual_entry_ref != watch_id


def test_a_plan_rewritten_before_the_open_is_the_row_that_counts(
    tmp_path, rules_path, calibrated, capsys
):
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated, cells=("B",)),
        journal=[
            _plan_row("B", ts="2025-06-02T12:35:00Z"),
            _plan_row("B", ts="2025-06-02T13:00:00Z"),  # 09:00 ET, still pre-open
        ],
    )
    assert rc == 0, capsys.readouterr().out
    conn = init_db(tmp_path / "journal.db")
    newest = max(
        d.id for d in list_decisions(conn) if (d.checklist_state or {}).get("phase") == "plan"
    )
    conn.close()
    assert _rows(tmp_path)[0].counterfactual_entry_ref == newest


def test_a_counterfactual_with_no_decision_to_link_is_written_but_reported(
    tmp_path, rules_path, calibrated, capsys
):
    """No plan decision row means no link — so the row is written and the gap is loud.

    The sighting is real evidence that the setup was seen, and dropping it over a
    bookkeeping gap would be the worse failure for a ledger whose job is recording what
    was *not* done. But an unattributed counterfactual cannot be traced back to the
    decision that produced it, so it is reported on stderr rather than slipped past.
    """
    rc = _run_eod(tmp_path, rules_path, _plan(calibrated, cells=("B",)), journal=[])
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "plan rows linked 0/1" in captured.out
    assert "no decision link" in captured.err
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0].counterfactual_entry_ref is None


def test_the_link_target_is_still_readable_after_the_run(tmp_path, rules_path, calibrated, capsys):
    rc = _run_eod(tmp_path, rules_path, _plan(calibrated, cells=("B",)), journal=[_plan_row("B")])
    assert rc == 0, capsys.readouterr().out
    ref = _rows(tmp_path)[0].counterfactual_entry_ref
    conn = init_db(tmp_path / "journal.db")
    entry = get_decision(conn, ref)
    conn.close()
    assert entry is not None
    assert entry.arm == "B"
    assert entry.kind == "NO_TRADE"
    assert entry.checklist_state["phase"] == "plan"


def test_the_ledger_and_its_decisions_are_append_only(tmp_path, rules_path, calibrated, capsys):
    """Fixing the call site must not have quietly relaxed the journal's write contract."""
    import sqlite3

    rc = _run_eod(tmp_path, rules_path, _plan(calibrated), journal=[_plan_row("B"), _plan_row("C")])
    assert rc == 0, capsys.readouterr().out
    conn = init_db(tmp_path / "journal.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE noshots SET failed_conditions = '{}' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE noshots SET counterfactual_entry_ref = 99 WHERE id = 1")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the --offline path itself
# ---------------------------------------------------------------------------


def test_the_offline_flag_is_off_by_default():
    args = build_parser().parse_args(["eod"])
    assert args.offline is False
    assert args.offline_fixture is None


def test_offline_without_a_fixture_fails_loudly(tmp_path, rules_path, monkeypatch, capsys):
    """Defaulting to an empty series instead would give a streak of 0 and no rows."""
    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)

    def explode(*_a, **_k):  # pragma: no cover - only runs on failure
        raise AssertionError("offline mode built a live client")

    monkeypatch.setattr("executor.alpaca_client.AlpacaClient.from_env", explode)
    rc = main(["--rules", str(rules_path), "--db", str(tmp_path / "j.db"), "eod", "--offline"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--offline-fixture" in err


def test_offline_reads_no_credential(tmp_path, rules_path, calibrated, monkeypatch, capsys):
    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)

    def explode(*_a, **_k):  # pragma: no cover - only runs on failure
        raise AssertionError("offline mode built a live client")

    monkeypatch.setattr("executor.alpaca_client.AlpacaClient.from_env", explode)
    monkeypatch.setattr("executor.alpaca_trading.AlpacaTradingClient.from_env", explode)
    rc = _run_eod(tmp_path, rules_path, _plan(calibrated, cells=("B",)), journal=[_plan_row("B")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "OFFLINE" in out
    assert _rows(tmp_path)


def test_offline_and_live_together_is_refused(tmp_path, rules_path, capsys):
    rc = main(
        [
            "--live",
            "--rules",
            str(rules_path),
            "--db",
            str(tmp_path / "j.db"),
            "eod",
            "--offline",
            "--offline-fixture",
            str(tmp_path / "nope.json"),
        ]
    )
    assert rc == 2
    assert "--offline cannot be combined with --live" in capsys.readouterr().err


def test_a_missing_fixture_route_fails_like_the_real_api(tmp_path, rules_path, calibrated, capsys):
    """An unrouted request is a 404, not a silent empty answer.

    This is the point of running the real CLI offline: a fixture that has drifted from
    the API's shape fails here, where a step-by-step replay would have quietly used
    whatever the replay happened to pass.
    """
    bars_only = {k: v for k, v in _routes().items() if "snapshots" not in k}
    rc = _run_eod(
        tmp_path,
        rules_path,
        _plan(calibrated, cells=("B",)),
        journal=[_plan_row("B")],
        routes=bars_only,
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "404" in captured.err
    assert _rows(tmp_path) == []


def test_offline_accepts_a_bare_route_mapping_too(tmp_path, rules_path, calibrated, capsys):
    """``{"routes": {...}}`` is a convenience, not a requirement."""
    conn = init_db(tmp_path / "journal.db")
    append_decision(conn, DecisionEntry(**_plan_row("B")))
    conn.close()
    plan_path = write_plan_file(_plan(calibrated, cells=("B",)), tmp_path / "plan.json")
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(_routes()), encoding="utf-8")  # no "routes" wrapper
    rc = main(
        [
            "--rules",
            str(rules_path),
            "--db",
            str(tmp_path / "journal.db"),
            "--now",
            "2025-06-02T17:05:00-04:00",
            "eod",
            "--plan",
            str(plan_path),
            "--date",
            DAY.isoformat(),
            "--offline",
            "--offline-fixture",
            str(fixture),
        ]
    )
    assert rc == 0, capsys.readouterr().out
    assert _rows(tmp_path)


def test_a_fixture_that_is_not_a_mapping_is_refused(tmp_path, rules_path, capsys):
    fixture = tmp_path / "fixture.json"
    fixture.write_text("[1, 2, 3]", encoding="utf-8")
    rc = main(
        [
            "--rules",
            str(rules_path),
            "--db",
            str(tmp_path / "j.db"),
            "eod",
            "--offline",
            "--offline-fixture",
            str(fixture),
        ]
    )
    assert rc == 2
    assert "request path -> payload" in capsys.readouterr().err


def test_a_missing_fixture_file_is_reported_as_a_missing_file(tmp_path, rules_path, capsys):
    rc = main(
        [
            "--rules",
            str(rules_path),
            "--db",
            str(tmp_path / "j.db"),
            "eod",
            "--offline",
            "--offline-fixture",
            str(tmp_path / "absent.json"),
        ]
    )
    assert rc != 0
    assert "absent.json" in capsys.readouterr().err