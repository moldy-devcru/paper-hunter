"""Paper-trading order client — Phase 3b. PAPER ONLY, kill-switched by construction.

Why a separate module from ``executor/alpaca_client.py``
--------------------------------------------------------
Phase 3a shipped ``alpaca_client`` as a deliberately *incapable* data layer, and
``tests/test_alpaca_client.py::test_client_cannot_place_orders`` asserts that no
method on ``AlpacaClient`` contains an order verb. Order placement therefore lives
here, in its own module, and that assertion keeps standing — the data layer stays
unable to place an order and the trading layer is the only door to the order API.

The kill switch
---------------
Three independent layers, because this is the one place in the repository where a bug
costs real money rather than a journal row:

1. **Host allowlist.** :func:`assert_paper_url` accepts exactly one host,
   ``paper-api.alpaca.markets``. The live host is not "a flag we default off" — it is
   not constructible. Any URL whose host is not the paper host raises
   :class:`LiveTradingRefused` at construction time, before a request is built.
   The check is *exact host equality*, not substring matching, because the live host
   (``api.alpaca.markets``) is a strict substring of the paper host's reverse-DNS-ish
   sibling set; ``"api.alpaca.markets" in "paper-api.alpaca.markets"`` is True and a
   ``not in`` check would therefore have been meaningless in the permissive direction.
2. **No environment escape hatch.** There is no ``PAPER_HUNTER_LIVE_OK``, no
   ``--live-url`` flag, no config key, and nothing in this module reads an
   environment variable other than the two documented paper credentials
   (:func:`assert_paper_url` takes its answer from its argument, never from the
   environment). A live URL cannot be enabled by setting a variable, because no such
   variable is read. ``tests/test_trading.py`` asserts the constant
   ``LIVE_ENV_VAR`` does not exist and that populating a plausible name changes
   nothing.
3. **Account proof before the first order.** :meth:`AlpacaTradingClient.assert_paper_account`
   reads ``/v2/account`` and refuses unless the account proves it is a paper account
   (:func:`verify_paper_account`). Unproven is treated as *not paper*: the safe
   direction when the evidence is ambiguous is to stop.

Authentication
--------------
``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET``, read from the environment exactly as
the data client does. Keys live only in request headers. ``__repr__`` says
"configured"/"unconfigured"; no exception message, no log line and no journal payload
in this module ever carries a header value.

Offline tests
-------------
``TradingTransport`` is the single seam. ``MockTradingTransport`` records every
``(method, path, body)`` and answers from canned routes; a request with no matching
route raises ``AlpacaAPIError(404)`` exactly like the real API, so a wrong path fails
loudly in tests instead of silently 404-ing against a live account. No test in this
repository performs a network call.

# INTERPRETATION: paper proof is ``account.paper is True`` OR an account id beginning
# ``PA`` (the prefix Alpaca issues paper accounts). Alpaca does not put a
# ``"paper": true`` flag on every account payload, so an id-prefix fallback exists —
# but it is a *fallback*, and an account that matches neither is refused. Guessing
# "live" on absent evidence is how a paper-only experiment becomes a real-money one.

# INTERPRETATION: market orders are the default ``time_in_force``. Alpaca rejects
# ``DAY`` market orders after 16:00 ET, and the brief's hard-close is 15:30 ET, so DAY
# is correct for every order this executor places. ``limit`` orders also default to
# DAY because every limit order here is a mechanical exit, not a resting entry.

Python 3.12+, stdlib only (``urllib``) — same rule as executor/alpaca_client.py.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from executor.alpaca_client import (
    KEY_ENV,
    SECRET_ENV,
    Account,
    AlpacaAPIError,
    AlpacaConfigError,
    AlpacaError,
    Position,
)

# ---------------------------------------------------------------------------
# endpoints / constants
# ---------------------------------------------------------------------------

PAPER_TRADING_BASE = "https://paper-api.alpaca.markets"
LIVE_TRADING_BASE = "https://api.alpaca.markets"

#: The ONLY host this module will ever talk to. A set, not a string, so there is no
#: prefix/suffix game to play with it.
PAPER_HOSTS: frozenset[str] = frozenset({"paper-api.alpaca.markets"})

ORDERS_PATH = "/v2/orders"
ORDER_PATH = "/v2/orders/{order_id}"
POSITIONS_PATH = "/v2/positions"
POSITION_PATH = "/v2/positions/{symbol}"
ACCOUNT_PATH = "/v2/account"

#: Named here only so a test can assert it does not exist. It is permanently ``None``:
#: no code path reads it, so setting an environment variable of this name (or any
#: other) cannot change what :func:`assert_paper_url` accepts.
LIVE_ENV_VAR: str | None = None

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
TimeInForce = Literal["day", "gtc", "opg", "cls", "ioc", "fok"]
AssetClass = Literal["us_equity", "us_option"]


class LiveTradingRefused(AlpacaError):
    """Raised whenever anything points this module at a non-paper endpoint."""


class PaperAccountUnverified(AlpacaError):
    """Raised when ``/v2/account`` does not prove the account is a paper account."""


# ---------------------------------------------------------------------------
# the kill switch
# ---------------------------------------------------------------------------


def assert_paper_url(base: str) -> str:
    """Return ``base`` if it is the paper trading endpoint, else raise.

    The one function every outbound URL in this module passes through. It reads no
    environment variable and consults no flag: the answer depends only on the string
    it is given.

    Rules, in order:
      * the URL must parse and use https;
      * the host must be in :data:`PAPER_HOSTS` by **exact** equality;
      * as a belt-and-braces second check, a host containing ``api.alpaca.markets``
        that does not contain ``paper-`` is refused — this is redundant with the
        allowlist and is kept because the redundant check is the one that would still
        hold if someone widened the allowlist without thinking.
    """
    parts = urllib.parse.urlsplit((base or "").strip())
    if parts.scheme != "https":
        raise LiveTradingRefused(
            f"refusing non-https trading endpoint {base!r}; paper-only by construction"
        )
    host = (parts.hostname or "").lower()
    if host in PAPER_HOSTS:
        return base
    if "api.alpaca.markets" in host and "paper-" not in host:
        raise LiveTradingRefused(
            f"refusing LIVE trading endpoint {host!r}: this executor is paper-only by "
            f"construction. Allowed hosts: {sorted(PAPER_HOSTS)}. There is no env var, "
            f"flag or config key that enables live trading — that is the point."
        )
    raise LiveTradingRefused(
        f"refusing trading endpoint host {host!r}; allowed hosts: {sorted(PAPER_HOSTS)}"
    )


#: Executed at import. If this module is imported into a process whose defaults were
#: tampered with, import fails rather than the first order failing.
assert_paper_url(PAPER_TRADING_BASE)


# ---------------------------------------------------------------------------
# transport seam
# ---------------------------------------------------------------------------


class TradingTransport(Protocol):
    """The one injectable seam. Implementations get a full URL + parsed JSON body."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any: ...


class UrllibTradingTransport:
    """Real HTTPS via ``urllib``. Stdlib only.

    Same one-short-lived-request-per-call shape as ``UrllibTransport`` in the data
    layer: the trigger loop is not high-QPS, and per-request sockets make an error
    surface where it happened instead of being pooled and forgotten.
    """

    def __init__(self, timeout: float = 10.0, *, user_agent: str = "paper-hunter/0.1") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        full = url
        if params:
            clean = {k: str(v) for k, v in params.items() if v is not None}
            if clean:
                full = f"{url}?{urllib.parse.urlencode(clean)}"
        payload = None
        request = urllib.request.Request(full, data=None, method=method.upper())
        request.add_header("User-Agent", self.user_agent)
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request.data = payload
            request.add_header("Content-Type", "application/json")
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise _api_error(exc.code, detail, path=full) from None
        except urllib.error.URLError as exc:
            raise AlpacaError(f"alpaca transport failure for {full}: {exc.reason}") from None
        if not raw.strip():
            return {}
        return json.loads(raw)


def _api_error(status: int, raw: str, *, path: str = "") -> AlpacaAPIError:
    """Extract Alpaca's ``message``; fall back to a truncated body.

    Header values are never attached to the error, so a trading error can never carry
    a credential into a log line or a journal payload.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return AlpacaAPIError(status, (raw[:300] or "no body"), path=path)
    if isinstance(parsed, dict) and "message" in parsed:
        return AlpacaAPIError(status, str(parsed["message"])[:300], path=path)
    return AlpacaAPIError(status, raw[:300], path=path)


class MockTradingTransport:
    """Offline transport: canned JSON keyed by path.

    ``routes`` maps a path to a payload (returned as-is), or to a callable taking
    ``(method, body)`` and returning a payload. Unmatched paths raise
    ``AlpacaAPIError(404)`` — the same shape as the real API. ``calls`` records
    ``(method, path, body)`` so a test can assert an order was *not* sent.
    """

    def __init__(self, routes: Mapping[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = dict(routes or {})
        self.calls: list[tuple[str, str, Any]] = []

    def add(self, path: str, payload: Any) -> MockTradingTransport:
        self.routes[path] = payload
        return self

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        path = urllib.parse.urlsplit(url).path
        self.calls.append((method.upper(), path, dict(body or {})))
        if path not in self.routes:
            raise AlpacaAPIError(404, f"no mock route for {path}", path=path)
        payload = self.routes[path]
        if callable(payload):
            return payload(method.upper(), dict(body or {}))
        return payload


# ---------------------------------------------------------------------------
# typed returns
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class Order:
    """One Alpaca order. ``filled_avg_price`` is None until the fill lands."""

    id: str
    symbol: str
    qty: float
    side: Side
    order_type: OrderType
    status: str
    submitted_at: datetime | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    client_order_id: str | None = None

    @property
    def filled(self) -> bool:
        return self.status in ("filled", "closed") and (self.filled_qty or 0) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "qty": self.qty,
            "side": self.side,
            "order_type": self.order_type,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "filled_avg_price": self.filled_avg_price,
            "limit_price": self.limit_price,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Order:
        return cls(
            id=_as_str(raw.get("id")) or "",
            symbol=_as_str(raw.get("symbol")) or "",
            qty=_as_float(raw.get("qty")) or 0.0,
            side=(_as_str(raw.get("side")) or "buy"),  # type: ignore[arg-type]
            order_type=(_as_str(raw.get("type")) or "market"),  # type: ignore[arg-type]
            status=_as_str(raw.get("status")) or "unknown",
            submitted_at=_parse_ts(raw.get("submitted_at")),
            filled_qty=_as_float(raw.get("filled_qty")) or 0.0,
            filled_avg_price=_as_float(raw.get("filled_avg_price")),
            limit_price=_as_float(raw.get("limit_price")),
            client_order_id=_as_str(raw.get("client_order_id")),
        )


@dataclass(frozen=True, slots=True)
class PaperConfirmation:
    """Proof, carried into the journal and the CLI banner. Never a credential."""

    account_id: str
    status: str
    portfolio_value: float
    proof: Literal["paper_field", "id_prefix"]

    @property
    def is_paper(self) -> bool:
        return True

    def banner(self) -> str:
        return (
            f"PAPER ACCOUNT CONFIRMED via {self.proof}: id={self.account_id} "
            f"status={self.status} portfolio=${self.portfolio_value:,.2f} "
            f"endpoint={PAPER_TRADING_BASE} (paper-only executor)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "status": self.status,
            "portfolio_value": self.portfolio_value,
            "proof": self.proof,
            "endpoint": PAPER_TRADING_BASE,
        }


def verify_paper_account(payload: Mapping[str, Any]) -> PaperConfirmation:
    """Assert ``/v2/account`` describes a paper account. Raise if it does not.

    ``payload`` is the raw JSON dict (not the typed ``Account``) because the proof
    fields — ``paper`` and the id prefix — are not part of ``Account``'s model and
    deliberately stay out of it: the typed reader is for numbers, this is for a
    one-way door.
    """
    account = Account.from_json(payload)
    flag = payload.get("paper")
    if flag is True:
        proof: Literal["paper_field", "id_prefix"] = "paper_field"
    elif isinstance(account.id, str) and account.id.upper().startswith("PA"):
        proof = "id_prefix"
    else:
        raise PaperAccountUnverified(
            "account does not prove it is a paper account "
            f"(id={account.id!r}, paper flag={flag!r}); refusing to trade"
        )
    if account.trading_blocked:
        raise PaperAccountUnverified(
            f"account {account.id} reports trading_blocked; refusing to trade"
        )
    return PaperConfirmation(
        account_id=account.id,
        status=account.status,
        portfolio_value=account.portfolio_value,
        proof=proof,
    )


def _parse_ts(value: Any) -> datetime | None:
    text = _as_str(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlpacaTradingClient:
    """Paper-only order client.

    ``base`` is validated by :func:`assert_paper_url` on every request, not just at
    construction, so a client whose ``base`` is mutated after the fact (the dataclass
    is frozen, but ``object.__setattr__`` exists) still cannot send an order.
    """

    transport: TradingTransport = field(repr=False)
    key: str = field(repr=False, default="")
    secret: str = field(repr=False, default="")
    base: str = PAPER_TRADING_BASE

    @classmethod
    def from_env(cls, **kwargs: Any) -> AlpacaTradingClient:
        """Build from ``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET``.

        Missing credentials raise ``AlpacaConfigError`` naming the *variable*, never
        its value. The base URL is not a parameter here: it is a module constant, so
        there is no call site that can hand this client a live endpoint.
        """
        key = os.environ.get(KEY_ENV, "")
        secret = os.environ.get(SECRET_ENV, "")
        missing = [name for name, value in ((KEY_ENV, key), (SECRET_ENV, secret)) if not value]
        if missing:
            raise AlpacaConfigError(
                f"missing Alpaca credential env var(s): {', '.join(missing)} "
                f"(paper keys only — real-money keys never exist on this fleet)"
            )
        return cls(transport=UrllibTradingTransport(), key=key, secret=secret, **kwargs)

    # -- plumbing --------------------------------------------------------------

    @property
    def headers(self) -> dict[str, str]:
        if not self.key or not self.secret:
            raise AlpacaConfigError(
                f"client has no credentials — use AlpacaTradingClient.from_env() with "
                f"{KEY_ENV}/{SECRET_ENV} set, or pass key/secret explicitly"
            )
        return {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
        }

    def url_for(self, path: str) -> str:
        """The full URL for ``path``, kill-switched. Every request goes through this."""
        return f"{assert_paper_url(self.base)}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.transport.request(
            method,
            self.url_for(path),
            headers=self.headers,
            params={k: v for k, v in (params or {}).items() if v is not None},
            body=body,
        )

    def __repr__(self) -> str:
        state = "configured" if (self.key and self.secret) else "unconfigured"
        return f"AlpacaTradingClient({state}, base={self.base})"

    __str__ = __repr__

    # -- paper account proof ---------------------------------------------------

    def assert_paper_account(self) -> PaperConfirmation:
        """Fetch ``/v2/account`` and return the paper proof. Raises if unproven."""
        return verify_paper_account(self._request("GET", ACCOUNT_PATH))

    # -- orders ----------------------------------------------------------------

    def submit_order(
        self,
        *,
        symbol: str,
        qty: float,
        side: Side,
        order_type: OrderType = "market",
        limit_price: float | None = None,
        time_in_force: TimeInForce = "day",
        asset_class: AssetClass = "us_option",
        client_order_id: str | None = None,
    ) -> Order:
        """``POST /v2/orders``.

        ``symbol`` is a plain ticker for equities and an OCC symbol for options; the
        account's trading permissions decide whether it is accepted, and this module
        does not second-guess that. ``qty`` may be fractional (allowed for both asset
        classes on Alpaca) — the position manager passes whole contracts for options
        because a fractional option contract is not a thing, but it does not
        re-derive that here.
        """
        if qty is None or qty <= 0:
            raise AlpacaError(f"order qty must be positive, got {qty!r}")
        if side not in ("buy", "sell"):
            raise AlpacaError(f"order side must be buy/sell, got {side!r}")
        if order_type not in ("market", "limit", "stop", "stop_limit"):
            raise AlpacaError(f"unsupported order type {order_type!r}")
        if order_type in ("limit", "stop_limit") and limit_price is None:
            raise AlpacaError(f"{order_type} order requires limit_price")
        body: dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
            "asset_class": asset_class,
        }
        if limit_price is not None:
            body["limit_price"] = limit_price
        if client_order_id:
            body["client_order_id"] = client_order_id
        return Order.from_json(self._request("POST", ORDERS_PATH, body=body))

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """``DELETE /v2/orders/{id}``. Returns the API's ack payload.

        Cancelling is a mechanical exit path, not a judgment call — it is here because
        a mechanical position manager needs to be able to pull a resting order it
        placed, and because a test needs to assert the call happened.
        """
        payload = self._request("DELETE", ORDER_PATH.format(order_id=order_id))
        return payload if isinstance(payload, dict) else {"id": order_id}

    def get_order(self, order_id: str) -> Order:
        return Order.from_json(self._request("GET", ORDER_PATH.format(order_id=order_id)))

    def get_orders(
        self, *, status: Literal["open", "closed", "all"] = "open", limit: int = 100
    ) -> list[Order]:
        payload = self._request("GET", ORDERS_PATH, params={"status": status, "limit": limit})
        if not isinstance(payload, list):
            raise AlpacaError(f"expected a list of orders, got {type(payload).__name__}")
        return [Order.from_json(o) for o in payload]

    def cancel_all_orders(self) -> None:
        """``DELETE /v2/orders``. Used by the EOD pass to flatten working orders."""
        self._request("DELETE", ORDERS_PATH)

    # -- positions / account ---------------------------------------------------

    def get_positions(self) -> list[Position]:
        payload = self._request("GET", POSITIONS_PATH)
        if not isinstance(payload, list):
            raise AlpacaError(f"expected a list of positions, got {type(payload).__name__}")
        return [Position.from_json(p) for p in payload]

    def close_position(self, symbol: str, *, qty: float | None = None) -> Order:
        """``DELETE /v2/positions/{symbol}`` — the broker-side flatten.

        # INTERPRETATION: this is the *emergency* flatten, used when the executor
        cannot reason about a position (unparseable symbol, missing state). Normal
        exits go through :meth:`submit_order` so the journal sees an explicit order.
        Both end at the same broker; this one does not ask questions.
        """
        body: dict[str, Any] | None = {"qty": qty} if qty is not None else None
        return Order.from_json(
            self._request("DELETE", POSITION_PATH.format(symbol=symbol), body=body)
        )

    def get_account(self) -> Account:
        return Account.from_json(self._request("GET", ACCOUNT_PATH))


__all__ = [
    "ACCOUNT_PATH",
    "AlpacaAPIError",
    "AlpacaConfigError",
    "AlpacaError",
    "AlpacaTradingClient",
    "KEY_ENV",
    "LIVE_TRADING_BASE",
    "LiveTradingRefused",
    "MockTradingTransport",
    "ORDERS_PATH",
    "ORDER_PATH",
    "Order",
    "PAPER_HOSTS",
    "PAPER_TRADING_BASE",
    "PaperAccountUnverified",
    "PaperConfirmation",
    "SECRET_ENV",
    "TradingTransport",
    "UrllibTradingTransport",
    "assert_paper_url",
    "verify_paper_account",
]
