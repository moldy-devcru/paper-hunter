"""Trailing-lookback + moneyness arithmetic. Research scratch.

The 1y lookback (`iv_rank_lookback: "1y"`, DEFAULT_LOOKBACK_DAYS=365) is the binding
constraint, not the availability of history: `iv_rank` gates warmup on
`len(history(lookback_days=365)) < MIN_OBSERVATIONS`. So obs older than a year cannot
help even if backfilled. This measures what each candidate key schema yields inside a
trailing 365-day window ending at the November window's open.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor.iv_rank import DTE_BUCKET_DAYS, MIN_OBSERVATIONS, dte_tenor_key, strike_bucket

ROWS = [
    (dt.datetime.fromtimestamp(t, dt.UTC).date(), round(float(c), 2))
    for t, c in json.loads((Path(__file__).resolve().parent / "spy_closes.json").read_text())
    if c is not None
]
WINDOW_OPEN = dt.date(2026, 11, 2)
ARM_C_BAND = [90 + DTE_BUCKET_DAYS * i for i in range(13)]  # 90-180, floored to 7


def atm(spot: float) -> float:
    return math.floor(spot + 0.5)


def trailing(end: dt.date, days: int = 365) -> list[tuple[dt.date, float]]:
    return [r for r in ROWS if end - dt.timedelta(days=days) <= r[0] <= end]


def report(label: str, per_key: dict[str, int]) -> None:
    best = max(per_key.values()) if per_key else 0
    reached = sum(1 for v in per_key.values() if v >= MIN_OBSERVATIONS)
    top = sorted(per_key.items(), key=lambda kv: -kv[1])[:3]
    print(
        f"  {label:<46} keys={len(per_key):>4} max={best:>3} "
        f"keys>={MIN_OBSERVATIONS}={reached}  top={[(k, v) for k, v in top]}"
    )


def scenario_dte_fixed(w, dte=126, size=5.0):
    per = defaultdict(int)
    for _, spot in w:
        per[dte_tenor_key(dte=dte, right="call", strike_bucket=strike_bucket(atm(spot), size))] += 1
    return per


def scenario_dte_rotating(w, size=5.0):
    """Arm C band spans 13 DTE buckets; nearest-to-spot across ~20 expiries is a near-tie,
    so the winning bucket moves. Model the bucket as cycling deterministically."""
    per = defaultdict(int)
    for i, (_, spot) in enumerate(w):
        per[
            dte_tenor_key(
                dte=ARM_C_BAND[i % len(ARM_C_BAND)], right="call",
                strike_bucket=strike_bucket(atm(spot), size),
            )
        ] += 1
    return per


def scenario_moneyness(w, width_pct=2.0, dte=126):
    """Candidate v2 key: bucket on strike/spot in percent, not absolute dollars."""
    per = defaultdict(int)
    for _, spot in w:
        m = 100.0 * (atm(spot) / spot - 1.0)
        b = math.floor(m / width_pct + 0.5) * width_pct
        per[f"mny{b:.1f}-call-dte{dte}"] += 1
    return per


def scenario_moneyness_rotating(w, width_pct=2.0):
    per = defaultdict(int)
    for i, (_, spot) in enumerate(w):
        m = 100.0 * (atm(spot) / spot - 1.0)
        b = math.floor(m / width_pct + 0.5) * width_pct
        per[f"mny{b:.1f}-call-dte{ARM_C_BAND[i % len(ARM_C_BAND)]}"] += 1
    return per


def main() -> None:
    print(f"MIN_OBSERVATIONS={MIN_OBSERVATIONS}, lookback=365d, as_of={WINDOW_OPEN} (window open)\n")
    w365 = trailing(WINDOW_OPEN)
    print(f"trailing 365d to window open: {len(w365)} sessions "
          f"({w365[0][0]}..{w365[-1][0]}), SPY {min(s for _, s in w365)}..{max(s for _, s in w365)}\n")

    print("CURRENT frozen key (absolute $5 strike bucket, per `dte_tenor_key`):")
    report("dte bucket fixed at 126 (best case)", scenario_dte_fixed(w365))
    report("dte bucket rotating over arm C's 13 buckets", scenario_dte_rotating(w365))
    print()
    print("Widening the ABSOLUTE strike bucket (same trailing 365d):")
    for size in (5.0, 10.0, 20.0, 25.0, 50.0):
        report(f"  ${size:.0f} bucket, dte fixed 126", scenario_dte_fixed(w365, size=size))
    print()
    print("Candidate v2: MONEYNESS-RELATIVE bucket (the queued ruling direction):")
    for width in (1.0, 2.0, 2.5, 5.0):
        report(f"  {width}% moneyness band, dte fixed", scenario_moneyness(w365, width))
    print()
    report("  2% moneyness + dte rotating (full rotation)", scenario_moneyness_rotating(w365, 2.0))
    report("  2% moneyness + dte rotating, 1y lookback only",
           scenario_moneyness_rotating(w365, 2.0))

    print("\nSame scenarios on the FULL available history (2024-02-01 floor, lookback ignored):")
    full = [r for r in ROWS if r[0] >= dt.date(2024, 2, 1)]
    print(f"  {len(full)} sessions")
    report("  $5 absolute, dte fixed", scenario_dte_fixed(full))
    report("  2% moneyness, dte fixed", scenario_moneyness(full, 2.0))


if __name__ == "__main__":
    main()