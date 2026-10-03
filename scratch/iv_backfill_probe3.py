"""Depth probe (final): how far back, what fields, enough prices for BS inversion?

Answers with live responses, not reference notes. Prints no credentials.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from executor.alpaca_client import AlpacaClient, AlpacaError  # noqa: E402


def occ(expiry_yymmdd: str, right: str, strike: float) -> str:
    return f"SPY{expiry_yymmdd}{right}{int(round(strike * 1000)):08d}"


def call(client: AlpacaClient, syms: list[str], start: str, end: str) -> dict:
    try:
        series = client.get_option_daily_bars(syms, start=start, end=end)
        got = series.symbols_with_data()
        return {"status": 200, "n": len(got), "series": series, "got": got}
    except AlpacaError as exc:
        return {"status": getattr(exc, "status", "?"), "err": str(exc)[:160]}


def main() -> None:
    if not os.environ.get("ALPACA_PAPER_KEY"):
        raise SystemExit("no creds in env")
    c = AlpacaClient.from_env()

    print("== A. depth floor: walk backwards, same live-ish strike band ==")
    # SPY strikes vary by era; use a strike that plausibly existed then.
    for label, exp, strike, start, end in [
        ("2024-06 expiry", "240621", 550.0, "2024-06-01", "2024-06-28"),
        ("2024-09 expiry", "240920", 555.0, "2024-09-01", "2024-09-30"),
        ("2024-12 expiry", "241220", 600.0, "2024-12-01", "2024-12-31"),
        ("2025-03 expiry", "250321", 590.0, "2025-03-01", "2025-03-31"),
        ("2023-12 expiry", "231215", 450.0, "2023-11-01", "2023-12-31"),
        ("2023-06 expiry", "230616", 430.0, "2023-05-01", "2023-06-30"),
    ]:
        syms = [occ(exp, "C", strike), occ(exp, "P", strike)]
        r = call(c, syms, start, end)
        if r["status"] != 200:
            print("  %-16s status=%s %s" % (label, r["status"], r.get("err", "")))
            continue
        bars = r["series"].bars_by_symbol
        n = sum(len(v) for v in bars.values())
        days = [b.t.date().isoformat() for v in bars.values() for b in v]
        print("  %-16s status=200 syms_with_data=%d bars=%d range=%s..%s"
              % (label, r["n"], n, min(days) if days else "-", max(days) if days else "-"))

    print("== B. bar fields / envelope shape (one 2026 contract, 3 months) ==")
    syms = [occ("260918", "C", 700.0), occ("260918", "P", 700.0)]
    r = call(c, syms, "2026-06-15", "2026-09-18")
    print("  status=%s syms=%d" % (r["status"], r.get("n", 0)))
    if r["status"] == 200:
        ser = r["series"]
        for s in ser.symbols_with_data()[:2]:
            v = ser.bars_by_symbol[s]
            print("  %s n=%d" % (s, len(v)))
            print("    first=%s" % json.dumps(dataclasses.asdict(v[0]), default=str))
        # raw envelope check: does it carry impliedVolatility / trades / quotes?
        print("  raw keys sample: %s" % json.dumps({k: v[0] for k, v in list(ser.bars_by_symbol.items())[:1]})[:400])

    print("== C. does a STALE/expired contract 403, or just come back empty? ==")
    for label, sym in [
        ("recent-expiry 2026-01", occ("260102", "C", 700.0)),
        ("2026-06", occ("260619", "C", 700.0)),
        ("nonexistent strike", occ("260918", "C", 1234.0)),
    ]:
        r = call(c, [sym], "2026-01-01", "2026-09-30")
        print("  %-24s %s status=%s n=%s %s" % (label, sym, r["status"], r.get("n", "-"), r.get("err", "")[:80]))

    print("== D. batch size sanity (valid 8-digit strikes, 100 max) ==")
    batch = [occ("260918", "C", 690.0 + i) for i in range(0, 21)]
    r = call(c, batch, "2026-09-01", "2026-09-30")
    print("  asked=%d status=%s syms_with_data=%d" % (len(batch), r["status"], r.get("n", 0)))

    print("PROBE_DONE %s" % date.today().isoformat())


if __name__ == "__main__":
    main()