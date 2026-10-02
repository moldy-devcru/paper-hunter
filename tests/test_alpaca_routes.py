"""Path-shape pinning: the exact URL and query params each client method builds.

Incident (2026-10-02, first live soak run)
-------------------------------------------
``executor/alpaca_client.py`` called ``GET /v2/stocks/bars`` — the *multi-symbol* route —
and sent the symbol nowhere. Alpaca rejected it:

    HTTP 400 (https://data.alpaca.markets/v2/stocks/bars: Invalid format for parameter
    symbols: query parameter 'symbols' is required)

All 537 tests passed, because the mock transport was keyed by whatever path the
production code asked for. It was a mirror of our own mistakes, not of the API.

These tests are the fix for that class of bug. They assert the literal URL string and
the literal query-parameter set for **every** route the clients touch, so route drift
fails here instead of on a live run. If Alpaca moves an endpoint, the failure is a
one-line diff in this file with the doc URL next to it — not a 3am journal entry.

Doc references (verified 2026-10-02):

* stock bars (single symbol)  https://docs.alpaca.markets/us/reference/stockbarsingle-1
* stock bars (multi symbol)   https://docs.alpaca.markets/us/reference/stockbars
* option chain                https://docs.alpaca.markets/us/reference/optionchain
* get account                 https://docs.alpaca.markets/reference/getaccount-1
* get open positions          https://docs.alpaca.markets/reference/getopenpositions-1
* get clock                   https://docs.alpaca.markets/reference/getclock-1
* get orders                  https://docs.alpaca.markets/reference/getorders-1
"""

from __future__ import annotations

import pytest
from fixtures_synthetic import synthetic_daily_payload

from executor.alpaca_client import (
    ACCOUNT_PATH,
    CLOCK_PATH,
    DATA_BASE,
    DOC_VERIFIED_ROUTES,
    OPTIONS_SNAPSHOTS_PATH,
    PAPER_TRADING_BASE,
    POSITIONS_PATH,
    STOCK_BARS_PATH,
    AlpacaAPIError,
    AlpacaClient,
    MockTransport,
    match_doc_route,
    stock_bars_path,
)

JUNK_KEY = "TESTKEY-not-a-real-key"
JUNK_SECRET = "TESTSECRET-not-a-real-secret"  # noqa: S105 - deliberately fake

BARS_URL = "https://data.alpaca.markets/v2/stocks/SPY/bars"
CHAIN_URL = "https://data.alpaca.markets/v1beta1/options/snapshots/SPY"

EMPTY_BARS = {"bars": [], "symbol": "SPY", "next_page_token": None}
EMPTY_CHAIN = {"snapshots": {}, "next_page_token": None}
EMPTY_ACCOUNT = {
    "id": "x",
    "status": "ACTIVE",
    "currency": "USD",
    "cash": "1",
    "portfolio_value": "1",
    "equity": "1",
    "buying_power": "1",
    "pattern_day_trader": False,
    "trading_blocked": False,
}


def probe(routes: dict[str, object], call) -> tuple[str, dict]:
    """Run one client call against a strict mock; return the (url, params) it asked for."""
    transport = MockTransport(routes)
    client = AlpacaClient(transport=transport, key=JUNK_KEY, secret=JUNK_SECRET)
    call(client)
    assert transport.calls, "the call under test made no request at all"
    url, params = transport.calls[0]
    return url, dict(params)


# ---------------------------------------------------------------------------
# the routes themselves
# ---------------------------------------------------------------------------


def test_stock_bars_route_is_single_symbol_with_the_symbol_in_the_path():
    assert STOCK_BARS_PATH == "/v2/stocks/{symbol}/bars"
    assert stock_bars_path("SPY") == "/v2/stocks/SPY/bars"
    # The multi-symbol route is a *different* contract: it requires ?symbols=... .
    assert stock_bars_path("SPY") != "/v2/stocks/bars"


def test_option_chain_route_is_v1beta1_not_v2beta1():
    """There is no ``v2beta1`` in the Alpaca API reference at all."""
    assert OPTIONS_SNAPSHOTS_PATH == "/v1beta1/options/snapshots"
    assert "v2beta1" not in OPTIONS_SNAPSHOTS_PATH
    assert "v2beta1" not in STOCK_BARS_PATH


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("SPY", "/v2/stocks/SPY/bars"),
        ("aapl", "/v2/stocks/AAPL/bars"),
        # "." is an RFC3986 unreserved char, so quote() leaves it alone — correct, the
        # path is still valid. "/" is NOT, and would otherwise create a new path segment.
        ("BRK.B", "/v2/stocks/BRK.B/bars"),
        ("A/B", "/v2/stocks/A%2FB/bars"),
    ],
)
def test_symbol_is_url_quoted_into_the_path(symbol, expected):
    assert stock_bars_path(symbol) == expected


# ---------------------------------------------------------------------------
# per-method URL pinning
# ---------------------------------------------------------------------------


def test_get_daily_bars_builds_the_documented_single_symbol_url():
    url, params = probe(
        {stock_bars_path("SPY"): synthetic_daily_payload(count=3)},
        lambda c: c.get_daily_bars("SPY", feed="sip", limit=400),
    )
    assert url == BARS_URL
    assert params == {"timeframe": "1Day", "feed": "sip", "limit": 400, "adjustment": "all"}


def test_get_intraday_bars_uses_the_same_route_with_an_intraday_timeframe():
    url, params = probe(
        {stock_bars_path("SPY"): EMPTY_BARS},
        lambda c: c.get_intraday_bars("SPY", timeframe="5Min", feed="iex"),
    )
    assert url == BARS_URL
    assert params == {"timeframe": "5Min", "feed": "iex", "adjustment": "raw"}


def test_bars_request_never_sends_the_symbol_as_a_query_param():
    """The exact regression: ``symbol`` as a query param is a 400, not a fallback."""
    _, params = probe(
        {stock_bars_path("SPY"): EMPTY_BARS}, lambda c: c.get_daily_bars("SPY", feed="sip")
    )
    assert "symbol" not in params
    assert "symbols" not in params


def test_optional_bars_params_are_omitted_entirely_when_unset():
    _, params = probe(
        {stock_bars_path("SPY"): EMPTY_BARS},
        lambda c: c.get_bars("SPY", timeframe="1Min", feed="iex"),
    )
    assert set(params) == {"timeframe", "feed"}


def test_option_chain_page_builds_the_documented_v1beta1_url():
    url, params = probe(
        {f"{OPTIONS_SNAPSHOTS_PATH}/SPY": EMPTY_CHAIN}, lambda c: c.get_option_chain_page("SPY")
    )
    assert url == CHAIN_URL
    assert params == {"feed": "indicative", "limit": 100}


def test_option_chain_filters_use_the_documented_parameter_names():
    """``strike_gte``/``expiration_gte`` are plausible and wrong; Alpaca 400s on them."""
    _, params = probe(
        {f"{OPTIONS_SNAPSHOTS_PATH}/SPY": EMPTY_CHAIN},
        lambda c: c.get_option_chain_page(
            "SPY",
            strike_price_gte=600.0,
            strike_price_lte=700.0,
            expiration_date_gte="2026-10-01",
            expiration_date_lte="2026-12-31",
            right="call",
        ),
    )
    # MockTransport records the params as the client passed them (typing happens at the
    # transport boundary in UrllibTransport); what matters is the parameter NAME.
    assert params["strike_price_gte"] == 600.0
    assert params["expiration_date_gte"] == "2026-10-01"
    assert params["type"] == "call"
    assert "strike_gte" not in params
    assert "expiration_gte" not in params


def test_option_chain_paginates_on_the_same_url_with_a_page_token():
    transport = MockTransport({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": EMPTY_CHAIN})
    client = AlpacaClient(transport=transport, key=JUNK_KEY, secret=JUNK_SECRET)
    client.get_option_chain("SPY", max_pages=1)
    assert transport.calls[0][0] == CHAIN_URL
    assert "page_token" not in transport.calls[0][1]


def test_account_positions_and_clock_hit_the_paper_trading_base():
    url, _ = probe({ACCOUNT_PATH: EMPTY_ACCOUNT}, lambda c: c.get_account())
    assert url == f"{PAPER_TRADING_BASE}/v2/account"
    url, _ = probe({POSITIONS_PATH: []}, lambda c: c.get_positions())
    assert url == f"{PAPER_TRADING_BASE}/v2/positions"
    url, _ = probe(
        {CLOCK_PATH: {"timestamp": "2026-10-02T20:00:00Z", "is_open": True}},
        lambda c: c.get_clock(),
    )
    assert url == f"{PAPER_TRADING_BASE}/v2/clock"


# ---------------------------------------------------------------------------
# the mock is now a faithful model, not a mirror of our own mistakes
# ---------------------------------------------------------------------------


def test_mock_rejects_a_symbol_query_param_the_way_alpaca_does():
    """Replay the live bug offline: sending the symbol as a query param is a 400."""
    transport = MockTransport({stock_bars_path("SPY"): EMPTY_BARS})
    client = AlpacaClient(transport=transport, key=JUNK_KEY, secret=JUNK_SECRET)
    with pytest.raises(AlpacaAPIError) as exc:
        client._get(DATA_BASE, "/v2/stocks/bars", {"symbol": "SPY", "timeframe": "1Day"})
    assert exc.value.status in {400, 404}


def test_mock_rejects_undocumented_query_params_per_route():
    transport = MockTransport({f"{OPTIONS_SNAPSHOTS_PATH}/SPY": EMPTY_CHAIN})
    with pytest.raises(AlpacaAPIError) as exc:
        transport.get_json(
            CHAIN_URL,
            headers={},
            params={"feed": "indicative", "strike_gte": "600"},
        )
    assert exc.value.status == 400
    assert "strike_gte" in str(exc.value)


def test_mock_refuses_a_path_that_is_not_doc_verified():
    transport = MockTransport({"/v2/stocks/bars": EMPTY_BARS})
    with pytest.raises(AlpacaAPIError) as exc:
        transport.get_json("https://data.alpaca.markets/v2/stocks/bars", headers={}, params={})
    assert exc.value.status == 404


def test_every_pinned_route_resolves_in_the_doc_registry():
    for path in (
        stock_bars_path("SPY"),
        f"{OPTIONS_SNAPSHOTS_PATH}/SPY",
        ACCOUNT_PATH,
        POSITIONS_PATH,
        CLOCK_PATH,
    ):
        route = match_doc_route(path)
        assert route is not None, f"{path} is not in DOC_VERIFIED_ROUTES"
        assert route.doc_url.startswith("https://docs.alpaca.markets/")


def test_doc_registry_documents_bars_symbol_as_a_path_parameter():
    bars_route = match_doc_route(stock_bars_path("SPY"))
    assert bars_route is not None
    assert "{symbol}" in bars_route.path
    assert "symbol" not in bars_route.query_params


def test_the_order_client_routes_are_the_documented_ones():
    """``alpaca_trading`` is the other client. Its paths are pinned too — a route fix
    that only covers the data layer leaves half the bug surface open."""
    from executor.alpaca_trading import (
        ACCOUNT_PATH as T_ACCOUNT,
    )
    from executor.alpaca_trading import (
        ORDER_PATH,
        ORDERS_PATH,
        POSITION_PATH,
    )
    from executor.alpaca_trading import (
        POSITIONS_PATH as T_POSITIONS,
    )

    assert ORDERS_PATH == "/v2/orders"
    assert ORDER_PATH == "/v2/orders/{order_id}"
    assert POSITION_PATH == "/v2/positions/{symbol}"
    assert T_ACCOUNT == "/v2/account"
    assert T_POSITIONS == "/v2/positions"
    assert all(p.startswith("/v2/") for p in (ORDERS_PATH, ORDER_PATH, POSITION_PATH))
    # Symbol and order id are path segments on their routes, not query params.
    for path in (ORDER_PATH, POSITION_PATH):
        assert "{" in path and path.count("/") == 3


def test_doc_registry_has_no_duplicate_paths():
    paths = [r.path for r in DOC_VERIFIED_ROUTES]
    assert len(paths) == len(set(paths))
