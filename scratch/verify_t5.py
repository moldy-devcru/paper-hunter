#!/usr/bin/env python
"""Verify T5 warmup against the REAL store, using shipped production readers.

No reimplementation: counts come from the store's own SQL and every IV rank comes from
``IvRankStore.iv_rank``, which is the exact call the checklist's T5 makes. The question this
answers is narrow and falsifiable: after the backfill, does an arm-C gate read ``ok`` or
does it still read ``warmup``?

    .venv/bin/python scratch/verify_t5.py [--db data/ivrank.db] [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from executor.iv_rank import (  # noqa: E402
    MIN_OBSERVATIONS,
    IvRankStore,
    moneyness_bucket,
    moneyness_tenor_key,
)


def _median_iv(store: IvRankStore, key: str) -> float:
    """Median stored IV under a key — a real current reading to score, not a typed-in one."""
    rows = [r.iv for r in store.history("SPY", key) if r.iv is not None]
    if not rows:
        return 0.18
    rows.sort()
    return rows[len(rows) // 2]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/ivrank.db")
    p.add_argument("--as-of", default=None)
    p.add_argument("--spot", type=float, default=None, help="spot for the synthetic current IV")
    p.add_argument("--current-iv", type=float, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()

    print(f"store        : {args.db}")
    print(f"as of        : {as_of.isoformat()}   (floor MIN_OBSERVATIONS={MIN_OBSERVATIONS})")

    # ---- 1. what is actually in there, straight from SQL ----------------------
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    print("\n--- per-key counts (all origins) ---")
    rows = con.execute(
        "SELECT tenor_key, source, origin, COUNT(*), MIN(as_of), MAX(as_of) "
        "FROM iv_observations GROUP BY tenor_key, source, origin "
        "ORDER BY COUNT(*) DESC"
    ).fetchall()
    if not rows:
        print("  (store is EMPTY — no observations at all)")
    for key, source, origin, n, lo, hi in rows:
        flag = "WARM" if n >= MIN_OBSERVATIONS else f"short by {MIN_OBSERVATIONS - n}"
        print(f"  {key:<28} {source:<20} {origin:<9} n={n:<4} {lo}..{hi}  {flag}")

    print("\n--- totals by source/origin ---")
    for src, org, n in con.execute(
        "SELECT source, origin, COUNT(*) FROM iv_observations GROUP BY source, origin"
    ):
        print(f"  {src:<20} {org:<9} {n}")
    total = con.execute("SELECT COUNT(*) FROM iv_observations").fetchone()[0]
    print(f"  TOTAL rows: {total}")

    print("\n--- distinct sessions per key (the thing MIN_OBSERVATIONS counts) ---")
    per_key = con.execute(
        "SELECT tenor_key, COUNT(DISTINCT as_of) FROM iv_observations GROUP BY tenor_key "
        "ORDER BY 2 DESC"
    ).fetchall()
    for key, n in per_key[:12]:
        flag = "WARM" if n >= MIN_OBSERVATIONS else f"short by {MIN_OBSERVATIONS - n}"
        print(f"  {key:<28} sessions={n:<4} {flag}")
    con.close()

    if not per_key:
        print("\nVERDICT: nothing to score — T5 stays PENDING for every arm.")
        return 1

    # ---- 2. score with the shipped reader -------------------------------------
    store = IvRankStore.open(args.db, readonly=True)
    try:
        # A representative current IV: the median stored IV under the busiest key, so the
        # rank is a real score against real history rather than a number typed in.
        key0 = per_key[0][0]
        current_iv = args.current_iv
        if current_iv is None:
            current_iv = args.current_iv or _median_iv(store, key0)
        print(f"\n--- production iv_rank() reads (current_iv={current_iv:.4f}) ---")
        for key, n in per_key[:12]:
            res = store.iv_rank(current_iv, "SPY", key, as_of=as_of)
            verdict = (
                f"OK   rank={res.rank:.1f}"
                if res.rank is not None
                else f"NOT DEFINED  status={res.status}"
            )
            print(f"  {key:<28} sessions={n:<4} {verdict}   {res.reason[:60]}")

        # ---- 3. the key an arm-C gate would read TODAY -------------------------
        # Built with the production key builders, from a mid-window spot, so this is the
        # string the gate constructs rather than one of ours by hand.
        if args.spot:
            for right in ("call", "put"):
                k = moneyness_tenor_key(
                    right=right,
                    dte=135,
                    moneyness_bucket=moneyness_bucket(args.spot, args.spot),
                    dte_bucket_days=7,
                )
                res = store.iv_rank(current_iv, "SPY", k, as_of=as_of)
                n = store.count_observations("SPY", k)
                verdict = (
                    f"OK   rank={res.rank:.1f}"
                    if res.rank is not None
                    else f"NOT DEFINED  status={res.status}"
                )
                print(f"\n  arm-C-shaped key {k!r}: n={n}  {verdict}")
    finally:
        store.close()

    print("\nConclusion: 'WARM'/'OK' above means T5 no longer reports PENDING for that key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())