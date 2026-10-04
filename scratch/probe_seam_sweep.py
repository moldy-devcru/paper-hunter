"""Probe: which live bars-read shapes answer, and which answer EMPTY.

2026-10-03, paper creds, `GET /v2/stocks/SPY/bars`. The question this run asks is not
"does the route work" (measured twice already in ede6342) but "which of the SHAPES the
repo actually sends are answerable" — because every seam so far has been a shape, not a
bug: `limit` and no window answers 0 bars with HTTP 200.

Shapes probed (one call each, paced):

  A  1Day/sip  limit only                        (cmd_eod today, soak today)
  B  1Day/sip  end only (no start)              (soak today)
  C  1Day/sip  bar_window(400)                  (the fix)
  D  1Day/sip  start only, no end, sort=desc
  E  1Day/iex  limit only
  F  1Day/sip  limit=400 + start + end, NO sort (proves sort is load-bearing)

Nothing is printed except counts and the newest/oldest bar date. No payloads.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.alpaca_client import AlpacaClient  # noqa: E402
from executor.watch_loop import SIP_END_BACKOFF_MINUTES, bar_window  # noqa: E402

ENV_PATH = os.path.expanduser("~/.config/paper-hunter/alpaca.env")


def _load_env() -> None:
    for line in open(ENV_PATH):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def probe(client: AlpacaClient, label: str, **kwargs) -> None:
    time.sleep(0.5)  # free tier is 200/min; six calls do not need speed
    try:
        series = client.get_daily_bars("SPY", **kwargs)
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        print(f"{label}: RAISED {type(exc).__name__}: {str(exc)[:160]}")
        return
    bars = series.bars
    newest = bars[-1].t.date().isoformat() if bars else "-"
    oldest = bars[0].t.date().isoformat() if bars else "-"
    print(f"{label}: n={len(bars)} oldest={oldest} newest={newest}")


def main() -> int:
    _load_env()
    client = AlpacaClient.from_env()
    now = dt.datetime.now(dt.UTC)
    window = bar_window(timeframe="1Day", feed="sip", limit=400)

    probe(client, "A limit-only sip", feed="sip", limit=400)
    probe(
        client,
        "B end-only sip",
        feed="sip",
        end=(now - dt.timedelta(minutes=SIP_END_BACKOFF_MINUTES))
        .isoformat()
        .replace("+00:00", "Z"),
        limit=400,
    )
    probe(client, "C bar_window sip", feed="sip", limit=400, **window)
    probe(
        client,
        "D start-only sip (no end)",
        feed="sip",
        start=window["start"],
        sort=window["sort"],
        limit=400,
    )
    probe(client, "E limit-only iex", feed="iex", limit=400)
    probe(
        client,
        "F windowed, NO sort",
        feed="sip",
        limit=400,
        start=window["start"],
        end=window["end"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())