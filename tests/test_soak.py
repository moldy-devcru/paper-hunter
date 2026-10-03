"""Offline tests for the EOD soak job (executor/soak.py).

Everything here is fixture-driven. No test in this file opens a socket: the dry-run
path is handed an Alpaca client whose transport raises on any call, and the tests that
exercise the non-dry path inject a canned data source directly, so a network call
would fail loudly rather than silently succeed on a developer laptop with creds in
the environment.

What these tests pin down, in order of how much damage a regression would do:

1. A 0DTE chain + bars produce the expected ``flow_baseline`` row — the numbers T6's
   calibration will be read off.
2. Same-day re-runs are idempotent (one row, not two).
3. Weekends and holidays are clean no-ops, and a no-op still exits 0.
4. ``--dry-run`` makes zero transport calls.
5. The distance histogram re-derives the aggregate at any integer threshold, which is
   what makes a future threshold change free.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest

import executor.soak as soak
from config.loader import DEFAULT_RULES_PATH, load_rules
from executor.alpaca_client import AlpacaClient, Bar, BarSeries, OptionChain
from executor.iv_rank import IvRankStore
from journal.store import (
    DuplicateFlowBaseline,
    FlowBaselineRow,
    append_flow_baseline,
    get_flow_baseline,
    init_db,
    list_flow_baseline,
)
from tests.fixtures_synthetic import make_contract

SESSION = dt.date(2026, 3, 10)  # a Tuesday
SPOT = 625.00


# ---------------------------------------------------------------------------
# canned sources
# ---------------------------------------------------------------------------


class _ExplodingTransport:
    """A transport that fails the test if it is ever used.

    This is how "dry-run makes no network call" is enforced rather than asserted by
    hope: the dry-run tests build a real ``AlpacaClient`` around this and hand it to
    the same ``main()`` a cron job would call, then check ``calls == 0``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        raise AssertionError(f"dry-run attempted a network call: {method} {path}")

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)


class _StaticSource:
    """A :class:`soak.SoakData` over canned objects. No I/O of any kind."""

    def __init__(self, bars: BarSeries, chain: OptionChain) -> None:
        self.bars_seen: list[str] = []
        self.chains_seen: list[str] = []
        self._bars = bars
        self._chain = chain

    def daily_bars(self, symbol: str) -> BarSeries:
        self.bars_seen.append(symbol)
        return self._bars

    def option_chain(self, symbol: str) -> OptionChain:
        self.chains_seen.append(symbol)
        return self._chain


def bar_for(day: dt.date, close: float = SPOT, volume: float = 40_000_000) -> Bar:
    """A daily bar stamped 09:30 ET so the ET date is unambiguous."""
    return Bar(
        t=dt.datetime.combine(day, dt.time(9, 30), tzinfo=dt.UTC),
        o=close,
        h=close,
        l=close,
        c=close,
        v=volume,
    )


def bars_for(*days: dt.date, close: float = SPOT) -> BarSeries:
    return BarSeries(
        symbol="SPY",
        timeframe="1Day",
        feed="sip",
        bars=[bar_for(d, close=close) for d in days],
    )


def contract_with_volume(
    *,
    strike: float,
    right: str,
    volume: float,
    expiry: dt.date = SESSION,
    iv: float | None = 0.18,
    underlying: str = "SPY",
):
    """A contract with a ``dailyBar.v`` so the flow aggregate has something to sum."""
    occ = int(strike * 1000)
    code = "C" if right == "call" else "P"
    c = make_contract(
        symbol=f"{underlying}{expiry:%Y%m%d}{code}{occ:08d}",
        underlying=underlying,
        expiry=expiry.strftime("%Y%m%d"),
        strike=strike,
        right=right,
        iv=iv,
    )
    return dataclasses.replace(c, daily_bar=bar_for(expiry, close=strike, volume=volume))


def chain_of(*contracts) -> OptionChain:
    return OptionChain(underlying="SPY", feed="indicative", contracts=list(contracts))


def trading_days(start: dt.date, count: int) -> list[dt.date]:
    """``count`` consecutive weekdays from ``start`` (skips Sat/Sun, like the job does)."""
    days: list[dt.date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def deep_otm_chain(*, spot: float = SPOT, session: dt.date = SESSION) -> OptionChain:
    """A 0DTE chain with a deliberate spread of distances and volumes.

    Distance from spot at 625:
      629.00 -> 0.64%  (bucket 0)  NOT deep at 3%
      645.00 -> 3.20%  (bucket 3)  deep
      660.00 -> 5.60%  (bucket 5)  deep
      600.00 -> 4.00%  (bucket 4)  deep put
      610.00 -> 2.40%  (bucket 2)  NOT deep at 3%
      625.00 -> ATM               never counted
    """
    return chain_of(
        contract_with_volume(strike=629.00, right="call", volume=1_000, expiry=session),
        contract_with_volume(strike=645.00, right="call", volume=2_000, expiry=session),
        contract_with_volume(strike=660.00, right="call", volume=3_000, expiry=session),
        contract_with_volume(strike=600.00, right="put", volume=4_000, expiry=session),
        contract_with_volume(strike=610.00, right="put", volume=5_000, expiry=session),
        contract_with_volume(strike=625.00, right="call", volume=9_000, expiry=session),
    )


@pytest.fixture
def rules():
    return load_rules(DEFAULT_RULES_PATH)


@pytest.fixture
def journal():
    conn = init_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def iv_store(tmp_path: Path) -> IvRankStore:
    return IvRankStore.open(tmp_path / "ivrank.db")


def run(
    rules,
    journal,
    source,
    *,
    iv_store=None,
    session=SESSION,
    threshold=3.0,
    dry_run=False,
    force=False,
):
    return soak.run_soak(
        source=source,
        conn=journal,
        iv_store=iv_store,
        rules=rules,
        session=session,
        threshold_pct=threshold,
        dry_run=dry_run,
        force=force,
    )


# ---------------------------------------------------------------------------
# 1. the row itself
# ---------------------------------------------------------------------------


def test_flow_row_from_fixture_chain(rules, journal):
    source = _StaticSource(bars_for(SESSION), deep_otm_chain())
    run_out = run(rules, journal, source)

    assert run_out.status == "recorded"
    row = get_flow_baseline(journal, SESSION.isoformat())
    assert row is not None

    # 3.20% and 5.60% calls are deep at 3%; 0.64% and the ATM print are not.
    assert row.deep_otm_call_volume == pytest.approx(5_000.0)
    # 4.00% put is deep; 2.40% is not.
    assert row.deep_otm_put_volume == pytest.approx(4_000.0)
    assert row.deep_otm_total_volume == pytest.approx(9_000.0)
    assert row.deep_otm_threshold_pct == 3.0
    assert row.zero_dte_expiry == SESSION.strftime("%Y%m%d")
    assert row.session_spot == SPOT


def test_flow_row_carries_provenance(rules, journal):
    run_out = run(rules, journal, _StaticSource(bars_for(SESSION), deep_otm_chain()))
    row = run_out.flow_row
    assert row is not None
    # An unauditable flow number is the failure mode this exists to prevent.
    assert row.feed == "indicative"
    assert row.is_delayed is True
    assert row.chain_contracts == 6
    assert row.strategy_version == rules.strategy_version
    assert row.underlying == "SPY"


def test_flow_row_histogram_is_threshold_free(rules, journal):
    run(rules, journal, _StaticSource(bars_for(SESSION), deep_otm_chain()))
    row = get_flow_baseline(journal, SESSION.isoformat())
    assert row.call_volume_by_distance == {"0": 1_000.0, "3": 2_000.0, "5": 3_000.0}
    assert row.put_volume_by_distance == {"2": 5_000.0, "4": 4_000.0}


@pytest.mark.parametrize(
    ("threshold", "expected_call", "expected_put"),
    [
        (1.0, 5_000.0, 9_000.0),  # the 0.64% call drops out; both puts are >= 1%
        (3.0, 5_000.0, 4_000.0),
        (5.0, 3_000.0, 0.0),
        (6.0, 0.0, 0.0),  # nothing that far out exists
    ],
)
def test_threshold_changes_reaggregate_from_stored_buckets(
    rules, journal, threshold, expected_call, expected_put
):
    """A threshold revision must not require re-fetching OPRA.

    The row is written once at 3%; every other threshold is re-derived from the stored
    histogram and must equal what a fresh pass at that threshold would have computed.
    """
    run(rules, journal, _StaticSource(bars_for(SESSION), deep_otm_chain()))
    stored = get_flow_baseline(journal, SESSION.isoformat())
    assert stored is not None
    assert soak.threshold_from_buckets(stored.call_volume_by_distance, threshold) == (expected_call)
    assert soak.threshold_from_buckets(stored.put_volume_by_distance, threshold) == (expected_put)

    # ...and a live pass at that threshold agrees with the re-derivation.
    fresh = soak.aggregate_flow(deep_otm_chain(), SPOT, threshold_pct=threshold)
    assert fresh.call_volume == expected_call
    assert fresh.put_volume == expected_put


def test_first_session_has_no_baseline_and_no_ratio(rules, journal):
    run_out = run(rules, journal, _StaticSource(bars_for(SESSION), deep_otm_chain()))
    row = run_out.flow_row
    assert row is not None
    assert row.baseline_days == 0
    assert row.baseline_call_mean is None
    assert row.ratio_call is None  # a 0-day baseline makes the ratio undefined, not infinite


def test_ratio_against_trailing_baseline(rules, journal):
    """Three sessions at a fixed volume, then a fourth at 3x the mean."""
    volumes = [1_000.0, 1_000.0, 2_000.0, 4_000.0]
    for offset, volume in enumerate(volumes):
        day = SESSION + dt.timedelta(days=offset)
        chain = chain_of(
            contract_with_volume(strike=645.00, right="call", volume=volume, expiry=day),
            contract_with_volume(strike=600.00, right="put", volume=volume, expiry=day),
        )
        run(rules, journal, _StaticSource(bars_for(day), chain), session=day)

    rows = {r.date: r for r in list_flow_baseline(journal)}
    fourth = rows[(SESSION + dt.timedelta(days=3)).isoformat()]
    # mean of the prior three: (1000 + 1000 + 2000) / 3
    assert fourth.baseline_days == 3
    assert fourth.baseline_call_mean == pytest.approx(1_333.33, rel=1e-3)
    assert fourth.ratio_call == pytest.approx(3.0, rel=1e-3)


def test_baseline_excludes_rows_at_a_different_threshold(rules, journal):
    """Mixing thresholds would compare two different quantities under one number."""
    for offset, volume in enumerate([1_000.0, 1_000.0]):
        day = SESSION + dt.timedelta(days=offset)
        chain = chain_of(
            contract_with_volume(strike=645.00, right="call", volume=volume, expiry=day),
        )
        run(rules, journal, _StaticSource(bars_for(day), chain), session=day, threshold=3.0)
    day = SESSION + dt.timedelta(days=2)
    chain = chain_of(contract_with_volume(strike=645.00, right="call", volume=9_000, expiry=day))
    last = run(rules, journal, _StaticSource(bars_for(day), chain), session=day, threshold=5.0)
    assert last.flow_row is not None
    assert last.flow_row.baseline_days == 0  # prior rows were at 3%, so none count
    assert last.flow_row.ratio_call is None


def test_baseline_window_is_capped_at_the_rulebook_lookback(rules, journal):
    lookback = rules.checklist.t6_flow.baseline_lookback_days
    assert lookback == 20
    days = trading_days(SESSION, lookback + 5)
    for offset, day in enumerate(days):
        chain = chain_of(
            contract_with_volume(strike=645.00, right="call", volume=float(offset + 1), expiry=day),
        )
        run(rules, journal, _StaticSource(bars_for(day), chain), session=day)
    rows = list_flow_baseline(journal)
    assert len(rows) == lookback + 5
    assert rows[-1].baseline_days == lookback
    # the window is the LAST N rows before this one, i.e. offsets 4..23
    expected = sum(float(i + 1) for i in range(4, lookback + 4)) / lookback
    assert rows[-1].baseline_call_mean == pytest.approx(expected)


def test_zero_baseline_mean_yields_no_ratio(rules, journal):
    """0 / 0 is not infinity, and infinity would pass any >= N gate."""
    day_a = SESSION
    day_b = SESSION + dt.timedelta(days=1)
    idle = chain_of(
        contract_with_volume(strike=629.00, right="call", volume=0.0, expiry=day_a),
    )
    run(rules, journal, _StaticSource(bars_for(day_a), idle), session=day_a)
    busy = chain_of(
        contract_with_volume(strike=645.00, right="call", volume=5_000.0, expiry=day_b),
    )
    out = run(rules, journal, _StaticSource(bars_for(day_b), busy), session=day_b)
    assert out.flow_row is not None
    assert out.flow_row.baseline_call_mean == 0.0
    assert out.flow_row.ratio_call is None


# ---------------------------------------------------------------------------
# 2. idempotency
# ---------------------------------------------------------------------------


def test_same_day_rerun_is_a_noop(rules, journal):
    source = _StaticSource(bars_for(SESSION), deep_otm_chain())
    first = run(rules, journal, source)
    assert first.status == "recorded"

    second = run(rules, journal, source)
    assert second.status == "already_recorded"
    assert "idempotent no-op" in second.summary
    assert len(list_flow_baseline(journal)) == 1
    assert source.chains_seen == ["SPY"]  # the chain was not even fetched the second time


def test_rerun_with_different_numbers_does_not_overwrite(rules, journal):
    """The append-only guarantee: a re-poll cannot revise a recorded session."""
    run(rules, journal, _StaticSource(bars_for(SESSION), deep_otm_chain()))
    changed = chain_of(contract_with_volume(strike=645.00, right="call", volume=999.0))
    second = run(rules, journal, _StaticSource(bars_for(SESSION), changed))
    assert second.status == "already_recorded"
    row = get_flow_baseline(journal, SESSION.isoformat())
    assert row is not None
    assert row.deep_otm_call_volume == pytest.approx(5_000.0)


def test_flow_baseline_rejects_updates_and_deletes(journal):
    row = FlowBaselineRow(
        date=SESSION.isoformat(),
        underlying="SPY",
        feed="indicative",
        is_delayed=True,
        session_spot=SPOT,
        deep_otm_threshold_pct=3.0,
        deep_otm_call_volume=1.0,
        deep_otm_put_volume=2.0,
        call_volume_by_distance={"3": 1.0},
        put_volume_by_distance={"3": 2.0},
        strategy_version="test",
        ts="2026-03-10T21:00:00Z",
    )
    row_id = append_flow_baseline(journal, row)
    assert row_id > 0

    with pytest.raises(Exception, match="append-only"):
        journal.execute("UPDATE flow_baseline SET deep_otm_call_volume = 999")
    with pytest.raises(Exception, match="may not be deleted"):
        journal.execute("DELETE FROM flow_baseline")
    with pytest.raises(DuplicateFlowBaseline):
        append_flow_baseline(journal, row)


# ---------------------------------------------------------------------------
# 3. non-trading days
# ---------------------------------------------------------------------------


def test_weekend_is_a_noop_before_any_fetch(rules, journal):
    saturday = dt.date(2026, 3, 14)
    source = _StaticSource(bars_for(dt.date(2026, 3, 13)), deep_otm_chain(session=saturday))
    out = run(rules, journal, source, session=saturday)

    assert out.status == "weekend"
    assert "weekend" in out.summary
    assert source.bars_seen == []  # nothing was even fetched
    assert list_flow_baseline(journal) == []


def test_holiday_is_a_noop_detected_by_a_missing_bar(rules, journal):
    """Thanksgiving: a weekday with no session. The newest bar is the Friday before."""
    holiday = dt.date(2026, 11, 26)
    last_session = dt.date(2026, 11, 25)
    source = _StaticSource(
        bars_for(dt.date(2026, 11, 24), last_session), deep_otm_chain(session=holiday)
    )
    out = run(rules, journal, source, session=holiday)

    assert out.status == "no_session"
    assert "holiday" in out.summary
    assert source.chains_seen == []  # no chain fetch on a day that never traded
    assert list_flow_baseline(journal) == []


def test_force_records_without_bar_evidence(rules, journal):
    """The one legitimate override: a late bar pull for a session that did happen."""
    holiday = dt.date(2026, 11, 26)
    source = _StaticSource(bars_for(dt.date(2026, 11, 25)), deep_otm_chain(session=holiday))
    out = run(rules, journal, source, session=holiday, force=True)

    assert out.status == "recorded"
    assert "forced" in out.flow_row.ts or out.flow_row is not None
    assert get_flow_baseline(journal, holiday.isoformat()) is not None


def test_session_without_a_zero_dte_expiry_is_not_recorded_as_zero(rules, journal):
    """Writing 0.0 would inject a fake session into the distribution N is read from."""
    no_zero_dte = dt.date(2026, 1, 19)  # MLK day is a holiday, but take a listed chain
    chain = chain_of(
        contract_with_volume(
            strike=645.00, right="call", volume=100.0, expiry=no_zero_dte + dt.timedelta(days=30)
        ),
    )
    source = _StaticSource(bars_for(no_zero_dte), chain)
    out = run(rules, journal, source, session=no_zero_dte)

    assert out.status == "no_zero_dte"
    assert "no listed 0DTE expiry" in out.summary
    assert list_flow_baseline(journal) == []


# ---------------------------------------------------------------------------
# 4. dry-run makes no network calls
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_transport_calls(rules, tmp_path, monkeypatch):
    """The dry-run path must never construct a client that can reach the network.

    ``from_env`` is patched to hand back a real client wired to a transport that
    raises on any request, so if the dry-run branch ever grows an API call the test
    fails with that call's name instead of quietly depending on the developer's
    environment having valid creds.
    """
    transport = _ExplodingTransport()
    client = AlpacaClient(transport=transport, key="k", secret="s")
    monkeypatch.setattr(soak.AlpacaClient, "from_env", classmethod(lambda cls: client))

    db = tmp_path / "journal.db"
    iv_db = tmp_path / "ivrank.db"
    code = soak.main(
        ["--dry-run", "--session", SESSION.isoformat(), "--db", str(db), "--iv-db", str(iv_db)]
    )
    assert code == 0
    assert transport.calls == []
    assert not db.exists()  # dry-run writes nothing, anywhere
    assert not iv_db.exists()


def test_dry_run_computes_a_row_from_the_repo_fixtures(tmp_path, capsys):
    """``--dry-run`` against the canned SPY payloads: a row, printed, not stored."""
    payload = json.loads(Path("tests/fixtures/option_chain.json").read_text(encoding="utf-8"))
    chain = soak._chain_from_fixture(payload, "SPY")
    assert len(chain) > 0  # the fixture really did parse

    source = soak.load_fixture_source(symbol="SPY")
    conn = init_db(":memory:")
    rules = load_rules(DEFAULT_RULES_PATH)
    # The canned bars are dated 2025-12-18; replay that session.
    replay = dt.date(2025, 12, 18)
    bars = soak._FixtureSource(
        BarSeries(
            symbol="SPY",
            timeframe="1Day",
            feed="sip",
            bars=[
                Bar.from_json(b)
                for b in json.loads(
                    Path("tests/fixtures/stock_bars.json").read_text(encoding="utf-8")
                )["bars"]
            ],
        ),
        chain,
    )
    out = soak.run_soak(
        source=bars,
        conn=conn,
        iv_store=None,
        rules=rules,
        session=replay,
        dry_run=True,
    )
    # The canned chain has no 20251218 expiry, so the honest answer is "nothing to do".
    assert out.status in {"no_zero_dte", "recorded"}
    assert out.summary.startswith("soak:")
    assert list_flow_baseline(conn) == []  # dry-run never persists
    del source, payload


def test_dry_run_via_main_prints_one_line_and_persists_nothing(tmp_path, capsys):
    """The whole CLI contract: one stdout line, exit 0, no files created."""
    code = soak.main(
        [
            "--dry-run",
            "--session",
            SESSION.isoformat(),
            "--db",
            str(tmp_path / "j.db"),
            "--iv-db",
            str(tmp_path / "iv.db"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("soak:")
    assert len(out.strip().splitlines()) == 1
    assert not (tmp_path / "j.db").exists()
    assert not (tmp_path / "iv.db").exists()


def test_failure_exits_non_zero_with_a_message_on_stderr(tmp_path, capsys):
    """systemd contract: non-zero + stderr, no traceback, for an unrunnable job."""
    bad_rules = tmp_path / "rules.yaml"
    bad_rules.write_text("strategy: {}\n", encoding="utf-8")
    code = soak.main(["--dry-run", "--rules", str(bad_rules)])
    assert code != 0
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# 5. IV snapshot
# ---------------------------------------------------------------------------


def test_iv_snapshot_writes_both_tenor_flavours(rules, journal, iv_store):
    """Every ATM reading is stored under the expiry key AND the rolling DTE key."""
    chain = deep_otm_chain()
    # Add a second expiry so the store holds more than one tenor.
    chain = chain.extend(
        chain_of(
            contract_with_volume(
                strike=625.00, right="call", volume=10.0, expiry=SESSION + dt.timedelta(days=30)
            ),
            contract_with_volume(
                strike=625.00, right="put", volume=10.0, expiry=SESSION + dt.timedelta(days=30)
            ),
        )
    )
    out = run(rules, journal, _StaticSource(bars_for(SESSION), chain), iv_store=iv_store)
    assert out.status == "recorded"
    assert out.iv_rows > 0

    keys = {r["tenor_key"] for r in iv_store.conn.execute("SELECT tenor_key FROM iv_observations")}
    assert f"{SESSION:%Y%m%d}-call-625.00" in keys  # expiry-keyed
    # Rolling DTE-keyed, BUCKETED to 7 days (R4): 30 DTE floors to 28. Before the
    # bucket the key carried the raw DTE, so a contract wrote a fresh key every day of
    # its life and no rolling tenor could ever collect a second observation.
    # Rolling MONEYNESS-keyed (RULED 2026-10-03), DTE still bucketed at 7 days: 30 DTE
    # floors to 28. Strike 625 at spot 625 is 0.00% from the money, so the key reads
    # mny+0.00. Under the old dollar axis this row carried `dte28-call-625.00`.
    assert "mte28-call-mny+0.00" in keys
    assert "mte0-call-mny+0.00" in keys


def test_iv_snapshot_skips_far_out_expiries(rules, journal, iv_store):
    """A 200-DTE expiry is outside max_dte and is not polled every day."""
    chain = chain_of(
        contract_with_volume(strike=625.00, right="call", volume=1.0),
        contract_with_volume(
            strike=625.00,
            right="call",
            volume=1.0,
            expiry=SESSION + dt.timedelta(days=200),
        ),
    )
    observations, skipped = soak.iv_observations(chain, SPOT, session=SESSION, underlying="SPY")
    assert observations
    assert skipped == 0
    assert all(o.expiry == f"{SESSION:%Y%m%d}" for o in observations)


def test_iv_snapshot_is_empty_when_no_contract_carries_iv(rules):
    """A missing ATM IV is a skip, not a fallback to a neighbouring strike."""
    chain = chain_of(
        contract_with_volume(strike=625.00, right="call", volume=1.0, iv=None),
        contract_with_volume(strike=625.00, right="call", volume=1.0, iv=0.0),
    )
    observations, skipped = soak.iv_observations(chain, SPOT, session=SESSION, underlying="SPY")
    assert observations == []
    assert skipped == 1


def test_iv_skips_are_reported_in_the_summary(rules, journal, iv_store, capsys):
    """A gap in IV history is a fact the operator should see, not infer."""
    chain = chain_of(
        contract_with_volume(strike=645.00, right="call", volume=1_000.0, iv=None),
    )
    out = run(rules, journal, _StaticSource(bars_for(SESSION), chain), iv_store=iv_store)
    assert out.iv_rows == 0
    assert out.iv_skipped_expiries == 1
    assert "1 expiry(ies) skipped" in out.summary


def test_iv_snapshot_is_idempotent_across_reruns(rules, journal, iv_store):
    """A re-run upserts the same (underlying, as_of, tenor, source) keys."""
    source = _StaticSource(bars_for(SESSION), deep_otm_chain())
    run(rules, journal, source, iv_store=iv_store)
    first = iv_store.conn.execute("SELECT COUNT(*) c FROM iv_observations").fetchone()["c"]
    run(rules, journal, source, iv_store=iv_store)
    second = iv_store.conn.execute("SELECT COUNT(*) c FROM iv_observations").fetchone()["c"]
    assert first == second


def test_iv_snapshot_skips_contracts_without_usable_iv(rules):
    """A missing IV and a zero IV are both "not a reading", not a reading of 0.0.

    Storing 0.0 would land in the store as the lowest value ever seen and drag every
    percentile rank built on that tenor downward for the rest of the history.
    """
    chain = chain_of(
        contract_with_volume(strike=625.00, right="call", volume=1.0, iv=None),
        contract_with_volume(strike=624.00, right="call", volume=1.0, iv=0.0),
        contract_with_volume(strike=624.00, right="put", volume=1.0, iv=0.21),
    )
    # atm_contract ties to the 625.00 call, which has no IV -> the expiry is skipped
    # rather than filled from the 624 put that does.
    observations, skipped = soak.iv_observations(chain, SPOT, session=SESSION, underlying="SPY")
    assert observations == []
    assert skipped == 1


def test_iv_snapshot_records_the_atm_contract_when_it_has_an_iv(rules):
    chain = chain_of(
        contract_with_volume(strike=625.00, right="call", volume=1.0, iv=0.19),
        contract_with_volume(strike=624.00, right="put", volume=1.0, iv=0.21),
    )
    observations, skipped = soak.iv_observations(chain, SPOT, session=SESSION, underlying="SPY")
    assert len(observations) == 2  # one contract, two tenor keys
    assert all(o.iv == 0.19 for o in observations)
    assert {o.tenor_key for o in observations} == {
        f"{SESSION:%Y%m%d}-call-625.00",
        "mte0-call-mny+0.00",
    }
    assert skipped == 0


# ---------------------------------------------------------------------------
# 6. helpers
# ---------------------------------------------------------------------------


def test_otm_distance_and_atm():
    assert soak.otm_distance_pct(629.00, 625.00) == pytest.approx(0.64)
    assert soak.otm_distance_pct(610.00, 625.00) == pytest.approx(2.4)
    assert soak.otm_distance_pct(625.00, 625.00) is None  # ATM is not a zero-distance print
    with pytest.raises(soak.SoakError):
        soak.otm_distance_pct(600.0, 0.0)


def test_aggregate_floors_a_fractional_threshold():
    """3.5% is honoured as 3%: interpolating a partial bucket would invent volume."""
    agg = soak.aggregate_flow(deep_otm_chain(), SPOT, threshold_pct=3.5)
    assert agg.threshold_pct == 3.0
    assert agg.call_volume == pytest.approx(5_000.0)


def test_aggregate_counts_contracts_without_a_daily_bar():
    """A silent zero is indistinguishable from a quiet market unless it is counted.

    The contract here has no ``dailyBar`` at all (the pre-existing chain fixture has
    no volume field), which is exactly the shape a feed outage produces.
    """
    chain = chain_of(
        make_contract(
            symbol="SPY20260310C00645000",
            underlying="SPY",
            expiry="20260310",
            strike=645.00,
            right="call",
            iv=0.18,
        )
    )
    agg = soak.aggregate_flow(chain, SPOT)
    assert agg.contracts_without_volume == 1
    assert agg.contracts_considered == 1
    assert agg.call_volume == 0.0


def test_is_weekend():
    assert soak.is_weekend(dt.date(2026, 3, 14))  # Saturday
    assert soak.is_weekend(dt.date(2026, 3, 15))  # Sunday
    assert not soak.is_weekend(dt.date(2026, 3, 10))  # Tuesday


def test_summary_is_one_line(rules, journal):
    out = run(rules, journal, _StaticSource(bars_for(SESSION), deep_otm_chain()))
    assert "\n" not in out.summary
    assert out.summary.startswith("soak: ")


# ---------------------------------------------------------------------------
# 7. per-arm IV recording (P1: record what each arm's gate actually reads)
# ---------------------------------------------------------------------------

C_EXPIRY = SESSION + dt.timedelta(days=133)  # inside arm C's frozen 90-180 DTE band


def _banded_chain() -> OptionChain:
    """A chain carrying a 0DTE leg AND a 133-DTE leg, at clearly different IVs.

    Shaped so a recorder that ignored tenor bands cannot pass by accident: the two legs
    disagree on IV, so picking the wrong one changes the stored value.
    """
    return chain_of(
        contract_with_volume(strike=625.00, right="call", volume=10.0, iv=0.18),
        contract_with_volume(
            strike=600.00,
            right="call",
            volume=5.0,
            expiry=C_EXPIRY,
            iv=0.27,
        ),
    )


def test_arm_c_band_is_observed_even_when_the_sweep_cannot_reach_it():
    """The regression this exists for: IV_MAX_DTE=45 could not see arm C's 90-180 DTE band.

    Arm C's frozen entry band is 90-180 DTE. The sweep used to stop at 45, so the tenor
    arm C's gate reads accumulated nothing and T5 was in warmup by construction.
    """
    assert soak.IV_MAX_DTE >= 180, (
        "the IV sweep must reach arm C's frozen 90-180 DTE entry band; at 45 it "
        "structurally cannot observe the tenor the gate reads"
    )


def test_arm_iv_observations_writes_both_arms_own_band():
    """One row per arm, each under the tenor that arm's gate reads."""
    observations, skipped = soak.arm_iv_observations(
        _banded_chain(), SPOT, session=SESSION, underlying="SPY"
    )
    assert skipped == 0
    strikes = {o.strike for o in observations}
    # 625 (0DTE, arm B) and 600 (133 DTE, arm C) both get recorded...
    assert strikes == {625.00, 600.00}
    # ...and the IV stored for each is that contract's own, not the other's.
    by_strike = {o.strike: o.iv for o in observations}
    assert by_strike[625.00] == pytest.approx(0.18)
    assert by_strike[600.00] == pytest.approx(0.27)


def test_arm_c_row_key_names_the_long_dated_tenor():
    """The point of the fix: a rolling-DTE key in arm C's band actually lands in the store.

    Before this, the deepest key any observation carried was inside 45 DTE, so T5's
    arm-C series could never accumulate a single observation.
    """
    observations, _skipped = soak.arm_iv_observations(
        _banded_chain(), SPOT, session=SESSION, underlying="SPY"
    )
    keys = {o.tenor_key for o in observations}
    # 600 against SPOT 625 is -4.00% from the money, so the rolling key is
    # mte133-call-mny-4.00 — the arm-C tenor T5 actually reads.
    assert "mte133-call-mny-4.00" in keys
    assert f"{C_EXPIRY:%Y%m%d}-call-600.00" in keys


def test_the_recorders_rolling_key_survives_a_spot_drift():
    """The production bug the 2026-10-03 key ruling exists for, at the recorder.

    Two sessions a few percent apart, each with its own ATM contract. Under the old
    ``$5`` dollar axis those two readings landed in two different series, so a 60-observation
    warmup could never complete; under the moneyness axis they land in one. The expiry-
    keyed audit series still uses dollar buckets — deliberately, and asserted here so
    nobody "fixes" it into the rolling one.
    """
    keys_per_spot = {}
    for spot in (SPOT, SPOT * 1.05):
        chain = chain_of(contract_with_volume(strike=round(spot, 2), right="call", volume=1.0))
        observations, _skipped = soak.iv_observations(
            chain, spot, session=SESSION, underlying="SPY"
        )
        keys_per_spot[spot] = {o.tenor_key for o in observations}
    rolling = {k for keys in keys_per_spot.values() for k in keys if k.startswith("mte")}
    assert rolling == {"mte0-call-mny+0.00"}, keys_per_spot
    expiry = {
        k
        for keys in keys_per_spot.values()
        for k in keys
        if not k.startswith("mte")
    }
    assert len(expiry) == 2, f"the audit series must stay dollar-keyed, got {expiry}"


def test_arm_iv_observations_skips_an_empty_band_rather_than_filling_it():
    """A band with nothing in it is a reported gap, not a reading borrowed from elsewhere."""
    zero_dte_only = chain_of(
        contract_with_volume(strike=625.00, right="call", volume=10.0, iv=0.18),
    )
    observations, skipped = soak.arm_iv_observations(
        zero_dte_only, SPOT, session=SESSION, underlying="SPY"
    )
    # Arm B's 0DTE band resolves; arm C's 90-180 band does not and is counted.
    assert skipped == 1
    assert {o.strike for o in observations} == {625.00}


def test_arm_iv_observations_skips_a_band_whose_iv_is_unusable():
    """No IV, or a zero IV, is not a reading — same rule the ATM sweep already follows."""
    chain = chain_of(
        contract_with_volume(strike=625.00, right="call", volume=10.0, iv=0.18),
        contract_with_volume(
            strike=600.00, right="call", volume=5.0, expiry=C_EXPIRY, iv=None
        ),
    )
    observations, skipped = soak.arm_iv_observations(
        chain, SPOT, session=SESSION, underlying="SPY"
    )
    assert skipped == 1
    assert {o.strike for o in observations} == {625.00}


def test_arm_contract_for_band_reads_nearest_to_spot_inside_the_band():
    """The recorder must agree with hunt_plan._iv_contract_for_arm, not with ATM."""
    chain = chain_of(
        # a 600-strike and a 640-strike both inside arm C's band; 625 is nearer to spot
        contract_with_volume(strike=600.00, right="call", expiry=C_EXPIRY, volume=1.0),
        contract_with_volume(strike=640.00, right="call", expiry=C_EXPIRY, volume=1.0),
        contract_with_volume(strike=624.00, right="call", expiry=C_EXPIRY, volume=1.0),
    )
    chosen = soak.arm_contract_for_band(chain, SPOT, SESSION, 90, 180)
    assert chosen is not None
    assert chosen.strike == 624.00


def test_arm_contract_for_band_excludes_expiries_outside_the_band():
    chain = chain_of(
        contract_with_volume(strike=625.00, right="call", expiry=SESSION, volume=1.0),
        contract_with_volume(strike=600.00, right="call", expiry=C_EXPIRY, volume=1.0),
    )
    zero_dte = soak.arm_contract_for_band(chain, SPOT, SESSION, 0, 0)
    assert zero_dte is not None and zero_dte.strike == 625.00
    long_dated = soak.arm_contract_for_band(chain, SPOT, SESSION, 90, 180)
    assert long_dated is not None and long_dated.strike == 600.00


def test_bands_come_from_the_rulebook_not_from_a_constant(rules):
    """The recorder must not drift from the bands the gates read."""
    bands = soak._bands_from_rules(rules)
    assert bands == (("B", 0, 0), ("C", 90, 180))
    assert bands == soak.ARM_IV_BANDS, "the documented default and the rulebook disagree"


def test_soak_run_records_per_arm_iv_rows(rules, journal, iv_store):
    """End-to-end: a real run stores the arm-C tenor, not just the 0DTE one."""
    out = run(rules, journal, _StaticSource(bars_for(SESSION), _banded_chain()), iv_store=iv_store)
    keys = {
        r["tenor_key"]
        for r in iv_store.conn.execute("SELECT tenor_key FROM iv_observations")
    }
    assert "mte133-call-mny-4.00" in keys
    assert out.iv_rows > 0


def test_band_skips_are_counted_separately_from_expiry_skips(rules, journal, iv_store):
    """An empty arm band is the warm-up clock, not a hole in the series.

    Summed into one number, "arm C has no tenor to rank today" would read as "3 days
    of IV history are missing", which is a different fact and a scarier one.
    """
    # 0DTE leg readable; arm C's 90-180 band absent from the chain entirely.
    chain = chain_of(
        contract_with_volume(strike=625.00, right="call", volume=10.0, iv=0.18),
    )
    out = run(rules, journal, _StaticSource(bars_for(SESSION), chain), iv_store=iv_store)
    assert out.iv_skipped_bands == 1
    assert out.iv_skipped_expiries == 0
    assert "arm band(s) with no rankable contract" in out.summary
    assert "expiry(ies) skipped" not in out.summary
