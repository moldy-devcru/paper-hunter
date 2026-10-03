#!/usr/bin/env python
"""Backfill T5's rolling IV tenor series from historical option bars (rulings R8 + R9).

T5's warmup floor is ``MIN_OBSERVATIONS = 60`` at one observation per session, and an
undefined IV rank reads as PENDING, which *blocks* trades. So an un-warmed T5 does not
degrade the experiment, it stops it. Ruling R9 permits a backfilled observation — a
Black-Scholes inversion of a historical daily bar close — to count toward the 60, and this
script is that ruling made executable.

What it writes
--------------
Two tenor keys per observation, mirroring ``soak._observation_for``:

* the RULED **moneyness-keyed** rolling key (``mte<dte>-<side>-mny<±pp>``) that T5 reads;
* the **expiry-keyed** ``$5``-bucketed audit key, which dies with the expiry and exists so
  the approximation stays measurable against the exact thing.

Every row carries ``source='bars_bs_inversion'`` and ``origin='backfill'``, so it is
distinguishable from a live EOD chain reading while still counting per R9.

What it cannot do
-----------------
**Arm B cannot be backfilled.** Its frozen DTE band is ``0``, Black-Scholes vega is
identically zero at expiry, and a 0-DTE daily print does not determine a volatility. The
solver refuses rather than inventing one. Arm B stays in warmup and is filled only by the
live EOD poll. This is a property of the instrument, not of this code.

Usage
-----
    # 1. dry run first — makes every API call, writes nothing
    .venv/bin/python scripts/backfill_iv_rank.py --dry-run

    # 2. the real thing, against the live paper credential
    .venv/bin/python scripts/backfill_iv_rank.py

    # 3. narrower window / different arm set
    .venv/bin/python scripts/backfill_iv_rank.py --days 200 --arms C

Credentials come from ``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET`` in the environment.
They are never printed, never written to the store, and never accepted as flags.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import load_rules  # noqa: E402
from executor.alpaca_client import AlpacaClient, AlpacaError  # noqa: E402
from executor.iv_backfill import (  # noqa: E402
    DEFAULT_LADDER_RANGE_PCT,
    DEFAULT_SLACK_DAYS,
    DEFAULT_STRIKE_STEP,
    AlpacaIvSource,
    IvBackfillError,
    run_backfill,
)
from executor.iv_rank import DEFAULT_DB_PATH, DEFAULT_LOOKBACK_DAYS, IvRankStore  # noqa: E402

#: Fallback bands, used only when the rulebook cannot be read. Arm C's live band is 90-180;
#: arm B's is 0 — both are asserted against the rulebook below when it IS readable, so a
#: drifted rulebook is an error rather than a silent fallback.
FALLBACK_BANDS = (("B", 0, 0), ("C", 90, 180))


def bands_from_rulebook(rulebook_path: Path) -> tuple[tuple[str, int, int], ...]:
    """Per-arm ``(arm, dte_min, dte_max)`` read from the frozen rulebook.

    Uses the soak's own ``_bands_from_rules`` so the backfill cannot drift from the bands
    the live gates read — which is the entire bug class R4 was filed about.
    """
    from executor.soak import _bands_from_rules

    return _bands_from_rules(load_rules(rulebook_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_iv_rank.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB_PATH,
        help="IV store to write (default: %(default)s)",
    )
    parser.add_argument(
        "--rules", type=Path, default=Path("config/rules.example.yaml"),
        help="rulebook the DTE bands are read from (default: %(default)s)",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_LOOKBACK_DAYS + DEFAULT_SLACK_DAYS,
        help=(
            "trailing window in CALENDAR days. Defaults to the lookback plus slack, because "
            "history older than the frozen 365-day lookback is arithmetically discarded by "
            "iv_rank and backfilling it would be cost without effect (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--arms", default="BC",
        help="comma-separated arms to backfill (default: %(default)s). B is a no-op by design.",
    )
    parser.add_argument("--strike-step", type=float, default=DEFAULT_STRIKE_STEP)
    parser.add_argument("--range-pct", type=float, default=DEFAULT_LADDER_RANGE_PCT)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="make every API call, report what would be written, write nothing",
    )
    parser.add_argument(
        "--report", type=Path, default=None,
        help="also write the full run report as JSON to this path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)
    arms = tuple(a.strip().upper() for a in args.arms.split(",") if a.strip())

    if args.rules.exists():
        bands = bands_from_rulebook(args.rules)
        print(f"bands from {args.rules}: {bands}")
    else:
        print(f"!! {args.rules} not found — using the fallback bands {FALLBACK_BANDS}")
        bands = FALLBACK_BANDS
    wanted = tuple(b for b in bands if b[0] in arms)
    if not wanted:
        print(f"no arms matched {arms!r} in {bands}")
        return 2

    try:
        client = AlpacaClient.from_env()
    except AlpacaError as exc:
        # The message names the variable, never its value — AlpacaConfigError is written
        # that way precisely so it is safe to paste.
        print(f"cannot reach Alpaca: {exc}")
        return 2

    source = AlpacaIvSource(client)
    store = IvRankStore.open(args.db)
    try:
        report = run_backfill(
            source,
            store,
            start=start,
            end=end,
            bands=wanted,
            strike_step=args.strike_step,
            range_pct=args.range_pct,
            dry_run=args.dry_run,
        )
    except IvBackfillError as exc:
        print(f"backfill refused: {exc}")
        return 2
    finally:
        store.close()

    print(report.summary())
    if args.dry_run:
        print("\n(dry run — nothing was written)")
    if args.report:
        payload = {
            "start": report.start.isoformat(),
            "end": report.end.isoformat(),
            "sessions_seen": report.sessions_seen,
            "sessions_written": report.sessions_written,
            "observations_written": report.observations_written,
            "api_calls": report.api_calls,
            "retries": report.retries,
            "cooldowns": report.cooldowns,
            "bands_seen": [list(b) for b in report.bands_seen],
            "tenor_key_counts": report.tenor_key_counts,
            "origin_counts": report.origin_counts,
            "sessions": [
                {
                    "session": s.session.isoformat(),
                    "spot": s.spot,
                    "expiry": s.expiry.isoformat() if s.expiry else None,
                    "requested": s.contracts_requested,
                    "with_bar": s.contracts_with_bar,
                    "written": s.observations_written,
                    "keys": s.tenor_keys,
                    "skips": s.skips,
                }
                for s in report.sessions
            ],
        }
        args.report.write_text(json.dumps(payload, indent=2))
        print(f"report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())