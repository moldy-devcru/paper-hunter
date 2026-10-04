"""Does the /v2/stocks/{symbol}/bars route answer a limit-only query?

Minimal: 4 calls. Prints counts and the first bar's OHLCV (never credentials).
"""
from __future__ import annotations

import datetime as dt
import os

from executor.alpaca_client import AlpacaClient

ENV = os.path.expanduser("~/.config/paper-hunter/alpaca.env")
for line in open(ENV):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()

client = AlpacaClient.from_env()
now = dt.datetime.now(dt.UTC)
end = (now - dt.timedelta(minutes=20)).isoformat(timespec="seconds").replace("+00:00", "Z")


def show(label, series):
    print(f"{label}: {len(series.bars)} bars", end="")
    if series.bars:
        b = series.bars[-1]
        print(f" | newest t={b.t.isoformat()} o={b.o} h={b.h} l={b.l} c={b.c} v={b.v} n={b.n}")
    else:
        print(" | EMPTY")


show("daily limit-only (400)", client.get_daily_bars("SPY", feed="sip", limit=400))
show(
    "daily windowed (400, start=-640d)",
    client.get_daily_bars(
        "SPY",
        feed="sip",
        limit=400,
        start=(now - dt.timedelta(days=640)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        end=end,
    ),
)
show(
    "intraday limit-only (5)",
    client.get_intraday_bars("SPY", timeframe="1Min", feed="iex", limit=5),
)
show(
    "intraday windowed (700, 15Min, -30d)",
    client.get_intraday_bars(
        "SPY",
        timeframe="15Min",
        feed="iex",
        limit=700,
        start=(now - dt.timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        end=end,
    ),
)