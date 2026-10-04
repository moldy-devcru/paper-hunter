"""Live check of the FIXED provider: daily_series must return a fresh signal bar."""
from __future__ import annotations

import datetime as dt
import os

from executor.watch_loop import AlpacaWatchData

ENV = os.path.expanduser("~/.config/paper-hunter/alpaca.env")
for line in open(ENV):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()

from executor.alpaca_client import AlpacaClient  # noqa: E402

client = AlpacaClient.from_env()
provider = AlpacaWatchData(client)
daily = provider.daily_series("SPY")
print(
    f"daily_series: {len(daily.bars)} bars | newest {daily.bars[-1].t.date()} "
    f"c={daily.bars[-1].c}"
)
age = (dt.datetime.now(dt.UTC).date() - daily.bars[-1].t.date()).days
print(f"signal bar age: {age} calendar days (want small: newest, not truncated-oldest)")
intraday = provider._run_rate_series("SPY")
print(f"run-rate series: {len(intraday.bars) if intraday else 0} bars | newest "
      f"{intraday.bars[-1].t.isoformat() if intraday else '-'}")