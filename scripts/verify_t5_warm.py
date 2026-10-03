#!/usr/bin/env python
"""Is T5 warm yet? A per-key, per-arm read of the IV store through the REAL reader.

Why this exists. T5 is a **blocking** condition: an undefined IV rank reads as PENDING,
and PENDING blocks rather than passes, so a cold store does not degrade the experiment —
it stops it. That makes "is it warm?" the single question the rehearsal's NO-GO item 9
turns on, and it is a question that is very easy to answer wrongly.

**Answer it with row counts and you will get it wrong.** The store's uniqueness key is
``(underlying, as_of, tenor_key, source)``, so 28 rows across 28 different keys is one
observation each and not a distribution — that is precisely the shape the deployed store
is in right now. A row count says "28 observations, nearly half of 60." The per-key
count says what T5 actually reads. This script only ever reports the second.

**It uses the production reader.** Every number here comes from
``executor.iv_rank.IvRankStore`` — ``history``, ``latest``, ``iv_rank`` — under the same
defaults T5 runs with: the same ``MIN_OBSERVATIONS``, the same lookback, proxy rows
excluded, and per ruling **R9 both ``live`` and ``backfill`` origins counted** (which is
what ``origin=None`` means). Nothing here re-implements a query, so a change to the
store's semantics moves this script with it rather than away from it.

Usage
-----
    # the everyday question: is the store warm?
    .venv/bin/python scripts/verify_t5_warm.py --db data/ivrank.db

    # also name the key a live ATM contract would read at a known spot
    .venv/bin/python scripts/verify_t5_warm.py --db data/ivrank.db --spot 770.00

    # machine-readable, for a cron or a CI check
    .venv/bin/python scripts/verify_t5_warm.py --db data/ivrank.db --json /tmp/t5.json

Exit code is **0 only when every checklist-bearing arm is warm**. A cold store exits 1
by design: this is a monitor, and a monitor that returns success on "cannot tell" is a
monitor nobody reads. ``NOT WARM`` immediately after a backfill is the **expected** first
result, not a failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import load_rules  # noqa: E402
from executor.iv_rank import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_LOOKBACK_DAYS,
    MIN_OBSERVATIONS,
    STRIKE_BUCKET_SIZE,
    IvRankStore,
    moneyness_bucket,
    moneyness_tenor_key,
)

#: The ruled key spelling: ``mte<dte>-<side>-mny<bucket>`` (rulings R4 + R8). Matched
#: rather than rebuilt from the key function, because this script's whole job is to
#: identify what is ALREADY in a store written by some other version of the code.
KEY_RE = re.compile(r"^mte(?P<dte>\d+)-(?P<side>call|put)-mny(?P<mny>[+-][\d.]+)$")

#: Prefixes that identify rows the ruled reader will never look up. R7 and R8's
#: disposition is "orphaned by design" — no migration, no deletion — which is only safe
#: if somebody can still SEE that the orphans are there. Printing them is that.
LEGACY_LABELS = {
    "dte": "dte<dollars> (raw or 7-day DTE, absolute $5 strike)",
    "expiry": "expiry-keyed (absolute $5 strike audit series)",
    "other": "unrecognised scheme",
}


def _legacy_label(key: str) -> str:
    """Classify a non-ruled key by its SCHEME, not by its value.

    Grouping by the raw prefix would report fourteen separate schemes for fourteen
    expiries, because an expiry-keyed row's prefix is its calendar date. The point of
    this line is "how much of this store is unreadable", which is one number per
    scheme, not one per row.
    """
    prefix = key.split("-", 1)[0]
    if prefix.startswith("dte"):
        return "dte"
    if re.fullmatch(r"\d{8}", prefix):
        return "expiry"
    return "other"


@dataclass
class KeyRow:
    """One tenor key, measured exactly the way T5 measures it."""

    key: str
    dte_bucket: int
    side: str
    moneyness: float
    observations: int
    status: str
    reason: str

    @property
    def warm(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, object]:
        return {
            "tenor_key": self.key,
            "dte_bucket": self.dte_bucket,
            "side": self.side,
            "moneyness_pct": self.moneyness,
            "observations": self.observations,
            "status": self.status,
            "warm": self.warm,
            "reason": self.reason,
        }


@dataclass
class ArmRow:
    arm: str
    dte_min: int
    dte_max: int
    keys: list[KeyRow] = field(default_factory=list)

    @property
    def best(self) -> KeyRow | None:
        """The warmest key in the band — what T5 would actually be reading tomorrow."""
        return max(self.keys, key=lambda k: k.observations, default=None)

    @property
    def status(self) -> str:
        return "PASS" if self.best is not None and self.best.warm else "PENDING"

    def to_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "dte_band": [self.dte_min, self.dte_max],
            "status": self.status,
            "best_key": self.best.key if self.best else None,
            "best_observations": self.best.observations if self.best else 0,
            "keys_in_band": [k.to_dict() for k in self.keys],
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_t5_warm.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB_PATH,
        help="IV store to read (default: %(default)s)",
    )
    parser.add_argument(
        "--rules", type=Path, default=Path("config/rules.example.yaml"),
        help="rulebook the per-arm DTE bands are read from (default: %(default)s)",
    )
    parser.add_argument(
        "--underlying", default="SPY",
        help="underlying whose keys to check (default: %(default)s)",
    )
    parser.add_argument(
        "--spot", type=float, default=None,
        help=(
            "also report the exact key an ATM contract would read at this spot, and "
            "its count. Stronger evidence than 'some key in the band is warm'"
        ),
    )
    parser.add_argument(
        "--as-of", default=None,
        help="evaluate the lookback window as of this date (YYYY-MM-DD). Default: today",
    )
    parser.add_argument(
        "--min-observations", type=int, default=MIN_OBSERVATIONS,
        help=(
            "override the warmup floor for a what-if. The VERDICT always uses the "
            "production MIN_OBSERVATIONS so an override cannot make a cold store look "
            "warm (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--json", type=Path, default=None,
        help="also write the full result as JSON to this path",
    )
    return parser


def _bands(rules_path: Path) -> tuple[tuple[str, int, int], ...]:
    """Per-arm ``(arm, dte_min, dte_max)`` from the frozen rulebook.

    Read through the soak's own ``_bands_from_rules``, the same helper
    ``scripts/backfill_iv_rank.py`` uses, so the bands this script checks against are the
    bands the writer used. Re-deriving them here is exactly the drift R4 was filed about.
    """
    from executor.soak import _bands_from_rules

    return _bands_from_rules(load_rules(rules_path))


def _measure(store: IvRankStore, underlying: str, key: str, *, as_of, floor: int):
    """Measure one key through the production reader and return its rank result.

    ``current_iv`` is the key's own latest stored IV. That is deliberate and it is not
    what the gate will read: the gate reads a LIVE chain print, which may not be in the
    store yet. The VALUE of the rank is therefore not the answer here — only the STATUS
    is, and the status (warmup / no_history / empty_window / ok) depends purely on how
    many observations the window holds, not on what the reading is. Supplying the latest
    IV is what lets the production code reach that status instead of short-circuiting on
    "no current IV supplied".
    """
    latest = store.latest(underlying, key)
    if latest is None:
        # No rows at all: iv_rank says no_history, which is the honest reason to give.
        return store.iv_rank(
            None, underlying, key, min_observations=floor, as_of=as_of
        )
    return store.iv_rank(
        latest.iv, underlying, key, min_observations=floor, as_of=as_of
    )


def collect(
    store: IvRankStore,
    underlying: str,
    bands: tuple[tuple[str, int, int], ...],
    *,
    as_of,
    floor: int,
) -> tuple[list[KeyRow], dict[str, int]]:
    """Measure every RULED-schema key in the store, plus a count of the orphans.

    Enumeration goes through ``store.observations()`` — the production accessor — rather
    than SQL, so the set of keys this reports is the set the store itself can produce.
    """
    rows: list[KeyRow] = []
    legacy: dict[str, int] = {}
    seen: set[str] = set()

    for obs in store.observations(underlying):
        if obs.tenor_key in seen:
            continue
        seen.add(obs.tenor_key)
        match = KEY_RE.match(obs.tenor_key)
        if match is None:
            label = _legacy_label(obs.tenor_key)
            legacy[label] = legacy.get(label, 0) + 1
            continue
        result = _measure(store, underlying, obs.tenor_key, as_of=as_of, floor=floor)
        rows.append(
            KeyRow(
                key=obs.tenor_key,
                dte_bucket=int(match.group("dte")),
                side=match.group("side"),
                moneyness=float(match.group("mny")),
                observations=result.observations,
                status=result.status,
                reason=result.reason,
            )
        )

    rows.sort(key=lambda r: (-r.observations, r.key))
    return rows, legacy


def arms_from(rows: list[KeyRow], bands) -> list[ArmRow]:
    """Place each measured key in the arm whose DTE band contains its bucket."""
    out = [ArmRow(arm=a, dte_min=lo, dte_max=hi) for a, lo, hi in bands]
    for row in rows:
        for arm in out:
            if arm.dte_min <= row.dte_bucket <= arm.dte_max:
                arm.keys.append(row)
    return out


def spot_key(spot: float, right: str, dte: int, *, dte_bucket_days: int = 7) -> str:
    """The ruled key an ATM contract at ``spot`` would read.

    Built with the PRODUCTION key function, so this cannot drift from what the plan and
    the soak write. ``dte`` is the caller���s guess at the arm's tenor — this is a check on
    the store, not a substitute for reading a live chain.
    """
    return moneyness_tenor_key(
        dte=dte,
        right=right,
        moneyness_bucket=moneyness_bucket(spot, spot),  # ATM => bucket 0.00 by definition
        dte_bucket_days=dte_bucket_days,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = args.as_of

    if not args.db.exists():
        print(f"IV store not found: {args.db}")
        print("VERDICT: NOT WARM — no store to read. Nothing has been recorded yet.")
        return 1

    bands = _bands(args.rules)
    store = IvRankStore.open(args.db)
    try:
        rows, legacy = collect(
            store, args.underlying, bands, as_of=as_of, floor=args.min_observations
        )
        arms = arms_from(rows, bands)

        # ---- per-key table ----
        print(f"IV store      : {args.db}")
        print(f"rulebook      : {args.rules}  (bands {bands})")
        print(
            f"floor         : MIN_OBSERVATIONS={MIN_OBSERVATIONS}, "
            f"lookback={DEFAULT_LOOKBACK_DAYS}d, proxy rows excluded, "
            f"live+backfill counted (R9)"
        )
        if args.min_observations != MIN_OBSERVATIONS:
            print(f"  !! --min-observations={args.min_observations} is a WHAT-IF override; "
                  f"the verdict below still uses {MIN_OBSERVATIONS}")
        print()

        if not rows:
            print("per-key observations (ruled moneyness schema):")
            print("  (none — the store holds no mte…/mny… keys at all)")
        else:
            print("per-key observations (ruled moneyness schema, T5's own reading):")
            print(f"  {'key':<28} {'obs':>5}  {'/floor':>7}  status")
            for row in rows:
                print(
                    f"  {row.key:<28} {row.observations:>5}  "
                    f"{row.observations:>3}/{MIN_OBSERVATIONS:<3}  {row.status}"
                )
        print()

        if legacy:
            total = sum(legacy.values())
            print(f"legacy keys   : {total} key(s) under retired schemes, unreadable by "
                  f"the ruled reader:")
            for label, count in sorted(legacy.items(), key=lambda kv: -kv[1]):
                print(f"                 {count:>4}  {LEGACY_LABELS.get(label, label)}")
            print(
                "                 Orphaned by design (R7/R8) — never re-keyed, never "
                "deleted, never read"
            )
            print(
                "                 by the ruled reader. They do NOT count toward the "
                "floor above and are not a"
            )
            print("                 warmup head start.")
            print()

        # ---- per-arm T5 status ----
        print("per-arm T5 status (best key inside the arm's DTE band):")
        print(f"  {'arm':<4} {'DTE band':<10} {'keys':>4} {'best key':<28} {'obs':>5}  T5")
        for arm in arms:
            best = arm.best
            print(
                f"  {arm.arm:<4} {f'{arm.dte_min}-{arm.dte_max}':<10} "
                f"{len(arm.keys):>4} {(best.key if best else '-'):<28} "
                f"{(best.observations if best else 0):>5}  {arm.status}"
            )
        print()

        # ---- the live-key spot check ----
        spot_result = None
        if args.spot is not None:
            print(f"spot check    : --spot {args.spot}")
            for arm in arms:
                dte = max(arm.dte_min, 0)
                key = spot_key(args.spot, "call", dte)
                result = _measure(
                    store, args.underlying, key, as_of=as_of, floor=MIN_OBSERVATIONS
                )
                spot_result = {
                    "arm": arm.arm,
                    "key": key,
                    "observations": result.observations,
                    "status": result.status,
                }
                mark = "PASS" if result.status == "ok" else "PENDING"
                print(
                    f"  {arm.arm} at {dte:>3} DTE -> {key:<28} "
                    f"{result.observations:>3}/{MIN_OBSERVATIONS}  {result.status}  {mark}"
                )
            print()

        # ---- verdict ----
        pending = [a for a in arms if a.status != "PASS"]
        warm = not pending
        verdict = "WARM" if warm else "NOT WARM"
        if warm:
            print(f"VERDICT: {verdict} — every checklist arm has a key at or above "
                  f"{MIN_OBSERVATIONS} observations.")
        else:
            detail = ", ".join(
                f"{a.arm} "
                f"({a.best.observations if a.best else 0}/{MIN_OBSERVATIONS}"
                + (f" under {a.best.key}" if a.best else ", no key in band")
                + ")"
                for a in pending
            )
            print(f"VERDICT: {verdict} — {detail}")
            if not rows:
                print(
                    "  No ruled-schema keys exist yet, so nothing has been recorded in a "
                    "form T5 can read."
                )
                print(
                    "  Run scripts/backfill_iv_rank.py first — the live EOD soak alone "
                    "cannot reach the floor in time (see docs/ratification.md R8/R9)."
                )

        if args.json:
            args.json.write_text(
                json.dumps(
                    {
                        "db": str(args.db),
                        "underlying": args.underlying,
                        "as_of": as_of,
                        "min_observations": MIN_OBSERVATIONS,
                        "lookback_days": DEFAULT_LOOKBACK_DAYS,
                        "strike_bucket_size": STRIKE_BUCKET_SIZE,
                        "bands": [list(b) for b in bands],
                        "keys": [r.to_dict() for r in rows],
                        "legacy_key_counts": legacy,
                        "arms": [a.to_dict() for a in arms],
                        "spot_check": spot_result,
                        "warm": warm,
                    },
                    indent=2,
                )
            )
            print(f"\n(report written to {args.json})")

        return 0 if warm else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())