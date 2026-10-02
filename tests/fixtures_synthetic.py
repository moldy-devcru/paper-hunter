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
    return {"symbol": "SPY", "feed": feed, "bars": {"SPY": bars}, "next_page_token": None}


def with_volume_spike(payload: dict, *, multiplier: float = 2.2) -> dict:
    """Copy of a bars payload with the newest bar's volume multiplied (T4 test aid)."""
    out = json.loads(json.dumps(payload))
    bars = out["bars"]["SPY"]
    bars[-1]["v"] = bars[-1]["v"] * multiplier
    return out


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