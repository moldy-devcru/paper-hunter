"""Feasibility arithmetic for T5 IV backfill (docs/reviews/2026-10-03-iv-backfill-feasibility.md).

Research scratch only. Uses the repo's own strike_bucket/dte_tenor_key so the numbers
cannot drift from the frozen key semantics. SPY daily closes come from a Yahoo chart
pull (scratch/spy_closes.json) because no Alpaca credential is reachable here.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor.iv_rank import DTE_BUCKET_DAYS, STRIKE_BUCKET_SIZE, dte_tenor_key, strike_bucket

DATA = Path(__file__).resolve().parent / "spy_closes.json"
MIN_OBS = 60


def load_sessions() -> list[tuple[dt.date, float]]:
    raw = json.loads(DATA.read_text())
    rows = []
    for stamp, close in raw:
        if close is None:
            continue
        rows.append((dt.datetime.utcfromtimestamp(stamp).date(), round(float(close), 2)))
    return rows


def atm_strike(spot: float, step: float = 1.0) -> float:
    """Nearest listed SPY strike (SPY lists $1 strikes)."""
    return math.floor(spot / step + 0.5) * step


def report(label: str, sessions: list[tuple[dt.date, float]]) -> None:
    # optimistic bound: same DTE bucket every session, strike bucket = ATM bucket
    fixed_dte = 126
    per_key: dict[str, int] = defaultdict(int)
    for day, spot in sessions:
        key = dte_tenor_key(
            dte=fixed_dte, right="call", strike_bucket=strike_bucket(atm_strike(spot))
        )
        per_key[key] += 1
    best = max(per_key.values())
    print(
        f"{label}: {len(sessions)} sessions | distinct strike buckets={len(per_key)} "
        f"| max obs/key={best} | keys>={MIN_OBS}={sum(1 for v in per_key.values() if v >= MIN_OBS)} "
        f"| keys>={MIN_OBS//2}={sum(1 for v in per_key.values() if v >= MIN_OBS // 2)}"
    )
    return per_key


def fragmented(sessions: list[tuple[dt.date, float]], n_dte_buckets: int) -> None:
    """Realistic case: the gate's DTE bucket is decided by a chain-order tie-break we
    cannot reproduce offline, so treat the bucket as spread over the band."""
    per_key: dict[str, int] = defaultdict(int)
    band = [90 + DTE_BUCKET_DAYS * i for i in range(13)]  # 90-180 band, floored
    for i, (day, spot) in enumerate(sessions):
        dte = band[i % len(band)]
        per_key[dte_tenor_key(dte=dte, right="call", strike_bucket=strike_bucket(atm_strike(spot)))] += 1
    best = max(per_key.values())
    print(
        f"  spread over {len(band)} DTE buckets: distinct keys={len(per_key)} "
        f"max obs/key={best} keys>={MIN_OBS}={sum(1 for v in per_key.values() if v >= MIN_OBS)}"
    )


def bucket_width_sensitivity(sessions: list[tuple[dt.date, float]]) -> None:
    print("  strike-bucket width sensitivity (same DTE bucket, max obs/key):")
    for size in (5.0, 10.0, 20.0, 25.0, 50.0, 100.0):
        per_key: Counter[float] = Counter(strike_bucket(atm_strike(s), size) for _, s in sessions)
        best = max(per_key.values())
        print(
            f"    ${size:>5.0f} bucket -> {len(per_key):>4} keys, max obs/key={best:>4}, "
            f"keys>={MIN_OBS}={sum(1 for v in per_key.values() if v >= MIN_OBS)}"
        )


def spot_range(sessions: list[tuple[dt.date, float]]) -> None:
    lo = min(sessions, key=lambda r: r[1])
    hi = max(sessions, key=lambda r: r[1])
    print(f"  spot range: {lo[1]} ({lo[0]}) .. {hi[1]} ({hi[0]})")
    buckets = int((hi[1] - lo[1]) / STRIKE_BUCKET_SIZE) + 1
    print(f"  $5 buckets walked over the sample: ~{buckets}")


def main() -> None:
    rows = load_sessions()
    print(f"loaded {len(rows)} sessions {rows[0][0]}..{rows[-1][0]}\n")
    for label, start in (
        ("2023-01-01", dt.date(2023, 1, 1)),
        ("2024-02-01 (Alpaca floor)", dt.date(2024, 2, 1)),
    ):
        window = [r for r in rows if r[0] >= start]
        print(f"--- from {label} ---")
        spot_range(window)
        report(label, window)
        fragmented(window, 13)
        bucket_width_sensitivity(window)
        print()


if __name__ == "__main__":
    main()