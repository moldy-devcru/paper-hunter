"""Shared offline fixtures for the Phase 3a data-layer tests.

Everything here is synthetic or hand-transcribed from Alpaca's *documented* response
shapes. No test in this repository performs a network call, and nothing here reads an
Alpaca credential. That is enforced by the fact that every test constructs its client
with a ``MockTransport`` rather than ``AlpacaClient.from_env()``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# synthetic daily bars
# ---------------------------------------------------------------------------

#: Start date for the synthetic SPY-like daily series (a Monday).
SYNTH_START = dt.date(2024, 6, 3)


def synthetic_daily_payload(
    *,
    count: int = 260,
    start: float = 450.0,
    drift: float = 0.0011,
    seed: int = 7,
    feed: str = "sip",
    volume_base: float = 55_000_000.0,
    start_date: dt.date | None = None,
) -> dict:
    """A deterministic, SPY-shaped daily series.

    Not random: a seeded LCG so the series is byte-identical on every run (the journal
    requires reproducible indicator values, and a test fixture that changes between
    runs would hide exactly the kind of drift we care about). Shape: slow uptrend with
    sinusoid + noise, so EMA50 sits above EMA200 and RSI lands in a tradable band
    rather than pinned at 50 or 100.

    ``volume_base`` and a sinusoidal volume wobble mean the last bar can be given a
    volume spike for T4 tests without hand-editing the payload.
    """
    rng = _Lcg(seed)
    bars = []
    price = start
    day = start_date or SYNTH_START
    i = 0
    while len(bars) < count:
        # Only weekdays become bars, and the day advances ONLY on a trading day — so
        # `count` is exactly the number of bars. (Generating `count` calendar days and
        # dropping weekends silently yields ~72% of the bars you asked for, which is
        # how a "260-bar warmup fixture" ends up 186 bars long and EMA200 never defines.)
        if day.weekday() < 5:
            wave = math.sin(i / 11.0) * 0.006
            noise = (rng.next() - 0.5) * 0.012
            ret = drift + wave + noise
            open_ = price
            close = open_ * (1.0 + ret)
            high = max(open_, close) * (1.0 + abs(noise) * 0.9)
            low = min(open_, close) * (1.0 - abs(noise) * 0.9)
            volume = volume_base * (
                1.0 + 0.18 * math.sin(i / 7.0) + 0.10 * (rng.next() - 0.5)
            )
            bars.append(
                {
                    "t": dt.datetime.combine(day, dt.time(21, 0), tzinfo=dt.UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "o": round(open_, 4),
                    "h": round(high, 4),
                    "l": round(low, 4),
                    "c": round(close, 4),
                    "v": round(volume, 0),
                    "n": 480_000 + i,
                    "vw": round(close - 0.02, 4),
                }
            )
            price = close
            i += 1
        day += dt.timedelta(days=1)
    # Mirrors the documented single-symbol envelope (stockbarsingle-1): ``bars`` is a
    # flat list, not a symbol-keyed object. The keyed shape belongs to the
    # multi-symbol route /v2/stocks/bars?symbols=..., which this project never calls.
    return {"symbol": "SPY", "feed": feed, "bars": bars, "next_page_token": None}


def with_volume_spike(payload: dict, *, multiplier: float = 2.2) -> dict:
    """Copy of a bars payload with the newest bar's volume multiplied (T4 test aid)."""
    out = json.loads(json.dumps(payload))
    bars = out["bars"]
    bars[-1]["v"] = bars[-1]["v"] * multiplier
    return out


def series_from(payload: dict, *, feed: str | None = None):
    """Payload -> ``BarSeries`` (the shape the snapshot builder and hunt plan want)."""
    from executor.alpaca_client import Bar, BarSeries

    bars = sorted((Bar.from_json(b) for b in payload["bars"]), key=lambda b: b.t)
    return BarSeries(
        symbol="SPY",
        timeframe="1Day",
        feed=feed or payload.get("feed", "sip"),
        bars=bars,
    )


# ---------------------------------------------------------------------------
# synthetic options chains
# ---------------------------------------------------------------------------


def make_contract(
    *,
    symbol: str,
    underlying: str,
    expiry: str,
    strike: float,
    right: str,
    ask: float | None = 1.0,
    bid: float | None = None,
    delta: float | None = None,
    iv: float | None = 0.18,
):
    """One ``OptionContract``, built directly (no OCC parsing, no network)."""
    from executor.alpaca_client import Greeks, OptionContract, OptionQuote, OptionTrade

    return OptionContract(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        right=right,  # type: ignore[arg-type]
        implied_volatility=iv,
        greeks=Greeks(delta=delta),
        latest_quote=OptionQuote(bid=bid, ask=ask),
        latest_trade=OptionTrade(p=ask),
    )


def zero_dte_chain(
    *,
    day: dt.date,
    spot: float = 625.0,
    step: float = 1.0,
    otm_calls: int = 4,
    otm_puts: int = 4,
    ask: float = 1.40,
    underlying: str = "SPY",
):
    """A same-expiry (0DTE) chain around ``spot`` with one premium per contract.

    Enough for arm B's strike rule: the first OTM strike beyond the setup-day range
    projection, and a premium-cap check against the $200 hard cap.
    """
    expiry = day.strftime("%Y%m%d")
    calls = [
        make_contract(
            symbol=f"{underlying}{expiry}C{int((spot + i * step) * 1000):08d}",
            underlying=underlying,
            expiry=expiry,
            strike=round(spot + i * step, 2),
            right="call",
            ask=ask,
        )
        for i in range(1, otm_calls + 1)
    ]
    puts = [
        make_contract(
            symbol=f"{underlying}{expiry}P{int((spot - i * step) * 1000):08d}",
            underlying=underlying,
            expiry=expiry,
            strike=round(spot - i * step, 2),
            right="put",
            ask=ask,
        )
        for i in range(1, otm_puts + 1)
    ]
    return _chain(underlying, calls + puts)


def deep_itm_chain(
    *,
    day: dt.date,
    spot: float = 625.0,
    underlying: str = "SPY",
    specs: list[tuple[int, float, float]] | None = None,
):
    """A chain of long-dated calls for arm C's selection.

    ``specs`` is ``[(dte, strike, delta), ...]``. The default set deliberately contains
    a too-short expiry, a too-long one, and a delta below the floor, so the selection
    has something to reject.
    """
    if specs is None:
        specs = [
            (60, spot - 20, 0.88),    # outside the DTE window
            (120, spot - 20, 0.55),   # inside the window, delta too low
            (120, spot - 10, 0.82),   # qualifying, expensive
            (120, spot - 12, 0.85),   # qualifying, cheaper -> the selection
            (200, spot - 30, 0.95),   # outside the DTE window
        ]
    contracts = []
    for dte, strike, delta in specs:
        expiry_day = day + dt.timedelta(days=dte)
        exp = expiry_day.strftime("%Y%m%d")
        # Premium falls as the strike drops (deeper ITM), scaled off the delta.
        premium = max(0.20, round((1.0 - delta) * 40.0 + 2.0, 2))
        contracts.append(
            make_contract(
                symbol=f"{underlying}{exp}C{int(strike * 1000):08d}",
                underlying=underlying,
                expiry=exp,
                strike=strike,
                right="call",
                ask=premium,
                delta=delta,
            )
        )
    return _chain(underlying, contracts)


def _chain(underlying: str, contracts: list):
    from executor.alpaca_client import OptionChain

    return OptionChain(
        underlying=underlying,
        feed="opra",
        contracts=contracts,
        fetched_at=None,
    )


def calendar_with(*, day: dt.date, kind: str = "fomc", label: str = "FOMC decision",
                  veto: bool | None = None):
    """A one-event calendar (``veto`` defaults to the kind's own severity)."""
    from data.event_calendar import CalendarEvent, CalendarFile, EventCalendar

    return EventCalendar(
        [
            CalendarFile(
                quarter=None,
                updated=day,
                events=[
                    CalendarEvent(
                        date=day,
                        kind=kind,  # type: ignore[arg-type]
                        label=label,
                        veto=veto,
                        verified=True,
                        source="test fixture",
                    )
                ],
            )
        ]
    )


class _Lcg:
    """Deterministic linear congruential generator (Numerical Recipes constants).

    Not a crypto RNG and not ``random`` — the point is that it does not depend on the
    Python version's Mersenne implementation, so a fixture generated on 3.12 is the
    same fixture on 3.13.
    """

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF or 1

    def next(self) -> float:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state / 0x100000000