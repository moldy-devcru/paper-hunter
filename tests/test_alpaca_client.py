"""Tests for the Alpaca data layer. Every one runs offline via ``MockTransport``.

The load-bearing property of this file is not "the parsing works" — it is **"no test
touches the network and no test reads a credential"**. That holds by construction: the
only client any test constructs has a ``MockTransport``, and the credential tests use
literal junk strings. If someone adds a test that calls ``from_env()`` it fails
immediately, because these tests never set the variables and never call it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fixtures_synthetic import load_fixture, synthetic_daily_payload

from executor.alpaca_client import (
    ACCOUNT_PATH,
    CLOCK_PATH,
    DATA_BASE,
    KEY_ENV,
    OPTIONS_SNAPSHOTS_PATH,
    PAPER_TRADING_BASE,
    POSITIONS_PATH,
    SECRET_ENV,
    AlpacaAPIError,
    AlpacaClient,
    AlpacaConfigError,
    AlpacaError,
    MockTransport,
    RateLimiter,
    RateLimitError,
    parse_occ_symbol,
    sip_end_is_queryable,
    stock_bars_path,
)

#: The concrete single-symbol bars path. Route pinning lives in test_alpaca_routes.py;
#: these tests care about parsing, not URLs.
BARS_PATH = stock_bars_path("SPY")

JUNK_KEY = "TESTKEY-not-a-real-key"
JUNK_SECRET = "TESTSECRET-not-a-real-secret"  # noqa: S105 - deliberately fake


def client_with(routes: dict, **kwargs) -> AlpacaClient:
    """Build a client on a fresh MockTransport over ``routes``."""
    return AlpacaClient(
        transport=MockTransport(routes),
        key=JUNK_KEY,
        secret=JUNK_SECRET,
        **kwargs,
    )


def client_and_transport(routes: dict, **kwargs) -> tuple[AlpacaClient, MockTransport]:
    """Same, but hands back the transport too so a test can inspect what was requested.

    One transport, shared by both return values — a test asserting on a *second*
    transport object would pass vacuously, which is exactly how a wrong-URL assertion
    can silently stop testing anything.
    """
    transport = MockTransport(routes)
    client = AlpacaClient(transport=transport, key=JUNK_KEY, secret=JUNK_SECRET, **kwargs)
    return client, transport


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_client_from_env_reads_the_documented_variables(monkeypatch):
    monkeypatch.setenv(KEY_ENV, JUNK_KEY)
    monkeypatch.setenv(SECRET_ENV, JUNK_SECRET)
    client = AlpacaClient.from_env()
    assert client.headers["APCA-API-KEY-ID"] == JUNK_KEY
    assert client.headers["APCA-API-SECRET-KEY"] == JUNK_SECRET


def test_missing_credentials_name_the_variable_not_the_value(monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(AlpacaConfigError) as exc:
        AlpacaClient.from_env()
    message = str(exc.value)
    assert KEY_ENV in message and SECRET_ENV in message
    assert "paper keys only" in message


def test_repr_never_leaks_the_key(monkeypatch):
    """A repr ends up in tracebacks and pytest output, so it must be credential-free."""
    monkeypatch.setenv(KEY_ENV, "super-secret-key-value")
    monkeypatch.setenv(SECRET_ENV, "super-secret-secret-value")
    client = AlpacaClient.from_env()
    rendered = f"{client!r} {client!s}"
    assert "configured" in rendered
    assert "super-secret" not in rendered


def test_api_error_does_not_attach_headers(monkeypatch):
    transport = MockTransport({})
    client = AlpacaClient(transport=transport, key=JUNK_KEY, secret=JUNK_SECRET)
    with pytest.raises(AlpacaAPIError) as exc:
        client.get_clock()
    assert "TESTKEY" not in str(exc.value)
    assert "TESTSECRET" not in str(exc.value)
    assert exc.value.status == 404


def test_headers_require_credentials():
    client = AlpacaClient(transport=MockTransport({}), key="", secret="")
    with pytest.raises(AlpacaConfigError):
        _ = client.headers


# ---------------------------------------------------------------------------
# base urls / routes
# ---------------------------------------------------------------------------


def test_base_urls_are_paper_trading_and_data(monkeypatch):
    monkeypatch.setenv(KEY_ENV, JUNK_KEY)
    monkeypatch.setenv(SECRET_ENV, JUNK_SECRET)
    client = AlpacaClient.from_env()
    assert client.trading_base == "https://paper-api.alpaca.markets"
    assert client.data_base == "https://data.alpaca.markets"
    assert PAPER_TRADING_BASE == "https://paper-api.alpaca.markets"
    assert DATA_BASE == "https://data.alpaca.markets"


def test_bars_hit_the_single_symbol_bars_route_with_the_feed_param():
    client, transport = client_and_transport({BARS_PATH: load_fixture("stock_bars.json")})
    series = client.get_daily_bars("SPY", feed="sip", start="2025-12-16", end="2025-12-18")
    url, params = transport.calls[0]
    assert url == "https://data.alpaca.markets/v2/stocks/SPY/bars"
    assert params["timeframe"] == "1Day"
    assert params["feed"] == "sip"
    assert "symbol" not in params
    assert len(series) == 3


def test_intraday_bars_use_the_same_route_with_an_intraday_timeframe():
    client, transport = client_and_transport({BARS_PATH: {"bars": [], "next_page_token": None}})
    series = client.get_intraday_bars("SPY", timeframe="5Min", feed="iex")
    _, params = transport.calls[0]
    assert params["timeframe"] == "5Min"
    assert params["feed"] == "iex"
    assert series.timeframe == "5Min"


# ---------------------------------------------------------------------------
# bars parsing
# ---------------------------------------------------------------------------


def test_bars_parse_to_typed_objects_oldest_first():
    client = client_with({BARS_PATH: load_fixture("stock_bars.json")})
    series = client.get_daily_bars("SPY", feed="sip")
    assert series.closes == [679.88, 681.95, 678.44]
    assert series.bars[0].t == dt.datetime(2025, 12, 16, 5, 0, tzinfo=dt.UTC)
    assert series.bars[0].h == 680.35
    assert series.bars[-1].v == pytest.approx(66_902_112.0)
    assert series.bars[0].n == 402118
    assert series.bars[0].vw == pytest.approx(678.91)


def test_bars_are_sorted_even_if_the_api_returns_them_newest_first():
    raw = load_fixture("stock_bars.json")
    raw["bars"] = list(reversed(raw["bars"]))
    client = client_with({BARS_PATH: raw})
    series = client.get_daily_bars("SPY", feed="sip")
    assert [b.t for b in series.bars] == sorted(b.t for b in series.bars)
    # The fixture's closes are 679.88, 681.95, 678.44 in date order — deliberately not
    # monotonic, so a "sorted by close" mistake cannot masquerade as a date sort.
    assert series.closes == [679.88, 681.95, 678.44]


def test_malformed_bars_payload_is_an_error_not_an_empty_series():
    """An empty ``bars: {}`` for the requested symbol is a legitimate "no data"; a
    payload with no ``bars`` object at all is a shape error and must not be read as
    "zero bars, indicators undefined"."""
    client = client_with({BARS_PATH: {"next_page_token": None}})
    with pytest.raises(AlpacaError, match="bars payload"):
        client.get_daily_bars("SPY", feed="sip")


def test_bars_payload_with_no_data_for_the_symbol_is_an_empty_series():
    client = client_with({BARS_PATH: {"bars": [], "symbol": "SPY", "next_page_token": None}})
    series = client.get_daily_bars("SPY", feed="sip")
    assert len(series) == 0
    assert series.closes == []


def test_bar_without_a_timestamp_is_rejected():
    client = client_with({BARS_PATH: {"bars": [{"c": "1.0"}], "next_page_token": None}})
    with pytest.raises(AlpacaError, match="timestamp"):
        client.get_daily_bars("SPY", feed="sip")


# ---------------------------------------------------------------------------
# options chain
# ---------------------------------------------------------------------------


def test_option_chain_parses_the_documented_nested_envelope():
    client = client_with({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": load_fixture("option_chain.json")})
    chain = client.get_option_chain_page("SPY", feed="indicative")
    assert len(chain) == 8
    assert chain.feed == "indicative"
    assert chain.underlying == "SPY"
    assert set(chain.expiries()) == {"20251220", "20260117"}
    assert len(chain.calls()) == 5
    assert len(chain.puts()) == 3
    assert chain.strikes() == [680.0, 685.0, 690.0, 700.0]


def test_option_chain_defaults_to_the_indicative_feed_on_the_free_tier():
    route = {f"{OPTIONS_SNAPSHOTS_PATH}/SPY": load_fixture("option_chain.json")}
    client, transport = client_and_transport(route)
    client.get_option_chain_page("SPY")
    _, params = transport.calls[0]
    assert params["feed"] == "indicative"
    assert transport.calls[0][0].startswith(
        "https://data.alpaca.markets/v1beta1/options/snapshots/"
    )


def test_contract_parses_greeks_iv_quote_and_bars():
    client = client_with({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": load_fixture("option_chain.json")})
    chain = client.get_option_chain_page("SPY")
    call = next(c for c in chain if c.symbol == "SPY251220C00685000")
    assert (call.underlying, call.expiry, call.strike, call.right) == (
        "SPY",
        "20251220",
        685.0,
        "call",
    )
    assert call.implied_volatility == pytest.approx(0.1710488)
    assert call.greeks.delta == pytest.approx(0.1984)
    assert call.greeks.theta == pytest.approx(-0.0963)
    assert call.greeks.vega == pytest.approx(0.1031)
    assert call.latest_quote.bid == pytest.approx(1.80)
    assert call.latest_quote.ask == pytest.approx(1.92)
    assert call.latest_quote.mid == pytest.approx(1.86)
    assert call.latest_trade.p == pytest.approx(1.85)
    assert call.daily_bar is not None and call.daily_bar.c == pytest.approx(1.85)
    assert call.prev_daily_bar is not None and call.prev_daily_bar.c == pytest.approx(1.75)
    assert call.minute_bar is not None


def test_puts_carry_negative_deltas():
    client = client_with({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": load_fixture("option_chain.json")})
    chain = client.get_option_chain_page("SPY")
    put = next(c for c in chain if c.symbol == "SPY251220P00685000")
    assert put.right == "put"
    assert put.greeks.delta == pytest.approx(-0.7915)
    assert put.greeks.rho == pytest.approx(-0.1372)


def test_atm_contract_is_the_nearest_strike_and_prefers_calls():
    client = client_with({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": load_fixture("option_chain.json")})
    chain = client.get_option_chain_page("SPY")
    atm = chain.atm_contract(680.0, "20251220")
    assert atm is not None and atm.strike == 680.0 and atm.right == "call"
    # A tie between 680 and 685 at spot 682.5 resolves to the call side, deterministically.
    tied = chain.atm_contract(682.5, "20251220")
    assert tied is not None and tied.right == "call"


def test_chain_pagination_follows_the_token_and_dedupes():
    # The documented envelope keys ``snapshots`` by OCC contract symbol (optionchain).
    page1 = {
        "snapshots": {"SPY251220C00680000": _contract(680.0, "call")},
        "next_page_token": "tok-1",
    }
    page2 = {
        "snapshots": {
            "SPY251220C00680000": _contract(680.0, "call"),  # overlap, must dedupe
            "SPY251220C00685000": _contract(685.0, "call"),
        },
        "next_page_token": None,
    }

    def route(params):
        return page2 if params.get("page_token") == "tok-1" else page1

    client, transport = client_and_transport({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": route})
    chain = client.get_option_chain("SPY")
    assert [c.symbol for c in chain] == [
        "SPY251220C00680000",
        "SPY251220C00685000",
    ]
    assert chain.next_page_token is None
    assert len(transport.calls) == 2


def test_pagination_stops_at_max_pages_and_leaves_the_token_set():
    """An unbounded page loop against a paged API is how a data layer drains a wallet."""
    always_more = {
        "snapshots": {"SPY251220C00680000": _contract(680.0, "call")},
        "next_page_token": "never-done",
    }
    client, transport = client_and_transport({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": always_more})
    chain = client.get_option_chain("SPY", max_pages=3)
    assert len(transport.calls) == 3
    assert chain.next_page_token == "never-done"


def _contract(strike: float, right: str) -> dict:
    """One snapshot, keyed shape. The OCC symbol is the dict key, not a field."""
    return {
        "impliedVolatility": "0.17",
        "greeks": {
            "delta": "0.4",
            "gamma": "0.02",
            "theta": "-0.09",
            "vega": "0.10",
            "rho": "0.05",
        },
        "latestQuote": {
            "ap": "1.00",
            "as": 10,
            "bp": "0.95",
            "bs": 10,
            "t": "2025-12-18T19:59:58Z",
        },
        "latestTrade": {"p": "0.97", "s": 5, "t": "2025-12-18T19:59:59Z"},
        "dailyBar": {
            "t": "2025-12-18T05:00:00Z",
            "o": "0.9",
            "h": "1.0",
            "l": "0.9",
            "c": "0.97",
            "v": "100",
            "n": 5,
            "vw": "0.97",
        },
    }


def test_chain_payload_without_the_snapshots_object_is_an_error():
    client = client_with({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": {"next_page_token": None}})
    with pytest.raises(AlpacaError, match="snapshots"):
        client.get_option_chain_page("SPY")


def test_chain_symbol_comes_from_the_envelope_key_not_a_field():
    """Alpaca keys ``snapshots`` by OCC symbol; the symbol is not inside the snapshot.
    Taking it from the key is what stops a chain from being silently mislabelled."""
    client = client_with(
        {
            f"{OPTIONS_SNAPSHOTS_PATH}/SPY": {
                "snapshots": {"SPY251220C00685000": _contract(685.0, "call")},
                "next_page_token": None,
            }
        }
    )
    chain = client.get_option_chain_page("SPY")
    assert [c.symbol for c in chain] == ["SPY251220C00685000"]
    only = next(iter(chain))
    assert only.strike == 685.0
    assert only.right == "call"


def test_malformed_contract_symbol_is_rejected_loudly():
    client = client_with(
        {f"{OPTIONS_SNAPSHOTS_PATH}/SPY": {"snapshots": {"SPYXX": {}}, "next_page_token": None}}
    )
    with pytest.raises(AlpacaError, match="OCC"):
        client.get_option_chain_page("SPY")


# ---------------------------------------------------------------------------
# OCC symbol parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("SPY260116C00650000", ("SPY", "20260116", 650.0, "call")),
        ("SPY260116P00650000", ("SPY", "20260116", 650.0, "put")),
        ("QQQ251220C00400000", ("QQQ", "20251220", 400.0, "call")),
        ("BRKB251220C01250000", ("BRKB", "20251220", 1250.0, "call")),
    ],
)
def test_occ_symbol_parsing(symbol, expected):
    assert parse_occ_symbol(symbol) == expected


def test_occ_symbol_parsing_rejects_junk():
    for bad in ("", "SPY", "SPY260116X00650000", "SPY261345C00650000"):
        with pytest.raises(AlpacaError):
            parse_occ_symbol(bad)


# ---------------------------------------------------------------------------
# paper account / positions / clock
# ---------------------------------------------------------------------------


def test_account_parses_the_documented_fields():
    client = client_with({ACCOUNT_PATH: load_fixture("account.json")})
    account = client.get_account()
    assert account.status == "ACTIVE"
    assert account.currency == "USD"
    assert account.cash == pytest.approx(8412.55)
    assert account.portfolio_value == pytest.approx(10000.0)
    assert account.buying_power == pytest.approx(33650.20)
    assert account.pattern_day_trader is False
    assert account.trading_blocked is False


def test_positions_parse_including_option_asset_class():
    client = client_with({POSITIONS_PATH: load_fixture("positions.json")})
    positions = client.get_positions()
    assert len(positions) == 2
    option = next(p for p in positions if p.asset_class == "us_equity_option")
    assert option.symbol == "SPY260117C00700000"
    assert option.qty == pytest.approx(1.0)
    assert option.side == "long"
    assert option.unrealized_pl == pytest.approx(2.90)
    equity = next(p for p in positions if p.asset_class == "us_equity")
    assert equity.avg_entry_price == pytest.approx(670.0)


def test_clock_reports_market_state():
    client = client_with({CLOCK_PATH: load_fixture("clock.json")})
    clock = client.get_clock()
    assert clock.is_open is True
    assert clock.timestamp.replace(microsecond=0) == dt.datetime(
        2025, 12, 18, 20, 31, 12, tzinfo=dt.UTC
    )
    assert clock.next_close == dt.datetime(2025, 12, 18, 21, 0, tzinfo=dt.UTC)
    assert clock.next_open == dt.datetime(2025, 12, 19, 14, 30, tzinfo=dt.UTC)


def test_account_calls_hit_the_paper_trading_base():
    client, transport = client_and_transport({ACCOUNT_PATH: load_fixture("account.json")})
    client.get_account()
    assert transport.calls[0][0] == "https://paper-api.alpaca.markets/v2/account"


# ---------------------------------------------------------------------------
# no order placement exists (Phase 3a scope guard)
# ---------------------------------------------------------------------------


def test_client_cannot_place_orders():
    """Phase 3b owns order placement. Assert the absence so it can't happen quietly."""
    forbidden = [
        name
        for name in dir(AlpacaClient)
        if any(
            verb in name
            for verb in ("order", "submit", "place", "cancel", "replace", "close_position")
        )
    ]
    assert forbidden == []


# ---------------------------------------------------------------------------
# rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_up_to_the_cap_then_waits():
    now = [0.0]
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(
        max_calls=3,
        window_seconds=60.0,
        sleep=fake_sleep,
        clock=lambda: now[0],
    )
    for _ in range(3):
        limiter.acquire()
    assert slept == []
    limiter.acquire()
    assert slept == [60.0]
    assert now[0] == pytest.approx(60.0)


def test_rate_limiter_strict_when_blocking_is_off():
    limiter = RateLimiter(max_calls=1, window_seconds=60.0, clock=lambda: 0.0)
    limiter.acquire()
    with pytest.raises(RateLimitError, match="rate limit"):
        limiter.acquire(blocking=False)


def test_rate_limiter_forgets_stale_calls():
    now = [0.0]
    limiter = RateLimiter(
        max_calls=2, window_seconds=10.0, sleep=lambda s: None, clock=lambda: now[0]
    )
    limiter.acquire()
    limiter.acquire()
    now[0] = 11.0
    limiter.acquire()  # the earlier two have aged out; this must not raise or sleep


def test_client_applies_the_rate_limiter_per_request():
    calls: list[float] = []
    limiter = RateLimiter(
        max_calls=1,
        window_seconds=5.0,
        sleep=lambda s: calls.append(s),
        clock=lambda: 0.0,
    )
    client = client_with(
        {ACCOUNT_PATH: load_fixture("account.json"), CLOCK_PATH: load_fixture("clock.json")},
        rate_limiter=limiter,
    )
    client.get_account()
    client.get_clock()
    assert calls == [5.0]


def test_default_rate_limit_matches_the_documented_basic_plan():
    from executor.alpaca_client import RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW_SECONDS

    assert RATE_LIMIT_CALLS == 200
    assert RATE_LIMIT_WINDOW_SECONDS == 60.0


# ---------------------------------------------------------------------------
# free-tier SIP 15-minute rule
# ---------------------------------------------------------------------------


def test_sip_history_is_only_queryable_with_an_end_older_than_15_minutes():
    now = dt.datetime(2025, 12, 18, 21, 0, tzinfo=dt.UTC)
    assert sip_end_is_queryable(now - dt.timedelta(minutes=20), now=now) is True
    assert sip_end_is_queryable(now - dt.timedelta(minutes=15), now=now) is True
    assert sip_end_is_queryable(now - dt.timedelta(minutes=14), now=now) is False
    assert sip_end_is_queryable(now, now=now) is False
    # A missing end defaults to "now", which is inside the subscription window.
    assert sip_end_is_queryable(None, now=now) is False


# ---------------------------------------------------------------------------
# no-network guarantee
# ---------------------------------------------------------------------------


def test_no_credential_env_var_is_set_during_the_suite(monkeypatch):
    """Explicit tripwire: if a prior test leaked env state, this fails loudly."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    assert AlpacaClient(transport=MockTransport({})).key == ""
    with pytest.raises(AlpacaConfigError):
        AlpacaClient.from_env()


def test_mock_transport_records_calls_without_a_socket():
    client, transport = client_and_transport({BARS_PATH: synthetic_daily_payload(count=5)})
    client.get_daily_bars("SPY", feed="iex")
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "https://data.alpaca.markets/v2/stocks/SPY/bars"
    assert transport.calls[0][1]["feed"] == "iex"
    assert json.dumps(dict(transport.calls[0][1]))  # params are plain JSON-safe scalars
