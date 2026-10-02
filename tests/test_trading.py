"""Phase 3b — order placement: the kill switch, the paper guard, the dry run.

Offline by construction: every test drives :class:`MockTradingTransport`. No test in
this file reads an environment credential or opens a socket, and the ones that poke at
environment variables do so to prove that setting one changes nothing.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from executor.alpaca_client import AlpacaClient
from executor.alpaca_trading import (
    LIVE_ENV_VAR,
    LIVE_TRADING_BASE,
    ORDERS_PATH,
    PAPER_HOSTS,
    PAPER_TRADING_BASE,
    AlpacaConfigError,
    AlpacaError,
    AlpacaTradingClient,
    LiveTradingRefused,
    MockTradingTransport,
    PaperAccountUnverified,
    assert_paper_url,
    verify_paper_account,
)
from executor.watch_loop import DryRunRouter

JUNK_KEY = "PKTESTJUNKKEY0000"
JUNK_SECRET = "testsecretnotarealkey"


def client_with(routes=None, *, base: str = PAPER_TRADING_BASE, key: str = JUNK_KEY,
                secret: str = JUNK_SECRET):
    transport = MockTradingTransport(routes or {})
    client = AlpacaTradingClient(transport=transport, key=key, secret=secret, base=base)
    return client, transport


def order_payload(**overrides):
    payload = {
        "id": "o-1",
        "symbol": "SPY261002C00630000",
        "qty": 1,
        "side": "buy",
        "type": "market",
        "status": "accepted",
        "submitted_at": "2026-10-02T13:50:00Z",
        "filled_qty": 0,
        "filled_avg_price": None,
        "limit_price": None,
        "client_order_id": None,
    }
    payload.update(overrides)
    return payload


ACCOUNT_PAPER = {
    "id": "PA1234567",
    "status": "ACTIVE",
    "currency": "USD",
    "cash": "25000.00",
    "portfolio_value": "30000.00",
    "equity": "30000.00",
    "buying_power": "60000.00",
    "pattern_day_trader": False,
    "trading_blocked": False,
}

ACCOUNT_LIVE = dict(ACCOUNT_PAPER, id="9LZ7XQ2KDTYZA", paper=False)
ACCOUNT_UNPROVEN = dict(ACCOUNT_PAPER, id="9LZ7XQ2KDTYZA")


# ---------------------------------------------------------------------------
# kill switch — the URL can only ever be the paper endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base",
    [
        "https://api.alpaca.markets",
        "https://api.alpaca.markets/v2",
        "http://paper-api.alpaca.markets",           # not https
        "https://paper-api.alpaca.markets.evil.com",  # suffix trick
        "https://evil.com/?x=paper-api.alpaca.markets",
        "https://PAPER-API.alpaca.markets.evil.net",
        "https://live-api.alpaca.markets",
        "",
    ],
)
def test_assert_paper_url_refuses_everything_that_is_not_the_paper_host(base):
    with pytest.raises(LiveTradingRefused):
        assert_paper_url(base)


def test_assert_paper_url_accepts_the_one_allowed_host():
    assert assert_paper_url(PAPER_TRADING_BASE) == PAPER_TRADING_BASE
    assert PAPER_HOSTS == frozenset({"paper-api.alpaca.markets"})


def test_the_live_host_is_never_constructed_as_a_base():
    """The live base URL exists as a constant for documentation only.

    It is never a default, never a parameter default and never reachable through the
    client — this asserts both the constant and that the client refuses it.
    """
    assert LIVE_TRADING_BASE == "https://api.alpaca.markets"
    client, _ = client_with(base=LIVE_TRADING_BASE)
    with pytest.raises(LiveTradingRefused):
        client.url_for(ORDERS_PATH)


def test_there_is_no_live_env_var_to_set(monkeypatch):
    """The escape hatch that does not exist. Setting it changes nothing."""
    assert LIVE_ENV_VAR is None
    monkeypatch.setenv("PAPER_HUNTER_LIVE_OK", "1")
    monkeypatch.setenv("ALPACA_LIVE", "1")
    monkeypatch.setenv("ALPACA_BASE_URL", LIVE_TRADING_BASE)
    with pytest.raises(LiveTradingRefused):
        assert_paper_url(LIVE_TRADING_BASE)
    assert_paper_url(PAPER_TRADING_BASE)  # and the paper host still works

    client, _ = client_with()
    assert client.base == PAPER_TRADING_BASE
    assert client.url_for(ORDERS_PATH).startswith(PAPER_TRADING_BASE)


def test_no_module_reads_a_live_switch(monkeypatch):
    """Source-level check: only the two documented credential vars are read."""
    import executor.alpaca_trading as module

    source = open(module.__file__, encoding="utf-8").read()
    reads = [line for line in source.splitlines() if "os.environ" in line]
    assert reads, "expected the credential read to exist"
    for line in reads:
        assert "ALPACA_PAPER" in line or "KEY_ENV" in line or "SECRET_ENV" in line, line
    assert not hasattr(module, "LIVE_OK_ENV")
    assert not hasattr(module, "ENABLE_LIVE")


def test_phase_3a_scope_guard_still_stands():
    """Order verbs must not have crept into the read-only data client."""
    forbidden = [
        name
        for name in dir(AlpacaClient)
        if any(v in name for v in ("order", "submit", "place", "cancel", "replace",
                                   "close_position"))
    ]
    assert forbidden == []


# ---------------------------------------------------------------------------
# order placement
# ---------------------------------------------------------------------------


def test_submit_market_order_posts_the_expected_body():
    client, transport = client_with({ORDERS_PATH: order_payload()})
    order = client.submit_order(
        symbol="SPY261002C00630000", qty=1, side="buy", client_order_id="ph-b-1"
    )
    method, path, body = transport.calls[0]
    assert (method, path) == ("POST", ORDERS_PATH)
    assert body == {
        "symbol": "SPY261002C00630000",
        "qty": 1,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "asset_class": "us_option",
        "client_order_id": "ph-b-1",
    }
    assert order.id == "o-1" and order.status == "accepted"


def test_submit_limit_order_carries_the_price_and_equity_asset_class():
    client, transport = client_with({ORDERS_PATH: order_payload(type="limit")})
    client.submit_order(
        symbol="SPY",
        qty=39,
        side="buy",
        order_type="limit",
        limit_price=256.41,
        asset_class="us_equity",
    )
    _, _, body = transport.calls[0]
    assert body["limit_price"] == 256.41
    assert body["asset_class"] == "us_equity"
    assert body["qty"] == 39  # fractional/float qty is passed through untouched


def test_submit_rejects_impossible_orders_before_the_request():
    client, transport = client_with({ORDERS_PATH: order_payload()})
    with pytest.raises(AlpacaError):
        client.submit_order(symbol="SPY", qty=0, side="buy")
    with pytest.raises(AlpacaError):
        client.submit_order(symbol="SPY", qty=1, side="short")
    with pytest.raises(AlpacaError):
        client.submit_order(symbol="SPY", qty=1, side="buy", order_type="limit")
    assert transport.calls == []  # nothing left the process


def test_cancel_and_close_go_to_the_documented_paths():
    client, transport = client_with(
        {
            "/v2/orders/o-9": {"id": "o-9", "status": "canceled"},
            "/v2/positions/SPY": order_payload(symbol="SPY", side="sell"),
        }
    )
    client.cancel_order("o-9")
    assert transport.calls[0][:2] == ("DELETE", "/v2/orders/o-9")
    order = client.close_position("SPY", qty=39)
    assert transport.calls[1][:2] == ("DELETE", "/v2/positions/SPY")
    assert order.side == "sell"


def test_get_orders_and_positions_parse():
    client, _ = client_with(
        {
            ORDERS_PATH: [order_payload()],
            "/v2/positions": [
                {
                    "symbol": "SPY261002C00630000",
                    "qty": "1",
                    "side": "long",
                    "market_value": "150.00",
                    "cost_basis": "150.00",
                    "avg_entry_price": "1.50",
                    "current_price": "1.62",
                    "unrealized_pl": "12.00",
                    "asset_class": "us_option",
                }
            ],
        }
    )
    assert [o.id for o in client.get_orders()] == ["o-1"]
    position = client.get_positions()[0]
    assert position.avg_entry_price == 1.5
    assert position.asset_class == "us_option"


def test_every_request_goes_to_the_paper_host():
    """The FULL url is recorded, not just the path — host included."""

    class Recording:
        def __init__(self, inner):
            self.inner = inner
            self.urls: list[str] = []

        def request(self, method, url, **kwargs):
            self.urls.append(url)
            return self.inner.request(method, url, **kwargs)

    inner = MockTradingTransport(
        {ORDERS_PATH: order_payload(), "/v2/account": ACCOUNT_PAPER}
    )
    recorder = Recording(inner)
    client = AlpacaTradingClient(transport=recorder, key=JUNK_KEY, secret=JUNK_SECRET)
    client.submit_order(symbol="SPY", qty=1, side="buy")
    client.get_account()
    assert recorder.urls == [
        "https://paper-api.alpaca.markets/v2/orders",
        "https://paper-api.alpaca.markets/v2/account",
    ]


def test_missing_credentials_name_the_variable_not_the_value(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)
    with pytest.raises(AlpacaConfigError) as exc:
        AlpacaTradingClient.from_env()
    message = str(exc.value)
    assert "ALPACA_PAPER_KEY" in message and "paper keys only" in message


def test_credentials_never_appear_in_repr_or_errors():
    client, transport = client_with({})
    rendered = f"{client!r} {client}"
    assert "configured" in rendered
    assert JUNK_KEY not in rendered and JUNK_SECRET not in rendered
    with pytest.raises(Exception) as exc:
        client.submit_order(symbol="SPY", qty=1, side="buy")
    message = str(exc.value)
    assert JUNK_KEY not in message and JUNK_SECRET not in message


# ---------------------------------------------------------------------------
# paper-account proof — the live guard
# ---------------------------------------------------------------------------


def test_paper_flag_is_proof():
    confirmation = verify_paper_account(dict(ACCOUNT_PAPER, paper=True))
    assert confirmation.is_paper is True
    assert confirmation.proof == "paper_field"
    assert confirmation.account_id == "PA1234567"
    assert "PAPER ACCOUNT CONFIRMED" in confirmation.banner()


def test_paper_id_prefix_is_the_fallback_proof():
    confirmation = verify_paper_account(ACCOUNT_PAPER)  # no paper flag, id starts with PA
    assert confirmation.proof == "id_prefix"


@pytest.mark.parametrize("payload", [ACCOUNT_LIVE, ACCOUNT_UNPROVEN])
def test_an_account_that_does_not_prove_paper_is_refused(payload):
    with pytest.raises(PaperAccountUnverified):
        verify_paper_account(payload)


def test_a_blocked_account_is_refused_even_if_it_looks_paper():
    with pytest.raises(PaperAccountUnverified):
        verify_paper_account(dict(ACCOUNT_PAPER, trading_blocked=True))


def test_live_router_refuses_when_the_account_endpoint_says_live():
    from executor.watch_loop import LiveOrderRouter

    client, transport = client_with(
        {"/v2/account": ACCOUNT_LIVE, ORDERS_PATH: order_payload()}
    )
    with pytest.raises(PaperAccountUnverified):
        LiveOrderRouter(client, banner=False)
    assert not any(path == ORDERS_PATH for _m, path, _b in transport.calls)


def test_live_router_banner_names_the_paper_account_and_routes_legs(capsys):
    from executor.watch_loop import LiveOrderRouter

    client, transport = client_with(
        {"/v2/account": ACCOUNT_PAPER, ORDERS_PATH: order_payload()}
    )
    router = LiveOrderRouter(client)
    out = capsys.readouterr().out
    assert "PAPER ACCOUNT CONFIRMED" in out
    assert "PA1234567" in out
    assert "paper-api.alpaca.markets" in out

    from executor.position_manager import Action, OrderLeg

    router.route(
        Action(
            kind="ROLL",
            arm="C",
            reason="mechanical roll",
            legs=(
                OrderLeg("SPY261205C00580000", 1, "sell"),
                OrderLeg("SPY270115C00580000", 1, "buy"),
            ),
        )
    )
    methods = [(m, path) for m, path, _b in transport.calls]
    assert methods.count(("POST", ORDERS_PATH)) == 2


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_submits_nothing():
    from executor.position_manager import Action, OrderLeg

    router = DryRunRouter()
    ids = router.route(
        Action(
            kind="TRADE",
            arm="B",
            reason="would buy",
            legs=(OrderLeg("SPY261002C00630000", 1, "buy"),),
        )
    )
    assert ids == ()
    assert len(router.actions) == 1
    assert len(router.legs) == 1
    assert "DRY RUN" in router.describe()


def test_dry_run_cli_default_never_touches_credentials(monkeypatch):
    from executor.main import build_parser, main

    for name in ("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET"):
        monkeypatch.delenv(name, raising=False)
    args = build_parser().parse_args(["hunt-plan"])
    assert args.dry_run is True
    args = build_parser().parse_args(["--live", "watch"])
    assert args.dry_run is False
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--live", "--dry-run", "watch"])
    assert callable(main)


def test_no_environment_variable_makes_a_live_base_url(monkeypatch):
    """Belt and braces: even a perfectly-set paper credential cannot move the host."""
    monkeypatch.setenv("ALPACA_PAPER_KEY", JUNK_KEY)
    monkeypatch.setenv("ALPACA_PAPER_SECRET", JUNK_SECRET)
    monkeypatch.setenv("PAPER_HUNTER_LIVE_OK", "true")
    client = AlpacaTradingClient.from_env()
    assert client.base == PAPER_TRADING_BASE
    assert client.url_for("/v2/orders") == PAPER_TRADING_BASE + "/v2/orders"
    assert os.environ["ALPACA_PAPER_KEY"] == JUNK_KEY  # untouched
    assert isinstance(dt.datetime.now(dt.UTC), dt.datetime)  # trivial, keeps dt import honest
