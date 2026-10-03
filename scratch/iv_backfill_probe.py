"""Depth probe: what does /v1beta1/options/bars actually serve on THIS plan?

Research scratch (2026-10-03). The feasibility doc's data-depth claims came from dated
reference notes, not a live response. This probes the real route with the real paper
credential and records what comes back, so the backfill scope is set by evidence.

Questions:
  1. How far back does the feed actually go? (doc claims Feb 2024)
  2. What fields does a bar carry? trades? quotes? close?
  3. Is there enough priced history near-the-money for BS inversion?

Prints no credentials. Run with the creds already exported in the environment.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from executor.alpaca_client import AlpacaClient, AlpacaError  # noqa: E402


def probe(symbols: list[str], start: str, end: str) -> dict:
    client = AlpacaClient.from_env()
    out: dict = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i : i + 100]
        try:
            series = client.get_option_daily_bars(chunk, start=start, end=end)
        except AlpacaError as exc:
            out.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}")
            continue
        out.setdefault("bars_by_symbol", {})
        for sym in series.symbols_with_data():
            out["bars_by_symbol"][sym] = [
                {"t": b.t.isoformat(), "c": b.c, "o": b.o, "h": b.h, "l": b.l, "v": b.v}
                for b in series.bars_by_symbol.get(sym, [])
            ]
        out["requested"] = sorted(set(out.get("requested", [])) | set(chunk))
    return out


def main() -> None:
    if not os.environ.get("ALPACA_PAPER_KEY"):
        raise SystemExit("no creds in env")

    # Probe 1: a handful of mid-2024 SPY contracts, wide window -> where does depth start?
    mid_2024 = [f"SPY240621P{i:08d}" for i in range(55200, 55220, 5)]
    mid_2024 += [f"SPY240621C{i:08d}" for i in range(55200, 55220, 5)]
    r1 = probe(mid_2024, "2024-01-01", "2024-12-31")
    firsts = sorted(
        (min(v, key=lambda b: b["t"]), s) for s, v in r1.get("bars_by_symbol", {}).items()
    ) if r1.get("bars_by_symbol") else []
    print("PROBE1 symbols_asked=40 got=%d" % len(r1.get("bars_by_symbol", {})))
    if firsts:
        print("PROBE1 earliest_bar=%s  latest_bar=%s" % (firsts[0][0], max(v[0]["t"] for v in r1["bars_by_symbol"].values())))
    if r1.get("errors"):
        print("PROBE1 errors=%s" % r1["errors"][:2])
    sample = next(iter(r1.get("bars_by_symbol", {}).items()), None)
    if sample:
        print("PROBE1 sample_symbol=%s n_bars=%d first_bar=%s"
              % (sample[0], len(sample[1]), json.dumps(sample[1][0])))

    # Probe 2: the 2024 floor. Ask for early-2024 explicitly.
    early = [f"SPY240119C{i:08d}" for i in range(47200, 47220, 5)]
    r2 = probe(early, "2024-01-01", "2024-06-30")
    print("PROBE2 symbols_asked=4 got=%d errors=%s" % (len(r2.get("bars_by_symbol", {})), r2.get("errors", [""])[:1]))
    for s, v in list(r2.get("bars_by_symbol", {}).items())[:2]:
        print("PROBE2 %s first=%s n=%d" % (s, json.dumps(v[0]), len(v)))

    # Probe 3: today-ish contracts, wide history -> does the route keep history for
    # contracts that are STILL listed (the ones the gate actually reads)?
    now = [f"SPY260102C{i:08d}" for i in range(70000, 70020, 5)]
    now += [f"SPY260918C{i:08d}" for i in range(70000, 70020, 5)]
    r3 = probe(now, "2026-06-01", "2026-10-03")
    print("PROBE3 symbols_asked=8 got=%d errors=%s" % (len(r3.get("bars_by_symbol", {})), r3.get("errors", [""])[:1]))
    for s, v in list(r3.get("bars_by_symbol", {}).items())[:3]:
        print("PROBE3 %s first=%s last=%s n=%d" % (s, json.dumps(v[0]), json.dumps(v[-1]), len(v)))

    # Probe 4: field shape. Does the envelope carry impliedVolatility or trades/quotes?
    if r3.get("bars_by_symbol"):
        print("PROBE4 bar_fields=%s" % sorted(r3["bars_by_symbol"][next(iter(r3["bars_by_symbol"]))][0]))

    # Probe 5: far-back attempt (2023) — does it 422/404 or come back empty?
    old = [f"SPY231215C{i:08d}" for i in range(44000, 44010, 5)]
    r5 = probe(old, "2023-06-01", "2023-12-31")
    print("PROBE5 got=%d errors=%s" % (len(r5.get("bars_by_symbol", {})), r5.get("errors", [""])[:1]))

    print("PROBE_DONE date=%s" % date.today().isoformat())


if __name__ == "__main__":
    main()