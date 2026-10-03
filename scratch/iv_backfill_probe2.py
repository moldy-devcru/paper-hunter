"""Probe 2: pin down the options-bars entitlement wall, exactly. Second attempt.

Records the raw HTTP status for each distinct request shape so the verdict is not
"one 403 somewhere". Prints no credentials.
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from executor.alpaca_client import AlpacaClient, AlpacaError  # noqa: E402

CASES = [
    ("1known-2024", ["SPY240621C00552000"], "2024-06-01", "2024-06-28"),
    ("1known-2026", ["SPY260918C00700000"], "2026-09-01", "2026-09-30"),
    ("1known-2023", ["SPY231215C00440000"], "2023-11-01", "2023-12-31"),
    ("batch5-2026", [f"SPY260918C0070{i:04d}0" for i in range(0, 25, 5)], "2026-09-01", "2026-09-30"),
    ("batch5-2024", [f"SPY240621C0055{i:04d}0" for i in range(0, 25, 5)], "2024-06-01", "2024-06-28"),
]


def main() -> None:
    if not os.environ.get("ALPACA_PAPER_KEY"):
        raise SystemExit("no creds in env")
    client = AlpacaClient.from_env()

    # Control: the STOCK bars route, same credential. If stock works and options
    # 403s, it is an entitlement wall on options, not a dead credential.
    try:
        sb = client.get_stock_daily_bars("SPY", start="2026-09-01", end="2026-09-30")
        print("CONTROL stock_bars: OK bars=%d" % len(sb))
    except Exception as exc:  # noqa: BLE001
        print("CONTROL stock_bars: %s: %s" % (type(exc).__name__, exc))

    try:
        ch = client.get_option_chain("SPY")
        print("CONTROL option_chain: OK contracts=%d expiries=%d"
              % (len(ch), len(ch.expiries())))
    except Exception as exc:  # noqa: BLE001
        print("CONTROL option_chain: %s: %s" % (type(exc).__name__, exc))

    for name, syms, start, end in CASES:
        try:
            series = client.get_option_daily_bars(syms, start=start, end=end)
            got = series.symbols_with_data()
            print("CASE %-12s asked=%d status=200 bars_symbols=%d %s"
                  % (name, len(syms), len(got), got[:2]))
        except AlpacaError as exc:
            print("CASE %-12s asked=%d status=%s %s"
                  % (name, len(syms), getattr(exc, "status", "?"), str(exc)[:120]))


if __name__ == "__main__":
    main()