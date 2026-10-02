#!/usr/bin/env python
"""Seed the IV-rank store from the free Cboe VIX daily CSV — WARMUP BACKFILL ONLY.

This is a stub entry point for the T5 IV-rank history the brief needs and no free
source provides (research note §1: Alpaca has no point-in-time IV history; Tradier and
Massive have none either). The real history is built by the executor's daily EOD chain
poll against the Alpaca indicative chain. This script exists so the pre-window period
has *something* to rank against, and so the switch from proxy to real is a documented,
auditable event rather than a silent one.

Usage
-----
    # 1. download the CSV yourself (the executor's data layer owns all network I/O;
    #    this script never fetches, so it stays runnable offline and in tests)
    curl -o /tmp/VIX_History.csv \\
      https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv

    # 2. seed it
    .venv/bin/python scripts/seed_ivrank.py /tmp/VIX_History.csv

    # 3. report
    .venv/bin/python scripts/seed_ivrank.py --report

Every seeded row carries ``is_proxy=1`` and lands under the ``VIX`` underlying, so it is
excluded from any real SPY tenor's IV rank unless a caller explicitly passes
``include_proxy=True`` *and* asks for the VIX tenor. When the real SPY series takes
over, the VIX rows stay in the database as history and simply stop being read — nothing
has to be deleted, so the seam is visible in the store rather than lost.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from executor.iv_rank import (  # noqa: E402 - path shim must precede the import
    DEFAULT_DB_PATH,
    VIX_CSV_URL,
    VIX_PROXY_TENOR,
    VIX_PROXY_UNDERLYING,
    IvRankStore,
    seed_vix_proxy,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="?", type=Path, help=f"VIX daily CSV from {VIX_CSV_URL}")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=365,
        help="only seed rows inside this window (default 365)",
    )
    parser.add_argument("--report", action="store_true", help="print what is stored and exit")
    args = parser.parse_args(argv)

    store = IvRankStore.open(args.db)
    try:
        if args.report or args.csv is None:
            observations = store.observations()
            real = [o for o in observations if not o.is_proxy]
            proxy = [o for o in observations if o.is_proxy]
            print(f"db: {args.db}")
            print(f"  real (non-proxy) observations: {len(real)}")
            print(f"  proxy observations:           {len(proxy)}")
            by_tenor: dict[str, int] = {}
            for obs in real:
                by_tenor[obs.tenor_key] = by_tenor.get(obs.tenor_key, 0) + 1
            if by_tenor:
                print("  real observations by tenor:")
                for tenor, count in sorted(by_tenor.items(), key=lambda kv: -kv[1])[:20]:
                    flag = "  <- defined" if count >= store.min_observations else "  (warming up)"
                    print(f"    {count:5d}  {tenor}{flag}")
            else:
                print(
                    "  no real IV observations yet — T5 reads PENDING until the EOD chain "
                    f"poll has run {store.min_observations}+ times for a tenor"
                )
            print(
                f"  proxy tenor: {VIX_PROXY_TENOR} under {VIX_PROXY_UNDERLYING} "
                "(excluded from real IV ranks)"
            )
            return 0

        if not args.csv.is_file():
            parser.error(f"CSV not found: {args.csv} (download it from {VIX_CSV_URL})")
        written = seed_vix_proxy(
            store, args.csv.read_text(encoding="utf-8"), lookback_days=args.lookback_days
        )
        print(f"seeded {written} proxy observation(s) into {args.db}")
        print(
            "reminder: these are VIX rows flagged is_proxy=1. They warm the store up; "
            "they are NOT SPY IV rank and will never be read as one."
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())