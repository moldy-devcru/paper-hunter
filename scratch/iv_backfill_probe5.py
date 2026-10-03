"""Probe 5: what exactly triggers the 403 on options bars? One careful pass.

Known so far: single-symbol historical requests return 200 with data; a large
multi-symbol chunk returns 403 'OPRA agreement is not signed'. This isolates whether the
trigger is symbol COUNT, a specific symbol, or the expiry.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from executor.alpaca_client import AlpacaClient, AlpacaError, chunk_option_symbols  # noqa: E402


def occ(expiry: str, right: str, strike: float) -> str:
    return f"SPY{expiry}{right}{int(round(strike * 1000)):08d}"


def try_batch(client, syms, label):
    try:
        series = client.get_option_daily_bars(syms, start="2026-06-01", end="2026-09-18")
        print("%-34s OK  asked=%d syms_with_data=%d" % (label, len(syms), len(series)))
        return True
    except AlpacaError as exc:
        print("%-34s %s asked=%d %s" % (label, getattr(exc, "status", "?"), len(syms),
                                        str(exc)[:70]))
        return False


def main() -> None:
    c = AlpacaClient.from_env()

    # 1. Symbol count, one expiry (260918), strikes near 700, all known-good format.
    for n in (1, 2, 5, 10, 20, 50, 100):
        syms = [occ("260918", "C", 700.0 + i) for i in range(n)]
        try_batch(c, syms, f"count={n} same-expiry calls")

    # 2. Same count, both rights mixed (does mixing sides matter?).
    try_batch(c, [occ("260918", r, 700.0 + i) for i in range(10) for r in "CP"],
              "count=20 mixed-rights")

    # 3. Same count, MANY expiries (does span matter?).
    syms = []
    for exp in ("260618", "260625", "260702", "260709", "260716"):
        for i in range(4):
            syms.append(occ(exp, "C", 695.0 + i))
    try_batch(c, syms, f"count=20 five-expiries")

    # 4. The stale expiry that may have triggered the earlier 403.
    try_batch(c, [occ("260102", "C", 700.0)], "stale-expiry 260102 alone")
    try_batch(c, [occ("260918", "C", 700.0), occ("260102", "C", 700.0)],
              "fresh + stale together")

    # 5. Does a SHORT window behave differently from a long one?
    try_batch(c, [occ("260918", "C", 700.0 + i) for i in range(20)],
              "count=20 short-window")

    # 6. What the client itself would send.
    big = [occ("260918", "C", 690.0 + i * 0.5) for i in range(100)]
    chunks = chunk_option_symbols(big, 100)
    try_batch(c, chunks[0], "chunk_option_symbols(100)")


if __name__ == "__main__":
    main()