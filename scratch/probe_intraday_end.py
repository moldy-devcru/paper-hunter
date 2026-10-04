"""IEX intraday: is an explicit `end` clamp needed, or is `start` alone enough?"""
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


def iso(d):
    return d.isoformat(timespec="seconds").replace("+00:00", "Z")


def raw(timeframe, feed, start, end, limit):
    return client._get(
        client.data_base,
        "/v2/stocks/SPY/bars",
        {
            "timeframe": timeframe,
            "feed": feed,
            "start": iso(start),
            "end": iso(end) if end else None,
            "limit": limit,
            "sort": "desc",
            "adjustment": "raw" if feed == "iex" else "all",
        },
    )


for label, tf, feed, back, limit, end in [
    ("1Min iex, start=-5d, NO end, limit=5", "1Min", "iex", 5, 5, None),
    (
        "1Min sip, start=-5d, end=now-20m, limit=5",
        "1Min",
        "sip",
        5,
        5,
        now - dt.timedelta(minutes=20),
    ),
    ("15Min iex, start=-45d, NO end, limit=700", "15Min", "iex", 45, 700, None),
]:
    try:
        p = raw(tf, feed, now - dt.timedelta(days=back), end, limit)
        bars = p.get("bars") or []
        ts = sorted(b["t"] for b in bars)
        print(
            f"{label}: {len(bars)} bars | oldest {ts[0][:16] if ts else '-'} "
            f"newest {ts[-1][:16] if ts else '-'}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"{label}: {type(exc).__name__}: {exc}")