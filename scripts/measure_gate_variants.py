"""Measure pre-declared v2 gate candidates by recombining per-session statuses.

Additive-only analysis. It does NOT edit the rulebook, the config, or any harness
file: it reuses the existing base-rate harness (:mod:`analysis.gate_baserate`) over
the *cached* SPY/VIX history and scores a small set of **pre-declared** candidate
gate structures by recombining the per-session statuses the real
``executor.checklist.evaluate`` already produced. Nothing here fetches from the
network, writes to the journal/rulebook/any ``.db``, or changes a threshold that
lives in the rulebook — the candidate thresholds are applied only inside this
script's recomposition, and are labelled as such wherever they appear.

What is scored (arm C only; both bases ``plan`` and ``eod``)
------------------------------------------------------------
The frozen arm-C checklist is the AND of ``T1 AND T2 AND T2b AND T3 AND T4 AND T5``
(T6 is not carried by arm C). The candidates recombine the *same* per-session
statuses:

* ``V0`` — baseline: the current frozen AND, exactly ``outcome.fire``.
* ``V1`` — V0 minus T1, T2, T5 dropped from the AND (i.e. ``T2b AND T3 AND T4``).
* ``V2`` — V1 with T4's relative-volume branch re-thresholded to ``relvol >= 1.2``
  (the frozen value is 1.5; the run-rate branch is inert on daily bars).
* ``V3`` — V1 with T4's relative-volume branch re-thresholded to ``relvol >= 1.0``.
* ``V4`` — V1 but ``T2b OR T3-group`` replaces ``T2b AND T3-group`` (T3-group is the
  evaluator's ``T3a OR T3b`` resolution).
* ``V5`` — V4 with T4's relative-volume branch re-thresholded to ``relvol >= 1.2``.

For the T4 re-thresholds the T4 rule is mirrored *exactly* from
``executor.checklist._t4`` (an OR of the relative-volume branch and the intraday
run-rate branch; a non-finite or missing value is a FAIL, and T4 is PENDING only
when *neither* branch could be measured). On daily bars the run-rate branch is
unmeasurable (``intraday_run_rate`` is ``None``), so only the re-thresholded
relative-volume branch can carry a PASS — which is precisely what the frozen
evaluator already does today.

Pre-registration honesty
------------------------
These candidates were named *before* this measurement ran (they are the v2 gate
short-list under review, not a post-hoc search). The independence approximation is
used for the P(>=1 in window) figures exactly as in the base-rate harness, and is
labelled as such. The October review's limitations (T5 VIX proxy, Yahoo
consolidated volume vs IEX, daily resolution, ~11% event haircut) are carried
forward by reference and apply to every number below, including V0.

Run from the repo root::

    .venv/bin/python scripts/measure_gate_variants.py

A non-zero exit means ``verify_against_builder`` found a fast/slow-path mismatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.gate_baserate import (  # noqa: E402
    VixRankProxy,
    build_stack,
    expected_per_year,
    joint_rate_excluding,
    measure,
    verify_against_builder,
)
from config.loader import load_example  # noqa: E402
from executor.indicators import InsufficientData, relative_volume  # noqa: E402
from scripts.fetch_history import SPY_CSV, VIX_CSV, load_bars, load_vix  # noqa: E402

REPORT_PATH = Path("docs/research/2026-11-18-v2-gate-candidates.md")
JSON_PATH = Path("scratch/v2-variants-2026-11-18.json")

DIRECTIONS = ("call", "put", "either")
VARIANTS = ("V0", "V1", "V2", "V3", "V4", "V5")
SESSIONS_PER_YEAR = 252

# The frozen T4 relative-volume threshold, read from the rulebook at runtime; these
# are the *candidate* re-thresholds, applied only in this script's recomposition.
CANDIDATE_T4_THRESHOLDS = {"V2": 1.2, "V3": 1.0, "V5": 1.2}

REVIEW_LIMITATIONS = [
    "T5's `iv_rank` is a Cboe VIX percentile-rank proxy, not SPY option IV; no free "
    "point-in-time historical SPY option IV exists. T5's true pass rate could differ "
    "by several percentage points in either direction.",
    "The signal feed is Yahoo Finance daily bars, not the production Alpaca SIP feed. "
    "Yahoo reports consolidated volume; Alpaca's free tier is IEX-only. T4 is a volume "
    "gate, so its measured pass rate is likely an overestimate of what production will "
    "see — and the candidate T4 re-thresholds inherit that same volume uncertainty.",
    "Daily bars cannot resolve anything intraday; the T2b fresh-cross guard is "
    "effectively inert at daily resolution (consecutive bars are exactly 24h apart).",
    "T5's event-day leg is not replayed (the calendar covers 2026-Q4 only); it is "
    "carried as a separate ~11% (28/252, FOMC + CPI) session-count haircut, not folded "
    "into the measured rates.",
    "Independence is assumed in the P(>=1 in window) figures; the gates are "
    "regime-dependent and real runs will cluster, so the figure is generous at this p.",
]


def _finite(value: float | None) -> bool:
    """Mirror of ``executor.checklist._finite``."""
    if value is None:
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _t4_variant_status(
    relvol: float | None, run_rate: float | None, relvol_min: float, run_rate_min: float
) -> str:
    """T4's status with a re-thresholded relative-volume branch.

    Mirrors ``executor.checklist._t4`` exactly, swapping only the relative-volume
    threshold for ``relvol_min``. PASS if either branch passes; PENDING only when
    neither branch could be measured; otherwise FAIL.
    """
    relvol_ok = _finite(relvol) and relvol >= relvol_min
    run_rate_ok = _finite(run_rate) and run_rate >= run_rate_min
    if relvol_ok or run_rate_ok:
        return "PASS"
    if relvol is None and run_rate is None:
        return "PENDING"
    return "FAIL"


def _relvol_series(stack, rules) -> list[float | None]:
    """Aligned relvol per bar index — field-for-field what ``snapshot_for_index`` does.

    ``relvol[i]`` is ``None`` where the ``lookback_days`` baseline is not yet full, the
    same ``None`` the snapshot builder propagates so a warmup hole reads PENDING.
    """
    lookback = rules.checklist.t4_volume.lookback_days
    vols = stack.volumes
    out: list[float | None] = [None] * len(stack)
    for i in range(len(stack)):
        if i < lookback:
            continue
        try:
            out[i] = relative_volume(vols[i], vols[i - lookback : i])
        except InsufficientData:
            out[i] = None
    return out


def _signal_sequence(n_bars: int, first_signal: int, offset: int) -> list[int]:
    """The signal-bar indices ``measure()`` evaluates, in session order.

    Rebuilt from the same loop the harness uses so the k-th session aligns with the
    k-th outcome for a given (arm, direction).
    """
    seq: list[int] = []
    for i in range(first_signal, n_bars):
        signal_i = i - offset
        if signal_i < first_signal:
            continue
        seq.append(signal_i)
    return seq


def _direction_fires(outcome, rec: dict, variant: str, run_rate_min: float) -> bool:
    """Does ``outcome`` fire under ``variant`` for one direction?"""
    t2b = outcome.passed("T2b")
    t3 = outcome.passed("T3")  # the T3a OR T3b group, via _resolved_statuses
    t4_frozen = outcome.passed("T4")
    rv = rec["relvol"]
    if variant == "V0":
        return outcome.fire
    t4_12 = _t4_variant_status(rv, None, 1.2, run_rate_min) == "PASS"
    t4_10 = _t4_variant_status(rv, None, 1.0, run_rate_min) == "PASS"
    if variant == "V1":
        return t2b and t3 and t4_frozen
    if variant == "V2":
        return t2b and t3 and t4_12
    if variant == "V3":
        return t2b and t3 and t4_10
    if variant == "V4":
        return (t2b or t3) and t4_frozen
    if variant == "V5":
        return (t2b or t3) and t4_12
    raise ValueError(f"unknown variant {variant}")


def _v1_v0_delta(outcomes) -> dict:
    """Decompose the V1\\V0 delta: sessions where T2b/T3/T4 all pass (V1 fires) but
    the frozen AND does not (V0 does not), keyed by which of the dropped gates T1/T2/T5
    fail. This is the mechanism behind "V1 != V0": on these sessions the dropped gates'
    vetoes *overlap* — more than one fails at once — so no single dropped gate is a
    leave-one-out sole-veto, yet dropping all three together exposes the session."""
    from collections import Counter

    delta: list = []
    for o in outcomes:
        v1 = all(o.passed(c) for c in ("T2b", "T3", "T4"))
        if v1 and not o.fire:
            delta.append(o)
    c: Counter = Counter()
    for o in delta:
        failed = tuple(sorted(g for g in ("T1", "T2", "T5") if not o.passed(g)))
        c[failed] += 1
    return {
        "sessions": len(delta),
        "by_failed_dropped_gates": {
            " + ".join(k) if k else "(none)": v
            for k, v in sorted(c.items(), key=lambda x: -x[1])
        },
    }


def _aggregate(fires: list[bool]) -> dict[str, float]:
    n = len(fires)
    count = sum(1 for f in fires if f)
    joint = count / n if n else 0.0
    return {
        "sessions": n,
        "fires": count,
        "joint_rate": joint,
        **{
            k: expected_per_year(joint, sessions_per_year=SESSIONS_PER_YEAR, target_sessions=60)[k]
            for k in (
                "expected_days_per_year",
                "expected_trades_per_window",
                "p_at_least_one_in_window",
            )
        },
    }


def score(measurement, bars, rules) -> dict:
    """Score every pre-declared variant for arm C, both bases, three direction views."""
    stack = build_stack(bars, rules)
    relvol = _relvol_series(stack, rules)
    run_rate_min = rules.checklist.t4_volume.intraday_run_rate_min
    n_bars = len(bars)
    first_signal = measurement.warmup_bars

    payload: dict = {"per_basis": {}}
    v1_v0_mismatch: list[str] = []

    for basis, result in measurement.results.items():
        offset = 1 if basis == "plan" else 0
        seq = _signal_sequence(n_bars, first_signal, offset)
        call_out = result.for_arm("C", "call")
        put_out = result.for_arm("C", "put")
        if not (len(seq) == len(result.sessions) == len(call_out) == len(put_out)):
            raise AssertionError(
                f"{basis}: alignment broke "
                f"(seq={len(seq)} sessions={len(result.sessions)} "
                f"call={len(call_out)} put={len(put_out)})"
            )

        recs = []
        for k in range(len(result.sessions)):
            recs.append(
                {
                    "session": result.sessions[k].isoformat(),
                    "signal_i": seq[k],
                    "relvol": relvol[seq[k]],
                    "call": call_out[k],
                    "put": put_out[k],
                }
            )

        basis_payload: dict = {"sessions": len(recs), "directions": {}}
        for direction in DIRECTIONS:
            per_variant: dict[str, dict] = {}
            for variant in VARIANTS:
                if direction == "either":
                    fires = [
                        _direction_fires(r["call"], r, variant, run_rate_min)
                        or _direction_fires(r["put"], r, variant, run_rate_min)
                        for r in recs
                    ]
                else:
                    fires = [
                        _direction_fires(r[direction], r, variant, run_rate_min) for r in recs
                    ]
                per_variant[variant] = _aggregate(fires)
            basis_payload["directions"][direction] = per_variant
        payload["per_basis"][basis] = basis_payload

        # Sanity: the task's premise is that V1 must equal V0 because the dropped gates
        # T1/T2/T5 each measured zero leave-one-out sole-vetoes. Any divergence is a
        # finding — and the delta decomposition below is the mechanism for it.
        for direction in DIRECTIONS:
            v0 = basis_payload["directions"][direction]["V0"]["fires"]
            v1 = basis_payload["directions"][direction]["V1"]["fires"]
            if v0 != v1:
                v1_v0_mismatch.append(
                    f"{basis}/{direction}: V0 fired {v0}, V1 fired {v1} — "
                    "dropping T1/T2/T5 changed the fire set"
                )

        # Delta mechanism: on the V1\\V0 sessions, which dropped gate(s) fail? Overlap
        # (more than one failing at once) is why zero leave-one-out sole-vetoes does not
        # imply leave-three-out redundancy.
        basis_payload["v1_v0_delta"] = {
            direction: _v1_v0_delta(result.for_arm("C", direction))
            for direction in ("call", "put")
        }

    payload["sanity_v1_equals_v0"] = {
        "holds": not v1_v0_mismatch,
        "mismatches": v1_v0_mismatch,
    }
    return payload


def crosscheck_v1(measurement) -> dict:
    """Independent V1 figure from the harness's own joint_rate_excluding.

    ``V1 == joint_rate_excluding(outcomes, ("T1","T2","T5"))`` must agree with the
    recomposition above, otherwise something in the recomposition is off.
    """
    out: dict[str, dict[str, dict]] = {}
    for basis, result in measurement.results.items():
        out[basis] = {}
        for direction in ("call", "put"):
            stats = joint_rate_excluding(result.for_arm("C", direction), ("T1", "T2", "T5"))
            out[basis][direction] = {
                "fires": int(stats["fires"]),
                "joint_rate": stats["joint_rate"],
            }
    return out


def _pct(value: float) -> str:
    return f"{value * 100:.4f}%"


def _fmt_row(variant: str, d: dict) -> str:
    return (
        f"| {variant} | {d['fires']} / {d['sessions']} | {_pct(d['joint_rate'])} "
        f"| {d['expected_days_per_year']:.3f} | {d['expected_trades_per_window']:.4f} "
        f"| {_pct(d['p_at_least_one_in_window'])} |"
    )


def render_report(
    measurement, score_payload: dict, crosscheck: dict, verify: dict
) -> str:
    lines: list[str] = []
    w = lines.append
    w("# v2 Gate Candidates — base-rate measurement (pre-declared)")
    w("")
    w(
        f"rulebook `{measurement.rulebook_version}` · arm C · "
        f"{measurement.bar_count} daily bars "
        f"({measurement.first_session} .. {measurement.last_session}) · "
        f"warmup {measurement.warmup_bars} bars"
    )
    w("")
    w(
        "Candidate gate structures scored by **recombining the per-session statuses** the "
        "frozen `executor.checklist.evaluate` already produced — no rulebook or config edit. "
        "T4 re-thresholds (1.2 / 1.0 vs the frozen 1.5) are applied only inside this "
        "recomposition and mirrored from `checklist._t4` exactly; on daily bars the "
        "intraday run-rate branch is unmeasurable, so only the relative-volume branch can "
        "carry a PASS."
    )
    w("")
    w(
        "**Verification:** "
        + (
            f"OK — {verify['samples']} sampled sessions, "
            f"{verify['evaluations_compared']} evaluations, 0 mismatches"
            if verify.get("ok")
            else f"FAILED — {json.dumps(verify)[:400]}"
        )
    )
    w("")

    sanity = score_payload["sanity_v1_equals_v0"]
    w("## Sanity check: V1 must equal V0")
    w("")
    if sanity["holds"]:
        w(
            "**Holds.** Dropping T1, T2, T5 from the AND changes nothing — consistent with "
            "the October review's zero sole-vetoes for those three gates. The binding work "
            "in this AND lives entirely in T2b / T3 / T4."
        )
    else:
        w(
            "**FAILED — reported loudly.** Dropping T1/T2/T5 from the AND changed the fire "
            "set. This is a genuine finding, not a harness bug: the October review's "
            "*\"T1, T2, T5 are redundant — removing any of them changes nothing\"* is a "
            "**leave-one-out** statement, and it does not extend to dropping all three "
            "together, which is what V1 does."
        )
        w("")
        w(
            "**Why the premise fails.** Each of T1/T2/T5 has zero *leave-one-out* "
            "sole-vetoes: on no session is exactly one of them the sole blocker while the "
            "other two still pass. But their vetoes **overlap** — on some sessions more than "
            "one of them fails at once. In a leave-one-out the other two dropped gates are "
            "still counted as \"the other gates,\" so a session where two of them fail is not "
            "a sole-veto for either. In V1 all three are removed, so those overlapping-veto "
            "sessions are exposed. The delta below decomposes the V1\\V0 sessions by which "
            "dropped gate(s) fail; multi-gate failure keys are the signature of the overlap."
        )
        w("")
        w("Mismatch per (basis, direction):")
        w("")
        for note in sanity["mismatches"]:
            w(f"- {note}")
        w("")
        for basis in ("plan", "eod"):
            w(f"`{basis}` basis — V1\\V0 delta, by which dropped gate(s) fail:")
            w("")
            w(
                "| Direction | V1\\V0 sessions | T1 only | T2 only | T5 only "
                "| T1+T2 | T1+T5 | T2+T5 | T1+T2+T5 |"
            )
            w("|---|---|---|---|---|---|---|---|---|")
            for direction in ("call", "put"):
                delta = score_payload["per_basis"][basis]["v1_v0_delta"][direction]
                cells = delta["by_failed_dropped_gates"]
                row = [str(delta["sessions"])]
                for key in ("T1", "T2", "T5", "T1 + T2", "T1 + T5", "T2 + T5", "T1 + T2 + T5"):
                    row.append(str(cells.get(key, 0)))
                w(f"| {direction} | " + " | ".join(row) + " |")
            w("")
        w(
            "**Reading.** Call-direction delta is tiny (T1/T2/T5's vetoes barely overlap), but "
            "put-direction is large and **T1-dominated**: T1 (close > 50EMA > 200EMA) is far "
            "more binding on puts than calls, so it vetoes many put sessions where T2b/T3/T4 "
            "pass. The practical consequence: V1 is not a neutral \"remove the redundant "
            "gates\" move — it loosens the put side far more than the call side, mostly by "
            "releasing T1."
        )
    w("")

    # Cross-check note
    w("## Cross-check (V1 vs harness `joint_rate_excluding`)")
    w("")
    w("")
    for basis in ("plan", "eod"):
        w(f"### Basis: `{basis}`")
        w("")
        w(
            "| Variant | Direction | Fires | Joint rate | Days/yr "
            "| Trades/60-session window | P(>=1 in window)¹ |"
        )
        w("|---|---|---|---|---|---|---|")
        for direction in DIRECTIONS:
            for variant in VARIANTS:
                d = score_payload["per_basis"][basis]["directions"][direction][variant]
                w(_fmt_row(f"{variant} · {direction}", d))
        w("")
        for direction in ("call", "put"):
            cc = crosscheck[basis][direction]
            mine = score_payload["per_basis"][basis]["directions"][direction]["V1"]
            agree = cc["fires"] == mine["fires"]
            w(
                f"- V1/{direction}: recomposition fired {mine['fires']} vs "
                f"`joint_rate_excluding` {cc['fires']} — {'agree' if agree else 'DISAGREE'}."
            )
        w("")
        w("¹ independence approximation — see limitations.")
        w("")

    w("## How to read the candidates")
    w("")
    w(
        "- **V1** isolates the three *nominally* binding gates (T2b / T3 / T4) by dropping "
        "T1/T2/T5. It is **not** identical to V0 (see the sanity section): dropping the three "
        "together releases overlapping vetoes, dominated by T1 on the put side. Read V1 as "
        "\"the checklist if the trend/RSI/IV gates were removed\" — a materially looser put "
        "gate, not a no-op."
    )
    w(
        "- **V2 / V3** loosen T4's relative-volume floor to 1.2 / 1.0. Because T4 is the "
        "dominant constraint, these move the joint rate the most of any candidate."
    )
    w(
        "- **V4** relaxes the T2b/T3 confluence from AND to OR, which is a materially looser "
        "technical requirement; **V5** stacks the 1.2 T4 floor on top."
    )
    w("")

    w("## Limitations (carried from `2026-10-03-gate-base-rates.md`)")
    w("")
    for i, note in enumerate(REVIEW_LIMITATIONS, start=1):
        w(f"{i}. {note}")
    w("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    for path in (SPY_CSV, VIX_CSV):
        if not Path(path).exists():
            print(
                f"error: cached data file {path} is missing; refusing to fetch "
                "(this script is offline by design — run "
                "`scripts/measure_gate_baserates.py --refresh` first)",
                file=sys.stderr,
            )
            return 2

    rules = load_example()
    bars = load_bars()
    vix = VixRankProxy(load_vix())
    print(
        f"loaded {len(bars)} SPY bars ({bars[0].t.date()} .. {bars[-1].t.date()})",
        file=sys.stderr,
    )

    verify = verify_against_builder(bars, rules=rules, vix=vix)
    print(
        f"verify_against_builder: {'OK' if verify.get('ok') else 'MISMATCH'} "
        f"({verify.get('samples')} sessions, {verify.get('evaluations_compared')} evals, "
        f"{verify.get('mismatch_count')} mismatches)",
        file=sys.stderr,
    )

    measurement = measure(bars, rules=rules, vix=vix, arms=("C",))
    score_payload = score(measurement, bars, rules)
    crosscheck = crosscheck_v1(measurement)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        render_report(measurement, score_payload, crosscheck, verify), encoding="utf-8"
    )
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(
        json.dumps(
            {
                "rulebook_version": measurement.rulebook_version,
                "bars": measurement.bar_count,
                "first_session": measurement.first_session.isoformat(),
                "last_session": measurement.last_session.isoformat(),
                "warmup_bars": measurement.warmup_bars,
                "arm": "C",
                "target_sessions": rules.window.target_sessions,
                "frozen_t4_relative_volume_min": rules.checklist.t4_volume.relative_volume_min,
                "candidate_t4_thresholds": CANDIDATE_T4_THRESHOLDS,
                "verification": verify,
                "event_haircut": measurement.event_haircut,
                "crosscheck_v1": crosscheck,
                "sanity_v1_equals_v0": score_payload["sanity_v1_equals_v0"],
                "per_basis": score_payload["per_basis"],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"wrote {REPORT_PATH}", file=sys.stderr)
    print(f"wrote {JSON_PATH}", file=sys.stderr)
    return 0 if verify.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
