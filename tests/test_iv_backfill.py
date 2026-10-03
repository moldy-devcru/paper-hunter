"""The IV backfill pipeline: selection rules, refusals, provenance, and what it cannot do.

The pipeline's job is to reconstruct a *past* measurement, which means every place it has
to choose something is a place it can quietly be wrong. So the tests here pin the choices
as explicit contracts rather than as incidental behaviour:

- the expiry it picks (the shortest-dated in-band one — see the module docstring for why
  that is what the live chain's tie-break lands on);
- the strike it picks (nearest spot *among contracts that have a bar*);
- the refusal cases, especially the zero-DTE one, because arm B's band makes it the
  ordinary case rather than an edge;
- the provenance of everything it writes, since R9 counts these rows toward a frozen
  threshold.
"""

from __future__ import annotations

import datetime as dt

import pytest

from executor.alpaca_client import AlpacaAPIError, Bar, BarSeries, OptionBarSeries
from executor.black_scholes import black_scholes_price
from executor.iv_backfill import (
    DEFAULT_RETRY_BASE_SECONDS,
    FEED_FLOOR,
    FORBIDDEN_COOLDOWN_SECONDS,
    AlpacaIvSource,
    BackfillReport,
    IvBackfillError,
    SessionPlan,
    candidate_expiries,
    ladder_strikes,
    observations_for_session,
    occ_symbol,
    plan_sessions,
    run_backfill,
    select_expiry,
    third_friday,
)
from executor.iv_rank import MONEYNESS_BUCKET_PCT, IvRankStore

SESSION = dt.date(2026, 10, 1)
SPOT = 650.0


def bar(day: dt.date, close: float, volume: float = 500.0) -> Bar:
    return Bar(t=dt.datetime.combine(day, dt.time(9, 30), tzinfo=dt.UTC),
               o=close, h=close, l=close, c=close, v=volume, n=10, vw=close)


# ---------------------------------------------------------------------------
# expiry enumeration
# ---------------------------------------------------------------------------


def test_third_friday_is_the_third_friday():
    # March 2027: the 1st is a Monday, so Fridays are 5, 12, 19 -> third is the 19th.
    assert third_friday(2027, 3) == dt.date(2027, 3, 19)
    assert third_friday(2027, 6) == dt.date(2027, 6, 18)
    for month in range(1, 13):
        assert third_friday(2026, month).weekday() == 4


def test_candidate_expiries_covers_weeklies_and_quarterlies_inside_the_band():
    expiries = candidate_expiries(SESSION, 90, 180)
    assert expiries == sorted(set(expiries))
    assert all(e.weekday() == 4 for e in expiries)          # all Fridays
    assert all(90 <= (e - SESSION).days <= 180 for e in expiries)
    # The quarterly third-Friday must be IN the set — dropping it silently removes the
    # longest expiries, which is exactly where arm C's band lives.
    quarterlies = {third_friday(2027, 1), third_friday(2027, 2)}
    assert quarterlies <= set(expiries)


def test_select_expiry_is_the_shortest_dated_in_band_expiry():
    """This is the rule that mirrors the live chain's tie-break; see the module docstring."""
    chosen = select_expiry(SESSION, 90, 180)
    band = candidate_expiries(SESSION, 90, 180)
    assert chosen == band[0]
    # 90 days after 2026-10-01 is a Wednesday, so the nearest in-band expiry is the
    # following Friday at 92 DTE — i.e. the first listed expiry at or after the band floor,
    # which is what "nearest the lower edge" means for a weekly listing calendar.
    dte = (chosen - SESSION).days
    assert 90 <= dte < 97


def test_select_expiry_returns_none_for_an_empty_band():
    assert select_expiry(SESSION, 0, 0) is None      # 2026-10-01 is a Thursday
    assert select_expiry(SESSION, 90, 180) is not None
    with pytest.raises(IvBackfillError, match="empty DTE band"):
        candidate_expiries(SESSION, 180, 90)


def test_select_expiry_on_a_friday_returns_today_for_arm_b():
    friday = dt.date(2026, 10, 2)
    assert select_expiry(friday, 0, 0) == friday


# ---------------------------------------------------------------------------
# strike ladder + OCC layout
# ---------------------------------------------------------------------------


def test_ladder_is_symmetric_snapped_and_finite():
    ladder = ladder_strikes(SPOT)
    assert ladder == tuple(sorted(ladder))
    assert abs(ladder[0] - (SPOT * 0.97)) <= 1.0
    assert abs(ladder[-1] - (SPOT * 1.03)) <= 1.0
    assert all(abs((s / SPOT - 1) * 100) <= 3.0001 for s in ladder)


def test_ladder_rejects_impossible_parameters():
    for kwargs in ({"spot": 0.0}, {"spot": -5.0}):
        with pytest.raises(IvBackfillError):
            ladder_strikes(**kwargs)
    with pytest.raises(IvBackfillError, match="strike step"):
        ladder_strikes(SPOT, strike_step=0.0)
    with pytest.raises(IvBackfillError, match="range_pct"):
        ladder_strikes(SPOT, range_pct=0.0)


def test_occ_symbol_is_eight_digit_strike():
    """A 9-digit strike is a hard 400 from the route. This is the regression for that."""
    symbol = occ_symbol("SPY", dt.date(2027, 1, 1), "call", 650.0)
    assert symbol == "SPY270101C00650000"
    assert len(symbol) == 3 + 6 + 1 + 8
    assert occ_symbol("SPY", dt.date(2027, 1, 1), "put", 1234.5) == "SPY270101P01234500"
    with pytest.raises(IvBackfillError):
        occ_symbol("SPY", dt.date(2027, 1, 1), "call", 0.0)


# ---------------------------------------------------------------------------
# per-session inversion
# ---------------------------------------------------------------------------


def _plan(*, spot: float = SPOT, expiry: dt.date = dt.date(2027, 1, 1),
          session: dt.date = SESSION) -> SessionPlan:
    strikes = ladder_strikes(spot)
    return SessionPlan(
        session=session,
        spot=spot,
        expiry=expiry,
        symbols=tuple(
            occ_symbol("SPY", expiry, right, s) for s in strikes for right in ("call", "put")
        ),
        strikes=strikes,
    )


def _bars_for(plan: SessionPlan, *, strike: float = 650.0, vol: float = 0.16,
              volume: float = 500.0, rights=("call", "put")) -> dict:
    dte = (plan.expiry - plan.session).days
    out = {}
    for right in rights:
        price = black_scholes_price(
            spot=plan.spot, strike=strike, dte=dte, sigma=vol, right=right
        )
        symbol = occ_symbol("SPY", plan.expiry, right, strike)
        out.setdefault(symbol, {})[plan.session] = bar(plan.session, price, volume)
    return out


def test_session_writes_both_tenor_keys_with_backfill_provenance():
    plan = _plan()
    observations, report = observations_for_session(plan, _bars_for(plan), underlying="SPY")
    assert report.observations_written == 4          # 2 rights x 2 keys
    assert {o.origin for o in observations} == {"backfill"}
    assert {o.source for o in observations} == {"bars_bs_inversion"}
    assert not any(o.is_proxy for o in observations)
    keys = {o.tenor_key for o in observations}
    # DTE 92 floors into the weekly bucket 91, and 650/650-1 = 0.00% -> bucket +0.00.
    assert "mte91-call-mny+0.00" in keys
    assert "mte91-put-mny+0.00" in keys
    # The expiry-keyed audit series is written too, at the $5 dollar bucket.
    assert "20270101-call-650.00" in keys


def test_session_picks_the_strike_nearest_spot_among_those_with_a_bar():
    plan = _plan()
    # Only 652 has a bar; 650 (which is closer to a 650.0 spot) is absent.
    bars = _bars_for(plan, strike=652.0)
    observations, _ = observations_for_session(plan, bars, underlying="SPY")
    assert {o.strike for o in observations} == {652.0}
    # 652/650 - 1 = +0.31% -> bucket +0.00, not the dollar bucket 655.00.
    assert "mte91-call-mny+0.00" in {o.tenor_key for o in observations}


def test_session_records_both_rights_so_the_gate_can_read_either():
    plan = _plan()
    observations, _ = observations_for_session(
        plan, _bars_for(plan, rights=("call",)), underlying="SPY"
    )
    assert {o.right for o in observations} == {"call"}


def test_session_with_no_bars_is_skipped_and_counted_not_filled():
    plan = _plan()
    observations, report = observations_for_session(plan, {}, underlying="SPY")
    assert observations == []
    assert report.skips == {"no_contract_with_a_bar": 1}


def test_zero_dte_session_refuses_rather_than_inventing_an_iv():
    """Arm B's band. The refusal is the contract, so it is asserted as one."""
    friday = dt.date(2026, 10, 2)
    plan = _plan(expiry=friday, session=friday)
    symbol = occ_symbol("SPY", friday, "call", 650.0)
    bars = {symbol: {friday: bar(friday, 0.50, 5000.0)}}
    observations, report = observations_for_session(plan, bars, underlying="SPY")
    assert observations == []
    assert report.skips == {"zero_dte_no_invertible_iv": 1}


def test_thin_bar_is_skipped_by_name_rather_than_inverted():
    plan = _plan()
    observations, report = observations_for_session(
        plan, _bars_for(plan, volume=0.0), underlying="SPY"
    )
    assert observations == []
    assert sum(report.skips.values()) == 2          # both rights refused
    assert "IvNotInvertible" in report.skips


def test_vega_floor_does_not_eat_contracts_inside_the_default_ladder():
    """The conditioning guard must not reject the band arm C actually reads.

    Checked rather than assumed: at spot 650 the 3% ladder tops out at strike 669, and the
    *minimum* vega anywhere in that ladder — at the far edge, at the shortest DTE that still
    inverts — is several times the guard's floor of 1.0. So on this ladder the guard is
    inert, and the pipeline's refusals come from liquidity and no-arbitrage checks instead.
    (The guard biting on a genuinely collapsed contract is pinned in
    ``test_black_scholes.test_invert_bar_close_refuses_vega_collapse``.)
    """
    plan = _plan(expiry=SESSION + dt.timedelta(days=10))   # 10 DTE, far edge of the ladder
    far = 669.0
    observations, report = observations_for_session(
        plan, _bars_for(plan, strike=far, vol=0.16), underlying="SPY"
    )
    assert report.observations_written == 4
    assert report.skips == {}


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_plan_sessions_skips_days_with_no_spy_bar_as_holidays():
    closes = {dt.date(2026, 10, 1): 650.0, dt.date(2026, 10, 5): 652.0}
    plans = plan_sessions(closes, underlying="SPY", dte_min=90, dte_max=180,
                          start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 6))
    assert [p.session for p in plans] == [dt.date(2026, 10, 1), dt.date(2026, 10, 5)]


def test_plan_sessions_requests_both_rights_for_every_ladder_strike():
    closes = {SESSION: SPOT}
    plans = plan_sessions(closes, underlying="SPY", dte_min=90, dte_max=180,
                          start=SESSION, end=SESSION)
    assert len(plans) == 1
    plan = plans[0]
    assert len(plan.symbols) == 2 * len(plan.strikes)
    assert sum(1 for s in plan.symbols if s[9] == "C") == len(plan.strikes)


# ---------------------------------------------------------------------------
# the run, end to end, offline
# ---------------------------------------------------------------------------


class FakeSource:
    """Offline source: synthesises a bar per requested symbol at a known vol."""

    def __init__(self, *, vol: float = 0.16, closes: dict | None = None) -> None:
        self.calls = 0
        self.option_calls = 0
        self.vol = vol
        self.closes = closes or {}
        self.requested: set[str] = set()

    def equity_daily_bars(self, symbol, *, start, end):
        self.calls += 1
        bars = []
        for day in sorted(self.closes):
            if start <= day.isoformat() <= end:
                bars.append(bar(day, self.closes[day]))
        return BarSeries(symbol=symbol, timeframe="1Day", feed="sip", bars=bars)

    def option_daily_bars(self, symbols, *, start, end, page_token=None):
        self.calls += 1
        self.option_calls += 1
        self.requested.update(symbols)
        out: dict[str, list[Bar]] = {}
        window_end = dt.date.fromisoformat(end[:10])
        for symbol in symbols:
            expiry = dt.datetime.strptime(symbol[3:9], "%y%m%d").date()
            strike = int(symbol[10:]) / 1000.0
            right = "call" if symbol[9] == "C" else "put"
            day = dt.date.fromisoformat(start[:10])
            if day > min(window_end, expiry):
                continue
            while day <= min(window_end, expiry):
                dte = (expiry - day).days
                if dte > 0 and day.weekday() < 5:
                    price = black_scholes_price(
                        spot=self.closes.get(day, SPOT), strike=strike, dte=dte,
                        sigma=self.vol, right=right,
                    )
                    if price > 0.01:
                        out.setdefault(symbol, []).append(bar(day, price, 400.0))
                day += dt.timedelta(days=1)
        return OptionBarSeries(bars_by_symbol=out, next_page_token=None)


@pytest.fixture
def store(tmp_path):
    s = IvRankStore.open(tmp_path / "ivrank.db", min_observations=60)
    yield s
    s.close()


def test_run_backfill_refuses_a_window_older_than_the_confirmed_feed_floor(store):
    source = FakeSource()
    with pytest.raises(IvBackfillError, match="precedes the confirmed option-data floor"):
        run_backfill(source, store, start=dt.date(2023, 1, 1), end=SESSION,
                     bands=[("C", 90, 180)])
    with pytest.raises(IvBackfillError, match="is after end"):
        run_backfill(source, store, start=SESSION, end=dt.date(2026, 1, 1),
                     bands=[("C", 90, 180)])


def test_run_backfill_writes_warmable_keys_for_arm_c_and_nothing_for_arm_b(store):
    """The headline contract: C warms, B cannot, and the report says so per band."""
    closes = {}
    day = dt.date(2026, 4, 1)
    while day <= dt.date(2026, 9, 30):
        if day.weekday() < 5:
            closes[day] = 650.0
        day += dt.timedelta(days=1)
    source = FakeSource(closes=closes)

    report = run_backfill(
        source, store,
        start=dt.date(2026, 4, 1), end=dt.date(2026, 9, 30),
        bands=[("B", 0, 0), ("C", 90, 180)],
    )
    assert isinstance(report, BackfillReport)
    # Arm B contributes zero plans on non-expiry days and zero rows everywhere.
    band_plans = {arm: sessions for arm, _lo, _hi, sessions in report.bands_seen}
    assert band_plans["C"] > 100
    assert report.observations_written > 0
    assert report.origin_counts == {"backfill": report.observations_written}
    # Everything written is backfill provenance.
    rows = store.observations("SPY")
    assert rows and {r.origin for r in rows} == {"backfill"}
    assert {r.source for r in rows} == {"bars_bs_inversion"}


def test_run_backfill_reaches_the_warmup_floor_for_the_gate_key(store):
    closes = {}
    day = dt.date(2026, 4, 1)
    while day <= dt.date(2026, 9, 30):
        if day.weekday() < 5:
            closes[day] = 650.0
        day += dt.timedelta(days=1)
    report = run_backfill(FakeSource(closes=closes), store,
                          start=dt.date(2026, 4, 1), end=dt.date(2026, 9, 30),
                          bands=[("C", 90, 180)])
    gate_keys = [k for k in report.tenor_key_counts if k.startswith("mte")]
    assert gate_keys
    counts = {k: store.count_observations("SPY", k) for k in gate_keys}
    assert max(counts.values()) >= 60, f"no gate key reached the floor: {counts}"


def test_dry_run_writes_nothing_but_still_reports(store):
    closes = {dt.date(2026, 9, 1): 650.0, dt.date(2026, 9, 2): 651.0}
    report = run_backfill(FakeSource(closes=closes), store, dry_run=True,
                          start=dt.date(2026, 9, 1), end=dt.date(2026, 9, 2),
                          bands=[("C", 90, 180)])
    assert report.observations_written > 0
    assert store.observations("SPY") == []


def test_summary_prints_the_tenor_key_table():
    report = BackfillReport(start=dt.date(2026, 1, 1), end=dt.date(2026, 2, 1))
    report.tenor_key_counts = {"mte126-call-mny+0.00": 120, "mte126-put-mny+0.00": 119}
    text = report.summary()
    assert "mte126-call-mny+0.00" in text
    assert "bands (arm, dte, sessions)" in text


def test_feed_floor_is_the_confirmed_2024_02_01():
    assert FEED_FLOOR == dt.date(2024, 2, 1)
    assert MONEYNESS_BUCKET_PCT == 2.0

# ---------------------------------------------------------------------------
# source retry / pacing
# ---------------------------------------------------------------------------


class _FlakyClient:
    """Raises ``status`` on the first ``fail_times`` options calls, then succeeds."""

    def __init__(self, status: int, fail_times: int) -> None:
        self.status = status
        self.fail_times = fail_times
        self.attempts = 0

    def get_option_daily_bars(self, symbols, *, start, end, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise AlpacaAPIError(self.status, "OPRA agreement is not signed", path="/x")
        return OptionBarSeries(bars_by_symbol={s: [] for s in symbols})


def _clock_with_steps(steps):
    """A monotonic clock that advances by one second per reading."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return clock, state


def test_transient_403_is_retried_and_succeeds():
    slept: list[float] = []
    clock, _ = _clock_with_steps([])
    source = AlpacaIvSource(
        _FlakyClient(403, fail_times=2),
        sleep=slept.append,
        clock=clock,
        min_interval_seconds=0.0,
    )
    series = source.option_daily_bars(["SPY260918C00700000"], start="s", end="e")
    assert series is not None
    assert source.calls == 3 and source.retries == 2 and source.cooldowns == 2
    # The first retry waits FORBIDDEN_COOLDOWN_SECONDS, not the 5s a 429 would get:
    # recovery from this 403 was measured at ~2 minutes, so a short ramp would just burn
    # attempts. This assertion is the reason those two numbers are different constants.
    assert slept[0] == FORBIDDEN_COOLDOWN_SECONDS


def test_429_uses_the_short_ramp_not_the_403_cooldown():
    slept: list[float] = []
    clock, _ = _clock_with_steps([])
    source = AlpacaIvSource(
        _FlakyClient(429, fail_times=1),
        sleep=slept.append,
        clock=clock,
        min_interval_seconds=0.0,
    )
    source.option_daily_bars(["SPY260918C00700000"], start="s", end="e")
    assert slept[0] == DEFAULT_RETRY_BASE_SECONDS
    assert source.cooldowns == 0          # a 429 is not the misleading entitlement error


def test_unretryable_status_is_not_retried():
    clock, _ = _clock_with_steps([])
    source = AlpacaIvSource(
        _FlakyClient(422, fail_times=99),
        sleep=lambda _: None,
        clock=clock,
        min_interval_seconds=0.0,
    )
    with pytest.raises(AlpacaAPIError):
        source.option_daily_bars(["SPY260918C00700000"], start="s", end="e")
    assert source.calls == 1 and source.retries == 0


def test_retries_are_bounded_then_the_error_surfaces():
    """The ceiling is the retry. Exhausting it re-raises rather than looping forever."""
    slept: list[float] = []
    clock, _ = _clock_with_steps([])
    source = AlpacaIvSource(
        _FlakyClient(403, fail_times=99),
        max_retries=2,
        sleep=slept.append,
        clock=clock,
        min_interval_seconds=0.0,
    )
    with pytest.raises(AlpacaAPIError):
        source.option_daily_bars(["SPY260918C00700000"], start="s", end="e")
    assert source.calls == 3             # 1 attempt + 2 retries, then it gives up
    assert len(slept) == 2               # 30s, then 60s — and then it stops


def test_requests_are_paced_to_the_minimum_interval():
    """The throttle is per unit time, not per count, so the client spaces itself."""
    now = [100.0]
    slept: list[float] = []

    source = AlpacaIvSource(
        _FlakyClient(403, fail_times=0),
        min_interval_seconds=1.2,
        sleep=slept.append,
        clock=lambda: now[0],
    )
    source.option_daily_bars(["A"], start="s", end="e")
    assert slept == []                   # first call is never paced
    now[0] = 100.3                       # only 0.3s elapsed
    source.option_daily_bars(["A"], start="s", end="e")
    assert slept and slept[-1] == pytest.approx(0.9, abs=1e-9)
    now[0] = 120.0                       # plenty of time elapsed
    source.option_daily_bars(["A"], start="s", end="e")
    assert len(slept) == 1               # nothing to wait for
