#!/usr/bin/env python
"""Can T5 arm C actually warm under the FROZEN rules? Answered with production code, no API.

The live backfill is blocked by the options-bars throttle (see
``docs/reviews/2026-10-03-iv-backfill-depth-probe.md``), so the warmup question could not be
answered by observation. It can still be answered by ARITHMETIC, and that is worth doing
because the answer decides whether ``docs/ratification.md`` (e)/(f) should be rewritten at
all — a rewrite justified by a guess is exactly the failure mode this experiment exists to
avoid.

This uses the shipped key builders (:func:`plan_sessions`, :func:`select_expiry`,
:func:`moneyness_tenor_key`) so it cannot drift from what the gate reads.

    .venv/bin/python scratch/iv_backfill_warmup_arithmetic.py
"""

from __future__ import annotations

import collections
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from executor.iv_backfill import plan_sessions  # noqa: E402
from executor.iv_rank import (  # noqa: E402
    DTE_BUCKET_DAYS,
    MIN_OBSERVATIONS,
    moneyness_bucket,
    moneyness_tenor_key,
)

ARM_C_DTE = (90, 180)


def synthetic_closes(start: dt.date, end: dt.date) -> dict[dt.date, float]:
    """Weekday-only closes on a slow drift.

    The exact price path does not matter for this question: what matters is that spot MOVES,
    so the moneyness bucket is exercised rather than pinned by a constant. A constant would
    silently understate the key count.
    """
    closes: dict[dt.date, float] = {}
    day = start
    while day <= end:
        if day.weekday() < 5:
            closes[day] = 640.0 + ((day - start).days % 80) * 1.2
        day += dt.timedelta(days=1)
    return closes


def key_counts(start: dt.date, end: dt.date) -> tuple[list, collections.Counter]:
    plans = plan_sessions(
        synthetic_closes(start, end),
        underlying="SPY",
        dte_min=ARM_C_DTE[0],
        dte_max=ARM_C_DTE[1],
        start=start,
        end=end,
    )
    counts: collections.Counter = collections.Counter()
    for plan in plans:
        # The gate reads NEAREST-TO-SPOT (soak.arm_contract_for_band), so that is what is
        # scored here. Recording some other strike would answer a different question.
        strike = min(plan.strikes, key=lambda s: abs(s - plan.spot))
        dte = (plan.expiry - plan.session).days
        for right in ("call", "put"):
            counts[
                moneyness_tenor_key(
                    right=right,
                    dte=dte,
                    moneyness_bucket=moneyness_bucket(strike, plan.spot),
                    dte_bucket_days=DTE_BUCKET_DAYS,
                )
            ] += 1
    return plans, counts


def main() -> int:
    end = dt.date(2026, 10, 3)
    print(f"DTE_BUCKET_DAYS={DTE_BUCKET_DAYS}  MIN_OBSERVATIONS={MIN_OBSERVATIONS}")
    print(f"arm C band = {ARM_C_DTE} DTE\n")

    for label, days in (("frozen 1y lookback", 365), ("max feed depth (~2.7y)", 969)):
        start = end - dt.timedelta(days=days)
        plans, counts = key_counts(start, end)
        best = max(counts.values()) if counts else 0
        print(f"--- {label}: {start} .. {end}")
        print(f"    sessions={len(plans)}  distinct keys={len(counts)}")
        for key, n in counts.most_common():
            flag = "WARM" if n >= MIN_OBSERVATIONS else f"short by {MIN_OBSERVATIONS - n}"
            print(f"      {key:<24} {n:>4}  {flag}")
        print(f"    best key = {best} -> arm C T5 {'WARMS' if best >= MIN_OBSERVATIONS else 'STAYS PENDING'}")

    # Why only one DTE bucket appears is load-bearing, so say it rather than let a reader
    # assume the arithmetic is a typo.
    print(
        "\nNote: select_expiry picks the SHORTEST-dated expiry inside the band, so DTE pins\n"
        "near the band's lower edge (~91) instead of sweeping 90-180. Every session therefore\n"
        "lands in ONE DTE bucket and accumulates one observation per session into it. That is\n"
        "why a single key reaches the floor and why widening the ladder buys nothing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())