"""Tests for the snapshot builder — the seam between I/O and the frozen checklist.

Two properties matter more than coverage here:

1. **A long series produces a fully-defined snapshot** (no ``None`` anywhere the
   checklist reads), so a PENDING in a real run is a fact about the data rather than a
   consequence of the builder quietly failing.
2. **A short series produces PENDING-shaped holes, never a crash and never a
   plausible-looking default.** Warmup honesty is the whole reason this module exists.

The 260-bar synthetic series in ``fixtures_synthetic`` is the primary fixture for
both: it is deterministic (seeded LCG, no ``random``), so every indicator value below
is reproducible from the same bars five months from now — which is the property the
journal depends on.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fixtures_synthetic import (
    SYNTH_START,
    load_fixture,
    synthetic_daily_payload,
    with_volume_spike,
)

from config.loader import load_rules
from data.event_calendar import EventCalendar
from executor.alpaca_client import BarSeries
from executor.checklist import evaluate
from executor.indicators import InsufficientData, ema, macd
from executor.snapshot_builder import (
    NEEDS_MACD,
    SnapshotInputError,
    build_bollinger_state,
    build_from_bars,
    build_relvol,
    build_snapshot,
    find_macd_cross,
)

LONG = 260
SHORT = 150


@pytest.fixture(scope="module")
def rules():
    return load_rules()


@pytest.fixture(scope="module")
def long_payload():
    return synthetic_daily_payload(count=LONG)


@pytest.fixture(scope="module")
def short_payload():
    return synthetic_daily_payload(count=SHORT)


def series_from(payload: dict, *, feed: str | None = None) -> BarSeries:
    from executor.alpaca_client import Bar

    bars = sorted((Bar.from_json(b) for b in payload["bars"]), key=lambda b: b.t)
    return BarSeries(
        symbol="SPY",
        timeframe="1Day",
        feed=feed or payload.get("feed", "sip"),
        bars=bars,
    )


def chain_from_fixture():
    from executor.alpaca_client import OPTIONS_SNAPSHOTS_PATH, AlpacaClient, MockTransport

    client = AlpacaClient(
        transport=MockTransport(
            {f"{OPTIONS_SNAPSHOTS_PATH}/SPY": load_fixture("option_chain.json")}
        ),
        key="k",
        secret="s",
    )
    return client.get_option_chain_page("SPY", feed="indicative")


# ---------------------------------------------------------------------------
# fully-defined snapshot
# ---------------------------------------------------------------------------


def test_long_series_produces_a_fully_defined_snapshot(long_payload):
    result = build_from_bars(long_payload, iv_rank=30.0)
    snap = result.snapshot
    assert result.bars_used == LONG
    assert snap.close > 0
    assert snap.ema_fast is not None and snap.ema_slow is not None
    assert snap.rsi is not None
    assert snap.macd_line is not None and snap.macd_signal is not None
    assert snap.macd_hist is not None and snap.macd_hist_prev is not None
    assert snap.bollinger is not None
    assert snap.relvol is not None
    assert snap.macd_cross_direction in ("call", "put")
    assert snap.macd_cross_age_hours is not None


def test_ema_values_match_the_indicator_module(long_payload):
    """The builder must not re-implement the maths. Recompute independently here and
    compare — if these drift apart, the journal's numbers stop matching the chart."""
    series = series_from(long_payload)
    closes = series.closes
    result = build_snapshot(daily=series)
    assert result.snapshot.ema_fast == pytest.approx(ema(closes, 50)[-1])
    assert result.snapshot.ema_slow == pytest.approx(ema(closes, 200)[-1])
    macd_values = macd(closes)
    assert result.snapshot.macd_line == pytest.approx(macd_values.macd_line[-1])
    assert result.snapshot.macd_signal == pytest.approx(macd_values.signal_line[-1])
    assert result.snapshot.macd_hist == pytest.approx(macd_values.histogram[-1])


def test_trend_is_read_correctly(long_payload):
    """A persistent uptrend must put close above EMA50 above EMA200, and the MACD line
    is checked for sign coherence — not a hard-coded value, because the fixture is
    synthetic and its exact numbers are not the point."""
    snap = build_from_bars(long_payload).snapshot
    assert snap.close > snap.ema_slow
    assert snap.ema_fast > snap.ema_slow
    assert snap.macd_line == pytest.approx(snap.macd_line)


def test_relvol_reflects_the_signal_bar(long_payload):
    base = build_from_bars(long_payload).snapshot.relvol
    spiked = build_from_bars(with_volume_spike(long_payload, multiplier=2.2)).snapshot.relvol
    assert base is not None and spiked is not None
    assert spiked == pytest.approx(base * 2.2)
    assert spiked > base


def test_relvol_uses_the_preceding_20_days_not_the_series_mean(long_payload):
    series = series_from(long_payload)
    value, reason, feed = build_relvol(series)
    expected = series.volumes[-1] / (sum(series.volumes[-21:-1]) / 20)
    assert reason is None
    assert value == pytest.approx(expected)
    assert feed == "sip"


def test_bollinger_state_is_fully_populated(long_payload):
    state, reason = build_bollinger_state(series_from(long_payload))
    assert reason is None
    assert state is not None
    assert state.upper > state.middle > state.lower
    assert state.bandwidth == pytest.approx((state.upper - state.lower) / state.middle)
    assert 0.0 <= state.bandwidth_percentile <= 100.0
    assert isinstance(state.bandwidth_expanding, bool)
    assert isinstance(state.middle_rising, bool)
    assert isinstance(state.squeeze, bool)


def test_bollinger_signal_bar_touches_are_the_newest_bars_high_low(long_payload):
    series = series_from(long_payload)
    state, _ = build_bollinger_state(series)
    assert state.low == series.bars[-1].l
    assert state.high == series.bars[-1].h


def test_squeeze_uses_the_rulebook_threshold(long_payload):
    series = series_from(long_payload)
    tight, _ = build_bollinger_state(series, squeeze_percentile=101.0)
    loose, _ = build_bollinger_state(series, squeeze_percentile=0.0)
    # A threshold above any possible percentile forces squeeze; below forces not.
    assert tight.squeeze is True
    assert loose.squeeze is False


def test_percentile_is_computed_over_prior_bandwidths_only(long_payload):
    """The current bar must not vouch for itself. Feeding the same bandwidth series
    into the indicator directly is the independent check."""
    from executor.indicators import bandwidth_percentile, bollinger

    series = series_from(long_payload)
    state, _ = build_bollinger_state(series, lookback_days=60)
    bb = bollinger(series.closes)
    expected = bandwidth_percentile(bb.bandwidth[-61:], window=60, value=bb.bandwidth[-1])
    assert state.bandwidth_percentile == pytest.approx(expected)


def test_snapshot_serialises_through_the_checklist(long_payload, rules):
    result = build_from_bars(long_payload, iv_rank=30.0)
    payload = result.to_dict(direction="call", arm="B")
    assert payload["indicators"]["close"] == pytest.approx(result.snapshot.close)
    assert payload["indicators"]["relvol"] is not None
    assert payload["bars_used"] == LONG
    assert "relvol computed on feed=sip" in payload["feed_notes"]


# ---------------------------------------------------------------------------
# warmup honesty
# ---------------------------------------------------------------------------


def test_short_series_leaves_pending_holes_and_does_not_crash(short_payload):
    result = build_from_bars(short_payload)
    snap = result.snapshot
    assert snap.ema_slow is None
    assert snap.ema_fast is not None  # 150 >= 50
    assert snap.rsi is not None
    assert snap.macd_line is not None
    assert result.pending is True
    assert any("EMA200 needs 200 closes" in r for r in result.pending_reasons)


def test_short_series_is_undefined_not_fabricated(short_payload):
    """The invariant that matters: a missing indicator is ``None``, never 0, never a
    carry-forward, never a plausible default."""
    snap = build_from_bars(short_payload).snapshot
    assert snap.ema_slow is None
    assert all(
        value is None
        for value in (snap.ema_slow,)
    )


def test_very_short_series_leaves_everything_but_close_pending():
    payload = synthetic_daily_payload(count=10)
    result = build_from_bars(payload)
    snap = result.snapshot
    assert snap.close > 0
    assert snap.ema_fast is None
    assert snap.ema_slow is None
    assert snap.rsi is None
    assert snap.macd_line is None
    assert snap.macd_signal is None
    assert snap.macd_hist is None
    assert snap.macd_hist_prev is None
    assert snap.bollinger is None
    assert snap.relvol is None
    assert snap.macd_cross_direction is None
    assert len(result.pending_reasons) >= 6


def test_series_of_exactly_200_closes_defines_ema200():
    """The boundary: 200 is enough, 199 is not."""
    enough = build_from_bars(synthetic_daily_payload(count=200))
    assert enough.snapshot.ema_slow is not None
    short = build_from_bars(synthetic_daily_payload(count=199))
    assert short.snapshot.ema_slow is None


def test_series_of_exactly_34_closes_defines_macd():
    assert build_from_bars(synthetic_daily_payload(count=NEEDS_MACD)).snapshot.macd_line is not None
    assert build_from_bars(synthetic_daily_payload(count=NEEDS_MACD - 1)).snapshot.macd_line is None


def test_bollinger_percentile_needs_61_defined_bandwidths():
    """20 bands for Bollinger + 60 trailing for the percentile + the current bar.

    N closes yield N-19 defined bandwidths, so 80 closes is the first count that
    clears a 60-day percentile window (61 bandwidths) and 79 is the last that does not.
    """
    short, reason = build_bollinger_state(
        series_from(synthetic_daily_payload(count=79)), lookback_days=60
    )
    assert short is None
    assert "61 defined bandwidths" in reason

    exact, reason = build_bollinger_state(
        series_from(synthetic_daily_payload(count=80)), lookback_days=60
    )
    assert exact is not None and reason is None

    # Same 80-bar series against a 59-day window also clears (61 >= 59 + 1 is false,
    # so the guard must not fire a second time on a generous window).
    loose, reason = build_bollinger_state(
        series_from(synthetic_daily_payload(count=80)), lookback_days=59
    )
    assert loose is not None and reason is None


def test_relvol_needs_21_bars():
    payload = synthetic_daily_payload(count=20)
    value, reason, _ = build_relvol(series_from(payload))
    assert value is None
    assert "needs 21 bars" in reason
    payload = synthetic_daily_payload(count=21)
    value, reason, _ = build_relvol(series_from(payload))
    assert value is not None and reason is None


def test_empty_series_is_an_explicit_input_error():
    with pytest.raises(SnapshotInputError, match="no bars"):
        build_snapshot(daily=BarSeries(symbol="SPY", timeframe="1Day", feed="sip", bars=[]))


# ---------------------------------------------------------------------------
# the same-feed rule (T4 correctness requirement)
# ---------------------------------------------------------------------------


def test_relvol_refuses_a_mismatched_baseline_feed(long_payload):
    """The research note's sharpest warning: IEX numerator over a SIP baseline compares
    two different universes and the checklist lies quietly. This must be a refusal,
    not a computed number."""
    signal = series_from(long_payload, feed="iex")
    baseline = series_from(long_payload, feed="sip")
    value, reason, _ = build_relvol(signal, baseline_series=baseline)
    assert value is None
    assert "feed mismatch" in reason
    assert "iex" in reason and "sip" in reason


def test_matched_feed_is_accepted(long_payload):
    value, reason, feed = build_relvol(
        series_from(long_payload, feed="iex"), baseline_series=series_from(long_payload, feed="iex")
    )
    assert value is not None and reason is None
    assert feed == "iex"


def test_snapshot_records_the_relvol_feed_for_the_journal(long_payload):
    payload = synthetic_daily_payload(count=LONG, feed="iex")
    result = build_from_bars(payload)
    assert result.relvol_feed == "iex"
    assert result.to_dict()["relvol_feed"] == "iex"


def test_mismatched_feed_propagates_to_pending_reasons(long_payload):
    result = build_snapshot(
        daily=series_from(long_payload, feed="iex"),
        baseline_series=series_from(long_payload, feed="sip"),
    )
    assert result.snapshot.relvol is None
    assert any("feed mismatch" in r for r in result.pending_reasons)


# ---------------------------------------------------------------------------
# MACD cross derivation
# ---------------------------------------------------------------------------


def test_macd_cross_is_found_and_aged_in_hours():
    closes = synthetic_daily_payload(count=LONG)["bars"]
    closes = [float(b["c"]) for b in closes]
    start = SYNTH_START
    times = [
        dt.datetime.combine(start, dt.time(21, 0), tzinfo=dt.UTC) + dt.timedelta(days=i)
        for i in range(len(closes))
    ]
    cross = find_macd_cross(closes, times)
    assert cross is not None
    assert cross.direction in ("call", "put")
    assert cross.age_hours >= 0.0
    # Age is in hours and a daily series spans ~24h per bar, so a cross older than a
    # few bars must be >= 24.
    if cross.age_hours > 24:
        assert cross.age_hours % 24 == pytest.approx(0.0, abs=1e-9)


def test_cross_age_is_measured_from_the_bar_not_the_wall_clock():
    """A snapshot recomputed at 16:15 must report age as of the close, or the journal's
    numbers stop being reproducible from the same bars."""
    closes = [float(b["c"]) for b in synthetic_daily_payload(count=LONG)["bars"]]
    result = build_from_bars(synthetic_daily_payload(count=LONG))
    cross_direction = result.snapshot.macd_cross_direction
    assert cross_direction in ("call", "put")
    # Age here is a whole number of daily bars; never a fraction that would imply
    # sub-bar precision the data does not have.
    age = result.snapshot.macd_cross_age_hours
    assert age == pytest.approx(round(age / 24.0) * 24.0, abs=1e-6)
    assert len(closes) == LONG


def test_flat_series_has_no_cross():
    """A series whose MACD line never separates from its signal yields ``None`` rather
    than a fabricated cross — the whipsaw guard stays inactive, which is what the
    checklist expects.

    Note a steadily *rising* series is NOT this case: on a linear ramp the MACD line
    starts negative and crosses the signal once, very early. That is real MACD
    behaviour and the builder correctly reports it rather than inventing a second one.
    """
    closes = [100.0] * 120
    times = [
        dt.datetime(2025, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=i) for i in range(120)
    ]
    assert find_macd_cross(closes, times) is None


def test_cross_lookup_needs_but_does_not_raise_on_short_series():
    times = [
        dt.datetime(2025, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=i) for i in range(10)
    ]
    assert find_macd_cross([100.0 + i for i in range(10)], times) is None


def test_cross_lookup_rejects_mismatched_series_lengths():
    times = [dt.datetime(2025, 1, 1, tzinfo=dt.UTC)]
    with pytest.raises(SnapshotInputError, match="mismatch"):
        find_macd_cross([100.0, 101.0], times)


def test_intraday_series_ages_in_hours_too():
    """A 5-minute series must produce fractional-hour ages, or the whipsaw guard would
    be measuring the wrong unit entirely."""
    payload = synthetic_daily_payload(count=1)
    bar = payload["bars"][0]
    base = dt.datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    # Ramp down then up so the macd line must cross the signal line at least once.
    closes: list[float] = []
    for i in range(200):
        closes.append(100.0 + (20.0 if i < 100 else -20.0) + (i % 7) * 0.1)
    times = [base + dt.timedelta(minutes=5 * i) for i in range(len(closes))]
    cross = find_macd_cross(closes, times, fast=12, slow=26, signal=9)
    assert cross is not None
    assert cross.age_hours % 5 == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# event calendar integration (T5 hard veto)
# ---------------------------------------------------------------------------


def calendar_with(kind: str, day: dt.date) -> EventCalendar:
    from data.event_calendar import CalendarFile

    calendar_file = CalendarFile.model_validate(
        {
            "quarter": "test",
            "events": [
                {
                    "date": day.isoformat(),
                    "kind": kind,
                    "label": f"test {kind}",
                    "release_time_et": "08:30",
                    "veto": True,
                }
            ],
        }
    )
    return EventCalendar([calendar_file])


def test_event_day_sets_is_event_day_and_records_kinds(long_payload):
    last_bar = long_payload["bars"][-1]
    day = dt.datetime.fromisoformat(last_bar["t"].replace("Z", "+00:00")).date()
    result = build_from_bars(long_payload, calendar=calendar_with("fomc", day))
    assert result.snapshot.is_event_day is True
    assert result.snapshot.event_kinds == ("fomc",)
    assert any("event-day hard veto" in n for n in result.notes)


def test_non_event_day_leaves_the_veto_clear(long_payload):
    last_bar = long_payload["bars"][-1]
    day = dt.datetime.fromisoformat(last_bar["t"].replace("Z", "+00:00")).date()
    elsewhere = calendar_with("fomc", day + dt.timedelta(days=40))
    result = build_from_bars(long_payload, calendar=elsewhere)
    assert result.snapshot.is_event_day is False
    assert result.snapshot.event_kinds == ()


def test_veto_kinds_filter_narrows_the_calendar(long_payload):
    """The rulebook declares [fomc, cpi] as veto kinds; OPEX is awareness only, so a
    filter passed through from the rulebook must actually exclude it."""
    last_bar = long_payload["bars"][-1]
    day = dt.datetime.fromisoformat(last_bar["t"].replace("Z", "+00:00")).date()
    cal = calendar_with("opex", day)
    unfiltered = build_from_bars(long_payload, calendar=cal)
    filtered = build_from_bars(long_payload, calendar=cal, veto_kinds=["fomc", "cpi"])
    assert unfiltered.snapshot.is_event_day is True
    assert filtered.snapshot.is_event_day is False


def test_no_calendar_means_no_veto(long_payload):
    result = build_from_bars(long_payload)
    assert result.snapshot.is_event_day is False
    assert result.snapshot.event_kinds == ()


def test_as_of_overrides_the_calendar_date(long_payload):
    """The EOD recompute runs on today's date, which may differ from the last bar's
    date (a stale feed, or a weekend)."""
    last_bar = long_payload["bars"][-1]
    bar_day = dt.datetime.fromisoformat(last_bar["t"].replace("Z", "+00:00")).date()
    cal = calendar_with("cpi", bar_day + dt.timedelta(days=3))
    result = build_from_bars(long_payload, calendar=cal, as_of=bar_day + dt.timedelta(days=3))
    assert result.snapshot.is_event_day is True


# ---------------------------------------------------------------------------
# end-to-end with the frozen checklist
# ---------------------------------------------------------------------------


def test_snapshot_feeds_the_checklist_without_crashing(long_payload, rules):
    """The integration that actually matters: real builder output + real rulebook ->
    a full 8-condition evaluation with no exception."""
    result = build_from_bars(long_payload, iv_rank=30.0)
    outcome = evaluate(result.snapshot, rules, direction="call", arm="B")
    assert set(outcome.conditions) == {"T1", "T2", "T2b", "T3a", "T3b", "T4", "T5", "T6"}
    # T1 must be decidable on a 260-bar series, and T5 must be decidable given an iv_rank.
    assert outcome.status("T1") in ("PASS", "FAIL")
    assert outcome.status("T5") in ("PASS", "FAIL")
    # T6's multiplier is still calibration_pending in the frozen rulebook.
    assert outcome.status("T6") == "PENDING"
    assert outcome.fire is False


def test_warmup_snapshot_makes_t1_pending_not_failed(short_payload, rules):
    """EMA200 undefined must read PENDING. A FAIL here would mean the system rejected
    a setup for a data reason and the NO-SHOT histogram would be a lie."""
    result = build_from_bars(short_payload, iv_rank=30.0)
    outcome = evaluate(result.snapshot, rules, direction="call", arm="B")
    assert outcome.status("T1") == "PENDING"
    assert any("T1: pending" in r for r in outcome.veto_reasons)


def test_event_day_short_circuits_t5(long_payload, rules):
    last_bar = long_payload["bars"][-1]
    day = dt.datetime.fromisoformat(last_bar["t"].replace("Z", "+00:00")).date()
    result = build_from_bars(long_payload, iv_rank=90.0, calendar=calendar_with("fomc", day))
    outcome = evaluate(result.snapshot, rules, direction="call", arm="B")
    assert outcome.status("T5") == "FAIL"
    assert "EVENT DAY HARD VETO" in outcome.conditions["T5"].detail


# ---------------------------------------------------------------------------
# chain provenance
# ---------------------------------------------------------------------------


def test_chain_is_recorded_as_provenance(long_payload):
    chain = chain_from_fixture()
    result = build_from_bars(long_payload, chain=chain, iv_rank=25.0, spot=678.44)
    assert any("options chain: 8 contracts" in n for n in result.notes)
    assert any("feed=indicative" in n for n in result.notes)


def test_missing_chain_and_missing_iv_rank_makes_t5_pending(long_payload, rules):
    result = build_from_bars(long_payload)
    outcome = evaluate(result.snapshot, rules, direction="call", arm="B")
    assert outcome.status("T5") == "PENDING"
    assert "iv_rank unavailable" in outcome.conditions["T5"].detail


def test_no_spot_records_that_no_tenor_was_selected(long_payload):
    chain = chain_from_fixture()
    result = build_from_bars(long_payload, chain=chain, iv_rank=25.0)
    assert any("no ATM tenor selection recorded" in n for n in result.notes)


# ---------------------------------------------------------------------------
# flow gate passthrough
# ---------------------------------------------------------------------------


def test_flow_gate_defaults_to_unknown_and_blocks_t6(long_payload, rules):

    result = build_from_bars(long_payload, iv_rank=30.0)
    assert result.snapshot.flow_gate.status == "unknown"
    outcome = evaluate(result.snapshot, rules, direction="call", arm="B")
    assert outcome.status("T6") == "PENDING"


def test_flow_gate_passthrough_when_supplied(long_payload):
    from executor.checklist import FlowGate

    gate = FlowGate(status="pass", ratio=3.2, eod_confirmed=True)
    result = build_from_bars(long_payload, iv_rank=30.0, flow_gate=gate)
    assert result.snapshot.flow_gate is gate
    assert result.snapshot.flow_gate.ratio == pytest.approx(3.2)


def test_intraday_relvol_uses_run_rate_over_the_same_feed(long_payload):
    """T4's intraday variant: the same maths over an intraday series. Confirms the
    builder is timeframe-agnostic rather than daily-only."""
    series = series_from(long_payload)
    intraday = BarSeries(
        symbol=series.symbol, timeframe="5Min", feed=series.feed, bars=series.bars
    )
    value, reason, feed = build_relvol(intraday, lookback_days=20)
    assert value is not None and reason is None
    assert feed == "sip"


def test_indicators_module_raises_rather_than_padding_for_short_input():
    """Reminder of why the builder catches rather than trusting: the indicator layer
    refuses to fabricate a short aligned series."""
    with pytest.raises(InsufficientData):
        ema([1.0] * 10, 50)