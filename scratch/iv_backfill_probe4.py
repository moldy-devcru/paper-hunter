"""Probe 3b: raw envelope keys (does the wire carry impliedVolatility?) + stale-symbol behaviour."""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from executor.alpaca_client import (  # noqa: E402
    OPTIONS_HISTORICAL_BARS_PATH,
    AlpacaClient,
    AlpacaError,
)


def occ(expiry_yymmdd: str, right: str, strike: float) -> str:
    return f"SPY{expiry_yymmdd}{right}{int(round(strike * 1000)):08d}"


def main() -> None:
    c = AlpacaClient.from_env()
    sym = occ("260918", "C", 700.0)
    raw = c._get(
        c.data_base,
        OPTIONS_HISTORICAL_BARS_PATH,
        {"symbols": sym, "timeframe": "1Day", "start": "2026-09-01", "end": "2026-09-30", "limit": 5},
    )
    bars = raw.get("bars", {}).get(sym, [])
    print("RAW envelope top-level keys: %s" % sorted(raw))
    if bars:
        print("RAW bar keys: %s" % sorted(bars[0]))
        print("RAW sample bar: %s" % json.dumps(bars[0]))
    print("RAW has impliedVolatility anywhere: %s"
          % ("impliedVolatility" in json.dumps(raw)))

    for label, s2, start, end in [
        ("stale 2026-01 expiry", occ("260102", "C", 700.0), "2026-01-01", "2026-09-30"),
        ("nonexistent strike 1234", occ("260918", "C", 1234.0), "2026-09-01", "2026-09-30"),
        ("long-history request", sym, "2024-02-01", "2026-09-30"),
    ]:
        try:
            ser = c.get_option_daily_bars([s2], start=start, end=end)
            nbars = sum(len(v) for v in ser.bars_by_symbol.values())
            days = sorted(b.t.date().isoformat() for v in ser.bars_by_symbol.values() for b in v)
            print("CASE %-26s 200 syms=%d bars=%d %s..%s"
                  % (label, len(ser), nbars, days[0] if days else "-", days[-1] if days else "-"))
        except AlpacaError as exc:
            print("CASE %-26s %s %s" % (label, getattr(exc, "status", "?"), str(exc)[:110]))


if __name__ == "__main__":
    main()