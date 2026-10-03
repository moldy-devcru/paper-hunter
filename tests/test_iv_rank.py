"""Tests for the self-built IV-rank store.

The behaviours worth testing here are not the percentile arithmetic — they are the
three ways this store could lie about how much history it has:

1. reporting a rank below the warmup floor,
2. mixing VIX proxy rows into a real SPY IV rank without saying so,
3. letting a series vouch for itself (including the current reading in its own
   comparison window, which biases every value toward 50).

Each has a test that fails if the corresponding guard is removed.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from executor.iv_rank import (
    DEFAULT_LOOKBACK_DAYS,
    DTE_BUCKET_DAYS,
    MIN_OBSERVATIONS,
    MONEYNESS_BUCKET_PCT,
    VIX_PROXY_SOURCE,
    VIX_PROXY_TENOR,
    VIX_PROXY_UNDERLYING,
    IvObservation,
    IvRankError,
    IvRankStore,
    dte_tenor_key,
    moneyness,
    moneyness_bucket,
    moneyness_tenor_key,
    parse_vix_csv,
    seed_vix_proxy,
    strike_bucket,
    tenor_key,
)

TENOR = tenor_key(expiry="20260117", right="call", strike=700.0)


@pytest.fixture
def store(tmp_path):
    s = IvRankStore.open(tmp_path / "ivrank.db", min_observations=10)
    yield s
    s.close()


def seed_days(store: IvRankStore, values, *, start=None, tenor=TENOR,
              underlying="SPY", source="alpaca_chain", step_days=1):
    """Seed ``values`` on consecutive days.

    ``start`` defaults to 30 days before today so the rows land inside the default
    365-day lookback no matter when the suite runs. Tests that care about *age* pass
    an explicit old ``start`` and an explicit ``as_of`` on the query.
    """
    day = (
        dt.date.fromisoformat(start)
        if start
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=30)
    )
    for i, iv in enumerate(values):
        store.record(
            underlying=underlying,
            as_of=day + dt.timedelta(days=i * step_days),
            tenor=tenor,
            iv=iv,
            source=source,
            expiry="20260117",
            right="call",
            strike=700.0,
        )
    return day + dt.timedelta(days=(len(values) - 1) * step_days)


# ---------------------------------------------------------------------------
# tenor keys
# ---------------------------------------------------------------------------


def test_tenor_key_buckets_strikes():
    """Bucketing is why the series ever fills: a raw-strike key gives every strike in
    a chain its own separate (forever-warming) history."""
    assert tenor_key(expiry="20260117", right="call", strike=700.0) == "20260117-call-700.00"
    # Half away from zero: 702.50 rounds UP to the 705 bucket (built-in round() would
    # banker's-round it to 700 and make bucketing non-monotonic at the edges).
    assert tenor_key(expiry="20260117", right="call", strike=702.5) == "20260117-call-705.00"
    assert tenor_key(expiry="20260117", right="call", strike=703.9) == "20260117-call-705.00"
    assert tenor_key(expiry="20260117", right="call", strike=702.49) == TENOR
    assert tenor_key(expiry="20260117", right="call", strike=705.0) == "20260117-call-705.00"
    assert tenor_key(expiry="20260117", right="put", strike=700.0) == "20260117-put-700.00"
    assert tenor_key(expiry="20260118", right="call", strike=700.0) != TENOR


def test_tenor_key_rejects_bad_input():
    with pytest.raises(IvRankError):
        tenor_key(expiry="20260117", right="straddle", strike=700.0)
    with pytest.raises(IvRankError):
        tenor_key(expiry="20260117", right="call", strike=700.0, bucket_size=0)


# ---------------------------------------------------------------------------
# moneyness tenor keying (RULED 2026-10-03, operator) — the schema that lets the
# rolling series reach MIN_OBSERVATIONS at all
# ---------------------------------------------------------------------------


def test_moneyness_bucket_is_zero_at_the_money():
    assert moneyness(650.0, 650.0) == pytest.approx(0.0)
    assert moneyness_bucket(650.0, 650.0) == pytest.approx(0.0)
    # A dollar axis cannot say this at all: it needs a spot to compare against.
    assert moneyness(663.0, 650.0) == pytest.approx(0.02, abs=1e-9)


def test_the_atm_bucket_survives_a_spot_drift_that_walks_the_dollar_axis():
    """The bug this ruling exists for, stated as one assertion.

    An ATM strike follows spot, so its dollar bucket changes constantly and its
    moneyness bucket never does. Over this walk the dollar axis mints several different
    keys for the same contract-following-the-money — and ``MIN_OBSERVATIONS`` is 60.
    """
    spots = [500.0, 587.5, 625.0, 650.0, 700.0, 775.0, 812.5]
    dollar_keys = {strike_bucket(spot) for spot in spots}
    assert len(dollar_keys) > 1, "the dollar axis did not rotate — the test proves nothing"
    assert {moneyness_bucket(spot, spot) for spot in spots} == {0.0}


def test_strikes_within_one_percent_of_spot_share_a_bucket():
    """Half-width of a 2pp bucket is 1pp, so a +/-1% neighbourhood is one series."""
    spot = 650.0
    for strike in (646.0, 650.0, 656.0):
        assert moneyness_bucket(strike, spot) == pytest.approx(0.0)
    # The first strike outside that neighbourhood rolls to the next bucket.
    assert moneyness_bucket(657.0, spot) == pytest.approx(MONEYNESS_BUCKET_PCT)


def test_the_bucket_width_is_the_measured_two_percent():
    """Pinned because the feasibility measurement the ruling rests on measured THIS width."""
    assert MONEYNESS_BUCKET_PCT == 2.0
    # +0.9% -> still the ATM bucket; +1.0% is the exact tie and rounds away from zero
    # (the same half-away-from-zero rule as `strike_bucket`); +2% -> one bucket up.
    assert moneyness_bucket(650.0 * 0.991, 650.0) == pytest.approx(0.0)
    assert moneyness_bucket(650.0 * 1.01, 650.0) == pytest.approx(2.0)
    assert moneyness_bucket(650.0 * 1.02, 650.0) == pytest.approx(2.0)
    assert moneyness_bucket(650.0 * 1.04, 650.0) == pytest.approx(4.0)
    # And the scale cancels: the same relative strike at any spot level.
    assert moneyness_bucket(800.0 * 1.04, 800.0) == pytest.approx(4.0)


def test_moneyness_bucket_rejects_bad_input():
    with pytest.raises(IvRankError, match="spot must be > 0"):
        moneyness_bucket(650.0, 0.0)
    with pytest.raises(IvRankError, match="bucket_pct"):
        moneyness_bucket(650.0, 650.0, bucket_pct=0)


def test_moneyness_tenor_key_buckets_dte_and_rights():
    key = moneyness_tenor_key(dte=133, right="call", moneyness_bucket=0.0)
    assert key == "mte133-call-mny+0.00"
    # The DTE axis is unchanged from R4: floored to a multiple of the bucket width.
    assert moneyness_tenor_key(
        dte=30, right="call", moneyness_bucket=0.0, dte_bucket_days=DTE_BUCKET_DAYS
    ) == "mte28-call-mny+0.00"
    assert moneyness_tenor_key(dte=0, right="put", moneyness_bucket=-4.0) == (
        "mte0-put-mny-4.00"
    )


def test_moneyness_tenor_key_rejects_bad_input():
    with pytest.raises(IvRankError, match="right must be"):
        moneyness_tenor_key(dte=7, right="straddle", moneyness_bucket=0.0)
    with pytest.raises(IvRankError, match="dte must be"):
        moneyness_tenor_key(dte=-1, right="call", moneyness_bucket=0.0)
    with pytest.raises(IvRankError, match="dte_bucket_days"):
        moneyness_tenor_key(dte=7, right="call", moneyness_bucket=0.0, dte_bucket_days=0)


def test_the_two_key_schemas_cannot_alias():
    """Namespacing is the whole anti-mixing mechanism — pin it at both ends.

    An old dollar-axis row and a new moneyness-axis row must never share a key, or a
    series built under one meaning would silently absorb observations from the other.
    """
    collisions = []
    for dte in (0, 28, 133):
        for right in ("call", "put"):
            for strike, spot in ((650.0, 650.0), (600.0, 625.0), (700.0, 650.0)):
                new = moneyness_tenor_key(
                    dte=dte,
                    right=right,
                    moneyness_bucket=moneyness_bucket(strike, spot),
                )
                for size in (5.0, 10.0, 25.0):
                    old = dte_tenor_key(
                        dte=dte, right=right, strike_bucket=strike_bucket(strike, size)
                    )
                    if new == old:
                        collisions.append((new, old))
    assert not collisions, f"key schemas alias: {collisions}"
    # And the prefixes are disjoint, so a prefix-scanning reader cannot confuse them.
    assert moneyness_tenor_key(dte=7, right="call", moneyness_bucket=0.0).startswith("mte")
    assert dte_tenor_key(dte=7, right="call", strike_bucket=650.0).startswith("dte")


def test_old_schema_rows_are_invisible_to_a_moneyness_query(store):
    """The R7 disposition, achieved by construction rather than by a migration."""
    old_key = dte_tenor_key(dte=28, right="call", strike_bucket=650.0)
    new_key = moneyness_tenor_key(dte=28, right="call", moneyness_bucket=0.0)
    today = dt.datetime.now(dt.UTC).date()
    for i in range(12):
        store.record(
            underlying="SPY",
            as_of=today - dt.timedelta(days=30 - i),
            tenor=old_key,
            iv=0.20 + i * 0.001,
            source="alpaca_chain",
            expiry="20260117",
            right="call",
            strike=650.0,
        )
    # A dollar-axis history of 12 rows must not answer a moneyness query — not
    # partially, not as a warmup count, not at all.
    assert store.count_observations("SPY", new_key) == 0
    assert store.history("SPY", new_key) == []
    result = store.iv_rank(0.20, "SPY", new_key)
    assert result.rank is None
    assert result.status == "no_history"


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def test_record_then_read_round_trips(store):
    seed_days(store, [0.20, 0.21, 0.19])
    history = store.history("SPY", TENOR)
    assert history == [0.20, 0.21, 0.19]
    assert store.count_observations("SPY", TENOR) == 3


def test_recording_the_same_day_again_corrects_rather_than_duplicates(store):
    """A re-run of the EOD poll should fix a number, not fail and not double-count."""
    day = seed_days(store, [0.20] * 5)
    store.record(
        underlying="SPY", as_of=day.isoformat(), tenor=TENOR, iv=0.31,
        source="alpaca_chain", expiry="20260117", right="call", strike=700.0,
    )
    assert store.count_observations("SPY", TENOR) == 5
    latest = store.latest("SPY", TENOR)
    assert latest is not None and latest.iv == pytest.approx(0.31)


def test_record_rejects_non_positive_iv(store):
    with pytest.raises(IvRankError, match="positive"):
        store.record(underlying="SPY", as_of="2025-09-01", tenor=TENOR, iv=0.0)
    with pytest.raises(IvRankError, match="positive"):
        store.record(underlying="SPY", as_of="2025-09-01", tenor=TENOR, iv=None)


def test_record_accepts_date_and_datetime_objects(store):
    today = dt.datetime.now(dt.UTC).date()
    store.record(underlying="SPY", as_of=today - dt.timedelta(days=2), tenor=TENOR, iv=0.20,
                 expiry="20260117", right="call", strike=700.0)
    store.record(
        underlying="SPY",
        as_of=dt.datetime.combine(today - dt.timedelta(days=1), dt.time(21, 0), tzinfo=dt.UTC),
        tenor=TENOR, iv=0.21, expiry="20260117", right="call", strike=700.0,
    )
    assert store.count_observations("SPY", TENOR) == 2


def test_proxy_rows_may_not_claim_option_tenor_fields(store):
    """A VIX row that carries expiry/strike could be selected by an option-tenor
    query. Refused at the door rather than filtered at read time."""
    with pytest.raises(IvRankError, match="must not carry"):
        store.record(underlying=VIX_PROXY_UNDERLYING, as_of="2025-09-01", tenor=TENOR,
                     iv=18.5, source=VIX_PROXY_SOURCE, is_proxy=True,
                     expiry="20260117", right="call", strike=700.0)


# ---------------------------------------------------------------------------
# warmup
# ---------------------------------------------------------------------------


def test_no_history_reports_none_not_zero(store):
    result = store.iv_rank(0.22, "SPY", TENOR)
    assert result.rank is None
    assert result.status == "no_history"
    assert result.defined is False
    assert "no IV observations stored" in result.reason


def test_below_the_floor_is_warmup_and_blocks(store):
    """The whole point: an under-built history must read as UNDEFINED, because the
    checklist turns undefined into PENDING which blocks, not into a pass."""
    seed_days(store, [0.20] * 9)
    result = store.iv_rank(0.25, "SPY", TENOR)
    assert result.rank is None
    assert result.status == "warmup"
    assert result.observations == 9
    assert "need >= 10" in result.reason


def test_warmup_to_defined_transition_at_the_floor(store):
    """The exact boundary: 9 observations -> warmup, 10 -> defined."""
    anchor = seed_days(store, [0.20] * 9)
    assert store.iv_rank(0.25, "SPY", TENOR).status == "warmup"

    seed_days(store, [0.30], start=(anchor + dt.timedelta(days=1)).isoformat())
    result = store.iv_rank(0.25, "SPY", TENOR)
    assert result.status == "ok"
    assert result.observations == 10
    # Nine stored values are 0.20 (all strictly below the 0.25 being scored) and one
    # is 0.30 (above it) -> 9/10 = 90.0.
    assert result.rank == pytest.approx(90.0)
    assert result.defined is True


def test_default_floor_is_sixty_observations():
    assert MIN_OBSERVATIONS == 60
    assert DEFAULT_LOOKBACK_DAYS == 365


def test_default_store_uses_the_documented_floor(tmp_path):
    s = IvRankStore.open(tmp_path / "ivrank.db")
    try:
        assert s.min_observations == MIN_OBSERVATIONS
    finally:
        s.close()


def test_floor_override_is_honoured(store):
    seed_days(store, [0.20] * 4)
    assert store.iv_rank(0.25, "SPY", TENOR).status == "warmup"
    result = store.iv_rank(0.25, "SPY", TENOR, min_observations=4)
    assert result.status == "ok"


def test_floor_below_two_is_rejected(store):
    with pytest.raises(IvRankError, match=">= 2"):
        store.iv_rank(0.25, "SPY", TENOR, min_observations=1)
    with pytest.raises(IvRankError, match=">= 2"):
        IvRankStore(sqlite3.connect(":memory:"), min_observations=1)


# ---------------------------------------------------------------------------
# percentile arithmetic
# ---------------------------------------------------------------------------


def test_rank_is_percent_strictly_below(store):
    seed_days(store, [0.10, 0.20, 0.30, 0.40, 0.50])
    # 4 of the 5 stored values (0.10-0.40) are strictly below 0.45. The floor is
    # lowered to 5 so this arithmetic is not masked by warmup.
    result = store.iv_rank(0.45, "SPY", TENOR, min_observations=5)
    assert result.rank == pytest.approx(80.0)
    assert "4/5" in result.reason


def test_ties_are_not_counted_as_below(store):
    """A value tied with its own history is not 'strictly below' it — the same strict
    convention indicators.bandwidth_percentile uses."""
    seed_days(store, [0.20] * 10)
    result = store.iv_rank(0.20, "SPY", TENOR)
    assert result.rank == pytest.approx(0.0)


def test_rank_does_not_include_the_current_reading(store):
    """A series vouching for itself biases every reading toward 50. Scoring against
    10 stored values, not 11."""
    seed_days(store, [0.10] * 10)
    result = store.iv_rank(0.50, "SPY", TENOR)
    assert result.observations == 10
    assert result.rank == pytest.approx(100.0)


def test_low_iv_rank_and_high_iv_rank(store):
    values = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
    seed_days(store, values)
    assert store.iv_rank(0.06, "SPY", TENOR).rank == pytest.approx(10.0)   # < 50 -> T5 pass
    assert store.iv_rank(0.49, "SPY", TENOR).rank == pytest.approx(90.0)   # >= 50 -> T5 fail


def test_missing_current_iv_is_never_a_zero_rank(store):
    seed_days(store, [0.20] * 10)
    result = store.iv_rank(None, "SPY", TENOR)
    assert result.rank is None
    assert result.defined is False


def test_lookback_window_excludes_older_observations(store):
    """Points outside the window must not count, or a stale reading from years ago
    would make the percentile look authoritative."""
    anchor = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=30)
    old_start = (anchor - dt.timedelta(days=1000)).isoformat()
    seed_days(store, [0.10] * 30, start=old_start)
    end = seed_days(store, [0.50] * 10, start=anchor.isoformat())
    # as_of must be the LAST seeded day; the window is [as_of - lookback, as_of].
    recent = store.iv_rank(0.45, "SPY", TENOR, lookback_days=60, as_of=end)
    assert recent.observations == 10
    assert recent.rank == pytest.approx(0.0)  # all in-window values are >= 0.45

    # Widen the window and the decade-old 0.10s pull the rank up.
    wide = store.iv_rank(0.45, "SPY", TENOR, lookback_days=3650, as_of=end)
    assert wide.observations == 40
    assert wide.rank > recent.rank


def test_observations_outside_the_window_report_empty_window(store):
    anchor = dt.date(2026, 6, 1)
    seed_days(store, [0.20] * 10, start=(anchor - dt.timedelta(days=200)).isoformat())
    result = store.iv_rank(0.25, "SPY", TENOR, lookback_days=30, as_of=anchor)
    assert result.rank is None
    assert result.status == "empty_window"
    assert "none fall inside" in result.reason


# ---------------------------------------------------------------------------
# proxy provenance
# ---------------------------------------------------------------------------


def test_vix_csv_parses_into_flagged_proxy_rows():
    rows = parse_vix_csv(load_fixture_csv())
    assert len(rows) == 10
    assert all(row.is_proxy for row in rows)
    assert all(row.underlying == VIX_PROXY_UNDERLYING for row in rows)
    assert all(row.tenor_key == VIX_PROXY_TENOR for row in rows)
    assert all(row.source == VIX_PROXY_SOURCE for row in rows)
    assert rows[0].as_of == "2024-01-02"
    assert rows[0].iv == pytest.approx(14.55)


def load_fixture_csv() -> str:
    from fixtures_synthetic import FIXTURES

    return (FIXTURES / "vix_history_sample.csv").read_text(encoding="utf-8")


def test_vix_csv_accepts_a_file_handle(tmp_path):
    path = tmp_path / "vix.csv"
    path.write_text(load_fixture_csv(), encoding="utf-8")
    with path.open(encoding="utf-8") as handle:
        assert len(parse_vix_csv(handle)) == 10


def test_vix_csv_without_a_close_column_is_rejected():
    with pytest.raises(IvRankError, match="CLOSE"):
        parse_vix_csv("DATE,OPEN\n2025-01-02,1.0\n")


def test_vix_csv_with_no_usable_rows_is_rejected():
    with pytest.raises(IvRankError, match="zero usable rows"):
        parse_vix_csv("DATE,OPEN,HIGH,LOW,CLOSE\n2025-01-02,1,1,1,.\n")


def test_seeding_the_vix_proxy_flags_every_row(store):
    written = seed_vix_proxy(
        store, load_fixture_csv(), today="2024-01-20", lookback_days=60
    )
    assert written == 10
    rows = store.observations(VIX_PROXY_UNDERLYING)
    assert len(rows) == 10
    assert all(row.is_proxy for row in rows)
    # Cboe's MM/DD/YYYY dates are normalised to ISO on the way in.
    assert rows[0].as_of == "2024-01-02"
    assert rows[-1].as_of == "2024-01-16"


def test_proxy_rows_are_excluded_from_real_iv_rank_by_default(store):
    """The single most important guarantee in this module: VIX never silently becomes
    SPY's IV rank."""
    seed_days(store, [0.20] * 5 + [0.30] * 5)
    seed_vix_proxy(store, load_fixture_csv(), today="2024-01-20", lookback_days=60)

    real = store.iv_rank(0.25, "SPY", TENOR)
    assert real.observations == 10
    # Five stored values (0.20) sit below 0.25 and five (0.30) sit above -> 50.0.
    # If any VIX row had leaked in, the rank would be 0.0.
    assert real.rank == pytest.approx(50.0)
    assert real.status == "ok"
    assert real.is_proxy is False


def test_proxy_must_be_requested_explicitly_and_is_then_labelled(store):
    seed_days(store, [0.20] * 10)
    seed_vix_proxy(store, load_fixture_csv(), today="2024-01-20", lookback_days=60)

    mixed = store.iv_rank(0.25, "SPY", TENOR, include_proxy=True)
    assert mixed.status == "proxy"
    assert mixed.is_proxy is True
    assert mixed.to_dict()["status"] == "proxy"
    assert mixed.to_dict()["is_proxy"] is True


def test_proxy_rows_cannot_be_read_through_an_option_tenor(store):
    """Double protection: even asking for proxy under the SPY option tenor returns
    nothing, because proxy rows are filed under the VIX underlying."""
    seed_vix_proxy(store, load_fixture_csv(), today="2024-01-20", lookback_days=60)
    assert store.history("SPY", TENOR, include_proxy=True) == []
    assert store.iv_rank(20.0, "SPY", TENOR, include_proxy=True).status == "no_history"


def test_proxy_iv_rank_itself_can_be_computed_and_is_marked(store):
    """Useful for the warmup backfill path: the VIX series has its own rank, and it is
    honestly labelled as proxy so the journal can say so."""
    seed_vix_proxy(store, load_fixture_csv(), today="2024-01-20", lookback_days=60)
    result = store.iv_rank(
        15.0, VIX_PROXY_UNDERLYING, VIX_PROXY_TENOR,
        include_proxy=True, min_observations=5, as_of="2024-01-16",
    )
    assert result.status == "proxy"
    assert result.rank is not None


def test_seeding_respects_the_lookback_cutoff(store):
    """Backfilling 9,286 rows of 1990-2026 history would make every future window
    enormous and the percentile look authoritative while meaning nothing."""
    written = seed_vix_proxy(store, load_fixture_csv(), today="2024-01-20", lookback_days=5)
    assert written < 10
    rows = store.observations(VIX_PROXY_UNDERLYING)
    assert rows
    assert all(row.as_of >= "2024-01-15" for row in rows)


# ---------------------------------------------------------------------------
# construction / hygiene
# ---------------------------------------------------------------------------


def test_store_is_a_separate_database_from_the_journal(tmp_path):
    """Documented choice: the IV store is a mutable time series, the journal is an
    append-only immutability-enforced ledger. Mixing them would put an overwritable
    table under triggers written for a different purpose."""
    from executor.iv_rank import DEFAULT_DB_PATH

    iv_db = tmp_path / "ivrank.db"
    journal_db = tmp_path / "journal.db"
    s = IvRankStore.open(iv_db)
    s.record(underlying="SPY", as_of="2025-09-01", tenor=TENOR, iv=0.20,
             expiry="20260117", right="call", strike=700.0)
    s.close()

    import journal.store as js

    jc = js.init_db(journal_db)
    js.append_decision(
        jc,
        js.DecisionEntry(
            ts=js.utcnow(),
            arm="B",
            kind="NO_TRADE",
            checklist_snapshot={"close": 678.44},
            checklist_state={"T1": {"pass": False}},
            reasoning="seed",
            strategy_version="0.1.0+test",
        ),
    )
    jc.close()

    iv = sqlite3.connect(str(iv_db))
    tables = {r[0] for r in iv.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    iv.close()
    assert "iv_observations" in tables
    assert "journal" not in tables and "decisions" not in tables

    assert DEFAULT_DB_PATH.name == "ivrank.db"
    assert DEFAULT_DB_PATH.parent.name == "data"


def test_latest_returns_none_when_nothing_is_stored(store):
    assert store.latest("SPY", TENOR) is None


def test_result_serialises_for_the_journal(store):
    seed_days(store, [0.20] * 5 + [0.30] * 5)
    payload = store.iv_rank(0.25, "SPY", TENOR).to_dict()
    assert payload["iv_rank"] == pytest.approx(50.0)
    assert payload["status"] == "ok"
    assert payload["underlying"] == "SPY"
    assert payload["tenor_key"] == TENOR
    assert payload["lookback_days"] == 365


def test_history_respects_an_explicit_as_of(store):
    anchor = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=30)
    seed_days(store, [0.20] * 5, start=anchor.isoformat())
    seed_days(store, [0.40] * 5, start=(anchor + dt.timedelta(days=5)).isoformat())
    # As of the fifth day only the first five exist.
    fifth = (anchor + dt.timedelta(days=4)).isoformat()
    assert store.count_observations("SPY", TENOR, as_of=fifth) == 5


def test_observations_helper_lists_everything(store):
    seed_days(store, [0.20] * 3)
    seed_vix_proxy(store, load_fixture_csv(), today="2024-01-20", lookback_days=60)
    assert len(store.observations()) == 13
    assert len(store.observations("SPY")) == 3
    assert len(store.observations(VIX_PROXY_UNDERLYING)) == 10


def test_record_many_writes_every_row(store):
    today = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=30)
    rows = [
        IvObservation(
            underlying="SPY",
            as_of=(today + dt.timedelta(days=i)).isoformat(),
            tenor_key=TENOR,
            iv=0.20 + i * 0.01,
            source="alpaca_chain",
            expiry="20260117",
            right="call",
            strike=700.0,
        )
        for i in range(5)
    ]
    assert store.record_many(rows) == 5
    assert store.count_observations("SPY", TENOR) == 5

# ---------------------------------------------------------------------------
# origin provenance — ruling R9
# ---------------------------------------------------------------------------


def test_live_is_the_default_origin_and_survives_a_legacy_database(tmp_path):
    """Every pre-existing row was a live EOD poll, so the migration must say exactly that.

    A legacy store is simulated by creating the pre-R9 schema by hand and then opening it
    with the current class — which is the only way to test the ALTER path at all, since
    ``CREATE TABLE IF NOT EXISTS`` silently no-ops on an existing table.
    """
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        """
        CREATE TABLE iv_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            underlying TEXT NOT NULL,
            as_of TEXT NOT NULL,
            tenor_key TEXT NOT NULL,
            expiry TEXT, right TEXT, strike REAL,
            iv REAL NOT NULL,
            source TEXT NOT NULL,
            is_proxy INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (underlying, as_of, tenor_key, source)
        )
        """
    )
    today = dt.datetime.now(dt.UTC).date()
    conn.execute(
        "INSERT INTO iv_observations (underlying, as_of, tenor_key, iv, source, is_proxy,"
        " created_at) VALUES ('SPY', ?, 'legacy', 0.2, 'alpaca_chain', 0, '2024-01-01T00:00:00Z')",
        (today.isoformat(),),
    )
    conn.commit()
    conn.close()

    s = IvRankStore.open(legacy, min_observations=2)
    row = s.observations("SPY")[0]
    assert row.origin == "live"
    # And a new write defaults to live too, with no call-site change anywhere.
    s.record(underlying="SPY", as_of=today, tenor="legacy", iv=0.21)
    assert s.latest("SPY", "legacy").origin == "live"
    s.close()


def test_backfilled_and_live_rows_coexist_for_the_same_session_and_key(store):
    """Uniqueness is (underlying, as_of, tenor_key, source) — so both can be stored.

    This is what makes the R9 splice auditable: you can hold a key's live-only history and
    its backfilled history side by side and measure the difference, instead of being told
    not to look.
    """
    day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=5)
    store.record(underlying="SPY", as_of=day, tenor=TENOR, iv=0.20, source="alpaca_chain")
    store.record(
        underlying="SPY",
        as_of=day,
        tenor=TENOR,
        iv=0.26,
        source="bars_bs_inversion",
        origin="backfill",
    )
    assert store.count_observations("SPY", TENOR) == 2
    assert store.count_observations("SPY", TENOR, origin="live") == 1
    assert store.count_observations("SPY", TENOR, origin="backfill") == 1
    sources = {o.source for o in store.observations("SPY")}
    origins = {o.origin for o in store.observations("SPY")}
    assert sources == {"alpaca_chain", "bars_bs_inversion"}
    assert origins == {"live", "backfill"}


def test_backfilled_observations_count_toward_warmup_per_ruling_r9(store):
    """The load-bearing consequence of R9: a purely backfilled key can leave warmup."""
    store.min_observations = 5
    day = dt.datetime.now(dt.UTC).date()
    for i in range(6):
        store.record(
            underlying="SPY",
            as_of=day - dt.timedelta(days=30 - i),
            tenor=TENOR,
            iv=0.20 + i * 0.01,
            source="bars_bs_inversion",
            origin="backfill",
        )
    result = store.iv_rank(0.25, "SPY", TENOR)
    assert result.status == "ok"
    assert result.observations == 6
    assert result.rank == pytest.approx(83.3333333, abs=1e-6)


def test_origin_filter_can_reproduce_a_live_only_series(store):
    store.min_observations = 5
    day = dt.datetime.now(dt.UTC).date()
    for i in range(6):
        store.record(
            underlying="SPY", as_of=day - dt.timedelta(days=30 - i),
            tenor=TENOR, iv=0.20 + i * 0.01, source="alpaca_chain",
        )
        store.record(
            underlying="SPY", as_of=day - dt.timedelta(days=30 - i),
            tenor=TENOR, iv=0.40 + i * 0.01, source="bars_bs_inversion", origin="backfill",
        )
    mixed = store.iv_rank(0.35, "SPY", TENOR)
    live_only = store.iv_rank(0.35, "SPY", TENOR, origin="live")
    assert mixed.observations == 12 and live_only.observations == 6
    # Same current reading, materially different answer — which is the entire reason the
    # column exists rather than folding provenance into `source`.
    assert mixed.rank != live_only.rank


def test_unknown_origin_is_refused_at_the_door(store):
    today = dt.datetime.now(dt.UTC).date()
    with pytest.raises(IvRankError, match="origin must be"):
        store.record(underlying="SPY", as_of=today, tenor=TENOR, iv=0.2, origin="guessed")


def test_unwritable_store_still_opens_and_still_reads():
    """A store on read-only media must be readable, or a deployment cannot see its own data.

    Not hypothetical: the rehearsal and the UI open the DEPLOYED store under
    ``/opt/paper-hunter``, owned by the ``paper-hunter`` service user. R9 added a
    ``CREATE INDEX`` that is a genuine write, where the pre-R9 ``CREATE TABLE IF NOT
    EXISTS`` was a no-op on an existing table -- so R9 turned "we could not add an index"
    into "the fixture cannot be built at all", and that regression broke three rehearsal
    tests. The fixture below is deliberately LEGACY (no ``origin`` column, no index) so
    the migration genuinely has work it cannot do.
    """
    import os
    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "deployed.db")
        legacy = sqlite3.connect(path)
        legacy.execute(
            "CREATE TABLE iv_observations ("
            " underlying TEXT NOT NULL, as_of TEXT NOT NULL, tenor_key TEXT NOT NULL,"
            " iv REAL NOT NULL, source TEXT NOT NULL, is_proxy INTEGER NOT NULL DEFAULT 0,"
            " expiry TEXT, right TEXT, created_at TEXT,"
            " PRIMARY KEY (underlying, as_of, tenor_key, source))"
        )
        legacy.execute(
            "INSERT INTO iv_observations (underlying, as_of, tenor_key, iv, source)"
            " VALUES ('SPY', '2026-10-01', 'mte91-call-mny+0.00', 0.18, 'alpaca_chain')"
        )
        legacy.commit()
        legacy.close()
        os.chmod(path, 0o444)

        conn = sqlite3.connect(path)      # readable, not writable, as the deployment is
        conn.row_factory = sqlite3.Row
        reopened = IvRankStore(conn)

        # 1. It OPENED, and the legacy row is still readable through the shipped reader.
        assert reopened.count_observations("SPY", "mte91-call-mny+0.00") == 1
        res = reopened.iv_rank(0.20, "SPY", "mte91-call-mny+0.00",
                              min_observations=2, as_of="2026-10-02")
        assert res.status == "warmup"

        # 2. The migration it could not do is REPORTED, not swallowed -- a reader must be
        #    able to tell a skipped migration from a current one.
        assert reopened.migration_note is not None
        assert "skipped" in reopened.migration_note
        conn.close()
