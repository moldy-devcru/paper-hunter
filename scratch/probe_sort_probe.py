"""Which end does `limit` truncate? And does the documented `sort` param fix it?"""
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
start = (now - dt.timedelta(days=640)).isoformat(timespec="seconds").replace("+00:00", "Z")


def show(label, series):
    if series.bars:
        print(
            f"{label}: {len(series.bars)} bars | oldest {series.bars[0].t.date()} "
            f"newest {series.bars[-1].t.date()} | next_page_token={series.next_page_token}"
        )
    else:
        print(f"{label}: EMPTY | next_page_token={series.next_page_token}")


show(
    "asc  (default), 640d window, limit=400",
    client.get_daily_bars("SPY", feed="sip", limit=400, start=start, end=end),
)
# sort is a documented param on this route (DOC_VERIFIED_ROUTES), but get_bars does
# not expose it yet — call the transport directly to learn whether it is honoured.
payload = client._get(
    client.data_base,
    "/v2/stocks/SPY/bars",
    {
        "timeframe": "1Day",
        "feed": "sip",
        "start": start,
        "end": end,
        "limit": 400,
        "adjustment": "all",
        "sort": "desc",
    },
)
raw = payload.get("bars") or []
ts = sorted(b["t"] for b in raw)
print(
    f"sort=desc: {len(raw)} bars | oldest {ts[0][:10]} newest {ts[-1][:10]} "
    f"| next_page_token={'set' if payload.get('next_page_token') else 'none'}"
)