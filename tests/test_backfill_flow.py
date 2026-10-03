"""Offline tests for the T6 flow backfill (executor/backfill_flow.py).

No test in this file opens a socket. Every data source is either the repo's canned
``tests/fixtures/backfill_day.json`` or a hand-built stub, and the dry-run tests wire
``AlpacaClient.from_env`` to a transport that raises on any call — so a network call
would fail the test rather than quietly succeed on a laptop with real credentials.

What is pinned here, in order of how much damage a regression would do:

1. **The reconstruction equals the live soak.** The same underlying volumes, fed through
   the historical-bars path and through the chain-snapshot path, must produce the *same*
   ``flow_baseline`` row. If they diverge, "backfilled" and "soaked" are two different
   numbers under one name and every T6 ratio is quietly meaningless.
2. **No fabricated zeros.** A session with an empty 0DTE ladder produces NO row.
3. **Provenance.** Rows say where they came from: ``origin='backfill'`` vs ``'live'``,
   including on a database created before the column existed.
4. **Idempotence and resumability.** Re-running a window skips recorded sessions; one
   failed session does not abort the other 62.
5. **The ladder is honest about its own coverage**, and off-session bars never leak
   into a session's volume.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

import executor.backfill_flow as backfill
import executor.soak as soak
from config.loader import DEFAULT_RULES_PATH, load_rules
from executor.alpaca_client import (
    Bar,
    BarSeries,
    OptionBarSeries,
    OptionChain,
    _chain_from_payload,
    _option_bars_from_payload,
    parse_occ_symbol,
)
from journal.store import (
    FlowBaselineRow,
    apply_migrations,
    get_flow_baseline,
    init_db,
    list_flow_baseline,
)

SESSION = dt.date(2025, 12, 18)  # a Thursday; the fixture's own session
SPOT = 678.44
FIXTURE = Path("tests/fixtures/backfill_day.json")


@pytest.fixture
def rules():
    return load_rules(DEFAULT_RULES_PATH)


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def fixture_source(fixture_payload):
    source, session = backfill.load_fixture_source()
    assert session == SESSION
    return source


def equity_bars() -> BarSeries:
    """The fixture's SPY series (12-17 and 12-18), same as the chain fixture's."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["stock_bars"]
    bars = sorted((Bar.from_json(b) for b in payload["bars"]), key=lambda b: b.t)
    return BarSeries(symbol="SPY", timeframe="1Day", feed=soak.BAR_FEED, bars=bars)


def chain_for_session(payload: dict) -> OptionChain:
    """A chain snapshot carrying the SAME contract volumes as the historical bars.

    This is the "equivalent inputs" the equality test needs: one set of numbers, two
    code paths (chain snapshot vs historical per-contract bars). Only bars stamped on
    the session are carried across — a live snapshot's ``dailyBar`` is *by definition*
    that session's bar, so the off-session fixture contract (which exists to exercise the
    backfill's date guard) has no counterpart in the chain view and is left out.
    """
    snapshots = {
        occ: {"dailyBar": bar}
        for occ, bars in payload["option_bars"]["bars"].items()
        for bar in bars
        if bar["t"].startswith("2025-12-18")
    }
    return _chain_from_payload("SPY", soak.CHAIN_FEED, {"snapshots": snapshots})


# ---------------------------------------------------------------------------
# 1. the reconstruction equals what the live soak writes
# ---------------------------------------------------------------------------


def test_fixture_day_reconstructs_the_row_the_live_soak_would_write(rules, fixture_payload):
    """Same volumes, two paths, one row. The whole justification for reusing the math."""
    conn_backfill = init_db(":memory:")
    run = backfill.backfill_flow(
        source=backfill._FixtureSource(
            equity_bars(), _option_bars_from_payload(fixture_payload["option_bars"])
        ),
        conn=conn_backfill,
        rules=rules,
        start=SESSION,
        end=SESSION,
        dry_run=True,
    )
    assert [d.status for d in run.days] == ["dry_run"]
    from_history = run.days[0].row
    assert from_history is not None

    class _ChainSource:
        def daily_bars(self, symbol: str) -> BarSeries:
            return equity_bars()

        def option_chain(self, symbol: str) -> OptionChain:
            return chain_for_session(fixture_payload)

    conn_live = init_db(":memory:")
    live = soak.run_soak(
        source=_ChainSource(),  # type: ignore[arg-type]
        conn=conn_live,
        iv_store=None,
        rules=rules,
        session=SESSION,
        dry_run=True,
    )
    assert live.status == "dry_run"
    from_chain = live.flow_row
    assert from_chain is not None

    # `origin` is the ONE intended difference, plus the write timestamp (never
    # comparable) and the store-assigned ids (never set on a dry run anyway).
    assert from_history.origin == "backfill"
    assert from_chain.origin == "live"
    comparable = [
        name
        for name in FlowBaselineRow.model_fields
        if name not in {"origin", "ts", "id", "created_at"}
    ]
    for name in comparable:
        assert getattr(from_history, name) == getattr(from_chain, name), (
            f"{name} differs between the backfill and the live soak for the same volumes"
        )
    # And the numbers themselves, spelled out so a failure says which quantity moved.
    assert from_history.deep_otm_call_volume == 1540.0  # 700 + 705 strikes
    assert from_history.deep_otm_put_volume == 2120.0  # 650 + 640 strikes
    assert from_history.session_spot == SPOT
    assert from_history.zero_dte_expiry == "20251218"


def test_backfill_reuses_the_soak_aggregation_functions(monkeypatch):
    """No forked math: the backfill calls the soak's functions, not a copy."""
    calls: list[str] = []
    for name in ("aggregate_flow", "build_flow_row"):
        original = getattr(backfill, name)
        assert original is getattr(soak, name), f"{name} is a copy, not the soak's function"

        def _spy(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(backfill, name, _spy)
    run = backfill.backfill_flow(
        source=backfill.load_fixture_source()[0],
        conn=init_db(":memory:"),
        rules=load_rules(DEFAULT_RULES_PATH),
        start=SESSION,
        end=SESSION,
        dry_run=True,
    )
    assert calls == ["aggregate_flow", "build_flow_row"]
    assert run.days[0].status == "dry_run"


# ---------------------------------------------------------------------------
# 2. no fabricated zeros
# ---------------------------------------------------------------------------


def test_empty_ladder_writes_no_row(rules):
    """A 0DTE ladder with no bars is an unknown, not a 0.0-volume session."""
    empty = OptionBarSeries(bars_by_symbol={})
    source = backfill._FixtureSource(equity_bars(), empty)
    conn = init_db(":memory:")
    run = backfill.backfill_flow(
        source=source, conn=conn, rules=rules, start=SESSION, end=SESSION
    )
    assert [d.status for d in run.days] == ["no_data"]
    assert list_flow_baseline(conn) == []
    assert "no row written" in run.days[0].summary


def test_zero_volume_bars_write_no_row(rules, fixture_payload):
    """Bars that all carry v=0 are the same unknown: nothing traded, or nothing there."""
    payload = dict(fixture_payload["option_bars"])
    bars = {
        occ: [{**bar, "v": 0} for bar in series] for occ, series in payload["bars"].items()
    }
    source = backfill._FixtureSource(equity_bars(), _option_bars_from_payload({"bars": bars}))
    conn = init_db(":memory:")
    run = backfill.backfill_flow(
        source=source, conn=conn, rules=rules, start=SESSION, end=SESSION
    )
    assert [d.status for d in run.days] == ["no_data"]
    assert list_flow_baseline(conn) == []


def test_session_without_an_equity_bar_is_a_no_session(rules, fixture_payload):
    """Holiday: the bar is the evidence, exactly as in the live soak."""
    run = backfill.backfill_flow(
        source=backfill._FixtureSource(
            equity_bars(), _option_bars_from_payload(fixture_payload["option_bars"])
        ),
        conn=init_db(":memory:"),
        rules=rules,
        start=dt.date(2025, 12, 25),  # a holiday inside the fixture's bar coverage gap
        end=dt.date(2025, 12, 26),
    )
    assert {d.status for d in run.days} == {"no_session"}
    assert not run.failures


# ---------------------------------------------------------------------------
# 3. provenance: origin
# ---------------------------------------------------------------------------


def test_backfilled_row_is_marked_and_live_row_defaults_to_live(rules, tmp_path):
    db = tmp_path / "journal.db"
    conn = init_db(db)
    run = backfill.backfill_flow(
        source=backfill.load_fixture_source()[0],
        conn=conn,
        rules=rules,
        start=SESSION,
        end=SESSION,
    )
    assert [d.status for d in run.days] == ["recorded"]
    row = get_flow_baseline(conn, SESSION.isoformat())
    assert row is not None and row.origin == "backfill"

    # A row written by the live writer with no origin in sight is 'live'.
    live = FlowBaselineRow(
        date="2025-12-19",
        underlying="SPY",
        feed=soak.CHAIN_FEED,
        is_delayed=True,
        session_spot=SPOT,
        deep_otm_threshold_pct=3.0,
        deep_otm_call_volume=1.0,
        deep_otm_put_volume=2.0,
        call_volume_by_distance={"3": 1.0},
        put_volume_by_distance={"3": 2.0},
        strategy_version=rules.strategy_version,
        ts=soak.utcnow(),
    )
    from journal.store import append_flow_baseline

    append_flow_baseline(conn, live)
    assert get_flow_baseline(conn, "2025-12-19").origin == "live"
    assert [r.origin for r in list_flow_baseline(conn)] == ["backfill", "live"]


def test_origin_is_constrained_at_the_schema():
    with pytest.raises(ValidationError):
        FlowBaselineRow(
            date="2025-12-19",
            underlying="SPY",
            feed=soak.CHAIN_FEED,
            is_delayed=True,
            origin="guess",  # type: ignore[arg-type]
            session_spot=SPOT,
            deep_otm_threshold_pct=3.0,
            deep_otm_call_volume=1.0,
            deep_otm_put_volume=2.0,
            call_volume_by_distance={},
            put_volume_by_distance={},
            strategy_version="v1",
            ts=soak.utcnow(),
        )


def test_migration_adds_origin_to_a_database_created_before_it_existed():
    """An existing install must not need a rebuild: the column is added, not assumed."""
    schema = Path("journal/schema.sql").read_text(encoding="utf-8")
    legacy_sql = schema.replace(
        "    origin                  TEXT    NOT NULL DEFAULT 'live'\n"
        "                                       CHECK (origin IN ('live', 'backfill')),\n",
        "",
    )
    assert "origin" not in legacy_sql.split("CREATE TABLE IF NOT EXISTS flow_baseline")[1][:400]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(legacy_sql)
    conn.execute(
        """INSERT INTO flow_baseline (ts, date, underlying, feed, is_delayed, session_spot,
           zero_dte_expiry, chain_contracts, deep_otm_threshold_pct, deep_otm_call_volume,
           deep_otm_put_volume, deep_otm_total_volume, call_volume_by_distance,
           put_volume_by_distance, baseline_lookback_days, baseline_days, strategy_version,
           created_at) VALUES ('t','2025-12-01','SPY','indicative',1,670.0,NULL,0,3.0,1.0,2.0,3.0,
           '{}','{}',20,0,'v1','t')"""
    )
    assert apply_migrations(conn) == ["flow_baseline.origin"]
    assert apply_migrations(conn) == []  # idempotent
    # The legacy row reads as 'live' (it was, by definition) and nothing was rewritten.
    assert conn.execute("SELECT origin FROM flow_baseline").fetchone()["origin"] == "live"
    # The append-only guarantee is untouched by the migration.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE flow_baseline SET deep_otm_call_volume = 99")


# ---------------------------------------------------------------------------
# 4. idempotence, resumability, failure isolation
# ---------------------------------------------------------------------------


def test_rerunning_the_same_window_skips_and_does_not_duplicate(rules, tmp_path):
    db = tmp_path / "journal.db"
    first = backfill.backfill_flow(
        source=backfill.load_fixture_source()[0],
        conn=init_db(db),
        rules=rules,
        start=SESSION,
        end=SESSION,
    )
    assert [d.status for d in first.days] == ["recorded"]

    second_conn = init_db(db)
    second = backfill.backfill_flow(
        source=backfill.load_fixture_source()[0],
        conn=second_conn,
        rules=rules,
        start=SESSION,
        end=SESSION,
    )
    assert [d.status for d in second.days] == ["already_recorded"]
    assert "nothing duplicated" in second.days[0].summary
    assert len(list_flow_baseline(second_conn)) == 1
    assert not second.failures


def test_one_failing_session_does_not_abort_the_others(rules, fixture_payload):
    """The 4-week window is worth nothing if day 40's timeout throws away days 1-39."""

    class _FlakySource:
        def __init__(self) -> None:
            self.calls = 0
            self._inner = backfill._FixtureSource(
                equity_bars(), _option_bars_from_payload(fixture_payload["option_bars"])
            )

        def equity_daily_bars(self, symbol, *, start, end):
            return self._inner.equity_daily_bars(symbol, start=start, end=end)

        def option_daily_bars(self, symbols, *, start, end):
            self.calls += 1
            if start == "2025-12-17":  # mid-window fetch blows up
                raise RuntimeError("alpaca HTTP 422 (simulated)")
            return self._inner.option_daily_bars(symbols, start=start, end=end)

    conn = init_db(":memory:")
    run = backfill.backfill_flow(
        source=_FlakySource(),
        conn=conn,
        rules=rules,
        start=dt.date(2025, 12, 16),
        end=dt.date(2025, 12, 18),
    )
    statuses = {d.session: d.status for d in run.days}
    assert statuses == {
        "2025-12-16": "no_session",
        "2025-12-17": "failed",
        "2025-12-18": "recorded",
    }
    failure = next(d for d in run.days if d.status == "failed")
    assert "HTTP 422" in failure.summary
    assert run.failures == [failure]
    # The good session still landed — resumability is the point.
    assert get_flow_baseline(conn, SESSION.isoformat()) is not None
    assert "1 recorded" in run.summary and "1 failed" in run.summary


def test_a_transient_failure_is_recovered_by_rerunning_the_window(rules, tmp_path):
    """Day 17 fails (network), day 18 lands. The rerun records 17 and skips 18."""
    db = tmp_path / "journal.db"
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["option_bars"]

    class _FlakyOnce(backfill._FixtureSource):
        """Fails the first time it is asked for 12-17, then behaves."""

        def __init__(self, *args) -> None:
            super().__init__(*args)
            self.exhausted = False

        def option_daily_bars(self, symbols, *, start, end):
            if start == "2025-12-17" and not self.exhausted:
                self.exhausted = True
                raise RuntimeError("alpaca HTTP 500 (simulated transient)")
            return super().option_daily_bars(symbols, start=start, end=end)

    flaky = _FlakyOnce(equity_bars(), _option_bars_from_payload(payload))
    conn = init_db(db)
    first = backfill.backfill_flow(
        source=flaky, conn=conn, rules=rules, start=dt.date(2025, 12, 17), end=SESSION
    )
    assert {d.session: d.status for d in first.days} == {
        "2025-12-17": "failed",
        "2025-12-18": "recorded",
    }
    assert [r.date for r in list_flow_baseline(conn)] == ["2025-12-18"]

    resumed_conn = init_db(db)
    resumed = backfill.backfill_flow(
        source=flaky, conn=resumed_conn, rules=rules, start=dt.date(2025, 12, 17), end=SESSION
    )
    # 17 is recorded this time; 18 is skipped rather than duplicated.
    assert {d.session: d.status for d in resumed.days} == {
        "2025-12-17": "no_data",  # the fixture's bars are stamped 12-18 only
        "2025-12-18": "already_recorded",
    }
    assert len(list_flow_baseline(resumed_conn)) == 1


def test_baseline_is_built_from_prior_sessions_only(rules):
    """Two reconstructed sessions: the second's baseline sees the first, not itself."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["option_bars"]
    first_bars = {
        occ: bars
        for occ, bars in payload["bars"].items()
        if "251218" in occ and bars[0]["t"].startswith("2025-12-18")
    }
    shifted = {
        occ.replace("251218", "251219"): [
            {**bar, "t": "2025-12-19T05:00:00Z"} for bar in bars
        ]
        for occ, bars in first_bars.items()
    }
    equity = json.loads(FIXTURE.read_text(encoding="utf-8"))["stock_bars"]
    two_days = {
        "symbol": "SPY",
        "next_page_token": None,
        "bars": equity["bars"]
        + [
            {
                "t": "2025-12-19T05:00:00Z",
                "o": "678.00",
                "h": "681.00",
                "l": "676.00",
                "c": "678.44",  # same close as the 18th, so the buckets land identically
                "v": "50000000",
                "n": 400000,
                "vw": "679.0",
            }
        ],
    }
    bars = sorted((Bar.from_json(b) for b in two_days["bars"]), key=lambda b: b.t)

    class _TwoDaySource:
        def __init__(self) -> None:
            self.calls = 0

        def equity_daily_bars(self, symbol, *, start, end):
            return BarSeries(symbol="SPY", timeframe="1Day", feed=soak.BAR_FEED, bars=bars)

        def option_daily_bars(self, symbols, *, start, end):
            self.calls += 1
            key = "2025-12-18" if start == "2025-12-18" else "2025-12-19"
            source = {"bars": first_bars if key == "2025-12-18" else shifted}
            return _option_bars_from_payload(source)

    conn = init_db(":memory:")
    run = backfill.backfill_flow(
        source=_TwoDaySource(),
        conn=conn,
        rules=rules,
        start=SESSION,
        end=dt.date(2025, 12, 19),
    )
    assert [d.status for d in run.days] == ["recorded", "recorded"]
    day_two = get_flow_baseline(conn, "2025-12-19")
    assert day_two is not None
    assert day_two.baseline_days == 1
    assert day_two.baseline_call_mean == pytest.approx(1540.0)
    assert day_two.ratio_call == pytest.approx(1540.0 / 1540.0)
    assert day_two.origin == "backfill"


# ---------------------------------------------------------------------------
# 5. the ladder is honest about its own coverage
# ---------------------------------------------------------------------------


def test_coverage_counts_are_reported_per_session(rules):
    run = backfill.backfill_flow(
        source=backfill.load_fixture_source()[0],
        conn=init_db(":memory:"),
        rules=rules,
        start=SESSION,
        end=SESSION,
    )
    details = run.days[0].details
    assert details["symbols_requested"] > 200  # the default ±10% grid at $1 steps
    assert details["symbols_with_bar"] == 9
    assert details["symbols_without_bar"] == details["symbols_requested"] - 9
    assert details["off_session_bars"] == 1
    # The report artifact carries the same numbers a reader needs to judge the row.
    report = run.report()
    assert report["sessions"][0]["details"]["symbols_with_bar"] == 9
    assert report["counts"] == {"recorded": 1}
    assert report["api_calls"] >= 1


def test_a_bar_stamped_on_another_day_is_discarded_not_summed(fixture_payload):
    """The 00720000 call has a 2025-12-17 bar; it must not reach 12-18's volume."""
    series = _option_bars_from_payload(fixture_payload["option_bars"])
    contracts, off_session = backfill.contracts_from_bars(
        ["SPY251218C00720000", "SPY251218C00700000"], series, SESSION
    )
    assert [c.symbol for c in contracts] == ["SPY251218C00700000"]
    assert off_session == 1
    assert sum(float(c.daily_bar.v) for c in contracts) == 1200.0


def test_zero_dte_symbols_are_centred_parsed_and_both_rights():
    symbols = backfill.zero_dte_symbols("SPY", "20251218", SPOT, strike_step=5.0, range_pct=2.0)
    # 664.87..692.01 at $5 steps -> 665, 670, 675, 680, 685, 690; both rights.
    assert len(symbols) == 2 * 6
    assert "SPY251218C00680000" in symbols and "SPY251218P00680000" in symbols
    rights = set()
    for symbol in symbols:
        root, expiry, strike, right = parse_occ_symbol(symbol)
        assert (root, expiry) == ("SPY", "20251218")
        assert 660.0 <= strike <= 695.0
        rights.add(right)
    assert rights == {"call", "put"}
    with pytest.raises(backfill.BackfillError):
        backfill.zero_dte_symbols("SPY", "20251218", 0.0)
    with pytest.raises(backfill.BackfillError):
        backfill.zero_dte_symbols("SPY", "20251218", SPOT, strike_step=0.0)
    with pytest.raises(backfill.BackfillError):
        backfill.zero_dte_symbols("SPY", "not-a-date", SPOT)


def test_session_days_skips_weekends_and_rejects_a_backwards_window():
    days = backfill.session_days(dt.date(2025, 12, 18), dt.date(2025, 12, 23))
    assert days == [
        dt.date(2025, 12, 18),
        dt.date(2025, 12, 19),
        dt.date(2025, 12, 22),
        dt.date(2025, 12, 23),
    ]
    with pytest.raises(backfill.BackfillError):
        backfill.session_days(dt.date(2025, 12, 23), dt.date(2025, 12, 18))


def test_stored_histogram_re_derives_the_aggregate_at_another_threshold(rules):
    """The reason the histogram is stored threshold-free: N can move without re-fetching."""
    conn = init_db(":memory:")
    backfill.backfill_flow(
        source=backfill.load_fixture_source()[0],
        conn=conn,
        rules=rules,
        start=SESSION,
        end=SESSION,
    )
    row = get_flow_baseline(conn, SESSION.isoformat())
    assert row is not None
    at4 = soak.threshold_from_buckets(row.call_volume_by_distance, 4.0)
    at4p = soak.threshold_from_buckets(row.put_volume_by_distance, 4.0)
    assert at4 == 0.0  # the deepest call in the fixture is the 705 strike, 3.91% out
    assert at4p == 2120.0  # 650 (4.19%) and 640 (5.66%)
    assert at4p == soak.threshold_from_buckets(row.put_volume_by_distance, 5.0) + 1900.0
    # Bucket keys are integer lower bounds: 685 is 0.96% out, 695 is 2.44%, 700 is 3.18%.
    assert set(row.call_volume_by_distance) == {"0", "2", "3"}
    assert set(row.put_volume_by_distance) == {"0", "2", "4", "5"}


# ---------------------------------------------------------------------------
# 6. CLI: dry-run touches no network, and a failed session is a non-zero exit
# ---------------------------------------------------------------------------


class _ExplodingTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_json(self, url, *, headers, params=None):
        self.calls.append(url)
        raise AssertionError(f"dry-run attempted a network call: {url}")

    def request(self, method: str, path: str, **kwargs):  # pragma: no cover - parity helper
        self.calls.append(f"{method} {path}")
        raise AssertionError(f"dry-run attempted a network call: {method} {path}")


def test_dry_run_makes_no_transport_calls_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from executor.alpaca_client import AlpacaClient

    transport = _ExplodingTransport()
    client = AlpacaClient(transport=transport, key="k", secret="s")
    monkeypatch.setattr(backfill.AlpacaClient, "from_env", classmethod(lambda cls, **kw: client))

    db = tmp_path / "journal.db"
    code = backfill.main(["--dry-run", "--db", str(db)])
    out = capsys.readouterr().out
    assert code == 0
    assert transport.calls == []
    assert not db.exists()
    assert "nothing written (dry-run)" in out


def test_dry_run_honours_an_explicit_window_and_says_so(tmp_path, capsys):
    db = tmp_path / "journal.db"
    code = backfill.main(
        ["--dry-run", "--from", "2025-12-17", "--to", "2025-12-18", "--db", str(db)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "1 dry_run" in out and "1 no_data" in out
    assert not db.exists()


def test_report_file_is_written(tmp_path):
    report = tmp_path / "nested" / "backfill-report.json"
    code = backfill.main(["--dry-run", "--report", str(report)])
    assert code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["window"] == {"from": "2025-12-18", "end": "2025-12-18"}
    assert payload["counts"] == {"dry_run": 1}


def test_main_wires_the_rate_limiter_and_returns_nonzero_on_failure(
    tmp_path, monkeypatch, capsys
):
    from executor.alpaca_client import RateLimiter

    captured: dict = {}

    def _from_env(cls, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("no credentials on the build box — expected in tests")

    monkeypatch.setattr(backfill.AlpacaClient, "from_env", classmethod(_from_env))
    code = backfill.main(
        ["--from", "2025-12-18", "--to", "2025-12-18", "--db", str(tmp_path / "j.db")]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert isinstance(captured.get("rate_limiter"), RateLimiter)
    assert captured["rate_limiter"].max_calls == backfill.DEFAULT_MAX_CALLS_PER_MINUTE
    assert "error: RuntimeError" in err


def test_json_output_is_machine_readable(tmp_path, capsys):
    assert backfill.main(["--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"] == {"dry_run": 1}
    assert payload["sessions"][0]["session"] == SESSION.isoformat()
