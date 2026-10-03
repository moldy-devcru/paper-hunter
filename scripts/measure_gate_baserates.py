"""Measure the frozen checklist's base rates over real SPY history.

Run from the repo root::

    .venv/bin/python scripts/measure_gate_baserates.py
    .venv/bin/python scripts/measure_gate_baserates.py --refresh   # re-fetch
    .venv/bin/python scripts/measure_gate_baserates.py --json out.json

What it prints
--------------
For each basis (``plan`` = the prior close, which is what the pre-market hunt plan
actually evaluates; ``eod`` = the session's own close, the usual backtest convention
and the closer match to the outsider review's arithmetic):

* per-condition pass / veto / pending rates, per arm and direction, from a replay of the
  real ``executor.checklist.evaluate``;
* the joint ANDed pass rate, and what it projects to as qualifying days per year and per
  planned window;
* which gates are binding, via sole-veto counts and a leave-one-out lift;
* the arm-level answer to the question the window actually turns on — how many sessions
  produce a green cell for each arm.

It also runs ``verify_against_builder`` on every execution, which re-derives a sample of
sessions through the production ``snapshot_builder.build_snapshot`` and asserts the fast
path agrees. A mismatch is printed loudly and sets a non-zero exit code, because a
base-rate table that quietly diverged from production is worse than no table.

Nothing here writes to the journal, the rulebook, or any threshold. It reads the frozen
rulebook and prints numbers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.gate_baserate import (  # noqa: E402
    CONDITIONS,
    RULEBOOK_CONDITIONS,
    BarRow,
    VixRankProxy,
    condition_rates,
    expected_per_year,
    joint_rate,
    joint_rate_excluding,
    marginal_contribution,
    measure,
    verify_against_builder,
)
from config.loader import load_example  # noqa: E402
from scripts.fetch_history import (  # noqa: E402
    SPY_CSV,
    VIX_CSV,
    fetch_spy_csv,
    fetch_vix_csv,
    load_bars,
    load_vix,
)


def _pct(value: float) -> str:
    return f"{value * 100:6.2f}%"


def _direction_header(direction: str) -> str:
    return f"{direction}s"


def report(measurement, rules, *, verify: dict) -> str:
    lines: list[str] = []
    w = lines.append
    w("SPY frozen-checklist gate base rates")
    w("=" * 78)
    w(f"rulebook            : {measurement.rulebook_version}")
    w(
        f"signal bars         : {measurement.bar_count} daily bars "
        f"({measurement.first_session} .. {measurement.last_session})"
    )
    w(f"warmup skipped      : first {measurement.warmup_bars} bars (indicator warmup only)")
    vix_first, vix_last = measurement.vix_coverage
    w(f"VIX proxy coverage  : {vix_first} .. {vix_last}")
    w(
        f"planned window      : {rules.window.target_sessions} sessions / "
        f"{rules.window.months} months, arms {rules.window.arms}"
    )
    w("")
    w("Verification against executor.snapshot_builder.build_snapshot:")
    if verify.get("ok"):
        w(
            f"  OK — {verify['samples']} sampled sessions, "
            f"{verify['evaluations_compared']} evaluations compared, 0 mismatches"
        )
    else:
        w(f"  FAILED — {json.dumps(verify)[:400]}")
    w("")

    summary: dict[str, dict[str, dict]] = {}
    for basis, result in measurement.results.items():
        w("=" * 78)
        basis_note = (
            "signal bar = PRIOR close (what hunt_plan evaluates pre-market)"
            if basis == "plan"
            else "signal bar = the session's OWN completed close (backtest convention)"
        )
        w(f"BASIS: {basis} — {basis_note}")
        w(f"sessions measured: {result.n_sessions}")
        w("")

        for arm in ("B", "C"):
            w("-" * 78)
            w(f"ARM {arm}")
            header = (
                f"{'cond':<5} {'pass%':>8} {'veto%':>8} {'pending':>6} "
                f"{'passes':>8} {'vetoes':>8}   (of evaluated sessions, per direction)"
            )
            w(header)
            rates_by_cond: dict[str, dict] = {}
            for condition in CONDITIONS:
                passes = vetoes = evaluated = 0
                per_direction = {}
                for direction in ("call", "put"):
                    rates = condition_rates(result.for_arm(arm, direction), condition)
                    per_direction[direction] = rates
                    passes += rates["pass"]
                    vetoes += rates["fail"]
                    evaluated += rates["evaluated"]
                veto_rate = vetoes / evaluated if evaluated else 0.0
                pending = sum(per_direction[d]["pending"] for d in ("call", "put"))
                rates_by_cond[condition] = {
                    "pass_rate": passes / evaluated if evaluated else 0.0,
                    "veto_rate": veto_rate,
                    "passes": passes,
                    "vetoes": vetoes,
                    "evaluated": evaluated,
                    "pending": pending,
                    "per_direction": per_direction,
                }
                call_rate = per_direction["call"]["pass_rate"]
                if call_rate == per_direction["put"]["pass_rate"]:
                    split = f"{_pct(call_rate).strip()} (both directions)"
                else:
                    split = (
                        f"call {_pct(call_rate).strip()}  put "
                        f"{_pct(per_direction['put']['pass_rate']).strip()}"
                    )
                w(
                    f"{condition:<5} {_pct(rates_by_cond[condition]['pass_rate'])} "
                    f"{_pct(veto_rate)} {pending:6.0f} {passes:8.0f} "
                    f"{vetoes:8.0f}   {split}"
                )
            summary.setdefault(basis, {})[arm] = {"conditions": rates_by_cond}
            w("")

            # -- joint rate, per direction and either-direction ------------------
            w("  JOINT (every blocking condition PASS):")
            joint: dict[str, dict] = {}
            for direction in ("call", "put"):
                outcomes = result.for_arm(arm, direction)
                stats = joint_rate(outcomes)
                proj = expected_per_year(
                    stats["joint_rate"],
                    target_sessions=rules.window.target_sessions,
                    target_months=rules.window.months,
                )
                joint[direction] = {**stats, **proj}
                w(
                    f"    {direction:<5} fires {stats['fires']:>4.0f} / "
                    f"{stats['sessions']:.0f} sessions = {_pct(stats['joint_rate'])}"
                    f"   -> {proj['expected_days_per_year']:.2f} days/yr, "
                    f"{proj['expected_days_per_window']:.3f} per "
                    f"{rules.window.target_sessions}-session window"
                    f"   P(0 in window)={proj['p_zero_in_window'] * 100:.1f}%"
                )
            either = tuple(
                o for o in result.outcomes if o.arm == arm
            )
            per_session: dict[dt.date, bool] = {}
            for o in either:
                per_session[o.session] = per_session.get(o.session, False) or o.fire
            fired = sum(1 for v in per_session.values() if v)
            either_rate = fired / len(per_session) if per_session else 0.0
            proj_either = expected_per_year(
                either_rate,
                target_sessions=rules.window.target_sessions,
                target_months=rules.window.months,
            )
            joint["either_direction"] = {
                "sessions": float(len(per_session)),
                "fires": float(fired),
                "joint_rate": either_rate,
                **proj_either,
            }
            w(
                f"    either fires {fired:>4.0f} / {len(per_session)} sessions "
                f"= {_pct(either_rate)}"
                f"   -> {proj_either['expected_days_per_year']:.2f} days/yr, "
                f"{proj_either['expected_days_per_window']:.3f} per window"
                f"   P(0 in window)={proj_either['p_zero_in_window'] * 100:.1f}%"
            )
            w("")
            summary[basis][arm]["joint"] = joint

            # -- binding gates ---------------------------------------------------
            #
            # Arm B carries T6, which is PENDING on every session while its multiplier is
            # uncalibrated. Its true joint rate is therefore zero and a marginal analysis
            # over T1..T6 is degenerate. So the marginal table runs at rulebook
            # granularity (T3 collapsed to one entry) and, for arm B, with T6 dropped —
            # reported as a counterfactual for the technical gates, never as a prediction
            # of what arm B will do.
            counterfactual = arm == "B"
            excl = ("T6",) if counterfactual else ()
            if counterfactual:
                w("  [T6 dropped — counterfactual for the technical gates only; T6 is")
                w("   PENDING on every session, so arm B's real joint rate is 0.00%]")
            w("  BINDING (sole-veto = only blocker that day;")
            w("           fires-if-removed = sessions that would fire without this gate):")
            marg = marginal_contribution(
                result.for_arm(arm, "call"), RULEBOOK_CONDITIONS, exclude=excl
            )
            marg_put = marginal_contribution(
                result.for_arm(arm, "put"), RULEBOOK_CONDITIONS, exclude=excl
            )
            for condition in RULEBOOK_CONDITIONS:
                entry = marg[condition]
                if entry["not_applicable"]:
                    note = (
                        " [dropped from this counterfactual]"
                        if counterfactual and condition == "T6"
                        else " [not carried by this arm - excluded]"
                    )
                elif entry["always_pending"]:
                    note = " [always PENDING - blocks for lack of data, not a constraint]"
                else:
                    note = ""
                w(
                    f"    {condition:<5} sole-veto {entry['sole_veto_sessions']:>4.0f} "
                    f"({_pct(entry['sole_veto_share_of_sessions']).strip()} of sessions)"
                    f"  pass|others-pass {_pct(entry['pass_given_others_pass']).strip()}"
                    f"  fires-if-removed {entry['fires_if_removed']:>3.0f}{note}"
                )
            summary[basis][arm]["binding"] = {"call": marg, "put": marg_put,
                                              "excluded": ",".join(excl)}
            w("")
    measurement_summary = summary
    lines.append("=" * 78)
    w("NOTES / LIMITATIONS")
    for note in measurement.notes:
        w(f"  - {note}")
    haircut = measurement.event_haircut
    w(
        f"  - event-leg haircut if T5's calendar veto were applied across history: "
        f"{haircut['estimated_veto_sessions_per_year']:.0f} of "
        f"{haircut['sessions_per_year']:.0f} sessions/yr "
        f"({_pct(haircut['haircut_fraction']).strip()}, kinds={haircut['veto_kinds']}) "
        f"— NOT included in the T5 rates above"
    )
    w("")
    w("JSON payload written alongside this report by --json; summary keys:")
    w("  results[basis][arm].conditions[T*].pass_rate / .passes / .vetoes")
    w("  results[basis][arm].joint[direction|either_direction].joint_rate")
    w("  results[basis][arm].binding[call|put][T*].sole_veto_sessions / .fires_if_removed")
    del measurement_summary
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--refresh", action="store_true", help="re-download the SPY and VIX CSVs"
    )
    parser.add_argument("--json", type=Path, help="also write the numbers as JSON here")
    parser.add_argument(
        "--verify-samples", type=int, default=12, help="sessions to re-derive the slow way"
    )
    args = parser.parse_args(argv)

    rules = load_example()

    if args.refresh or not SPY_CSV.exists():
        print("fetching SPY daily history ...", file=sys.stderr)
        fetch_spy_csv(refresh=args.refresh)
    if args.refresh or not VIX_CSV.exists():
        print("fetching Cboe VIX history ...", file=sys.stderr)
        fetch_vix_csv(refresh=args.refresh)

    bars: list[BarRow] = load_bars()
    vix = VixRankProxy(load_vix())
    print(
        f"loaded {len(bars)} SPY bars ({bars[0].t.date()} .. {bars[-1].t.date()})",
        file=sys.stderr,
    )

    verify = verify_against_builder(bars, rules=rules, vix=vix, samples=args.verify_samples)

    measurement = measure(bars, rules=rules, vix=vix)

    print(report(measurement, rules, verify=verify))

    if args.json:
        payload = {
            "rulebook_version": measurement.rulebook_version,
            "bars": measurement.bar_count,
            "first_session": measurement.first_session.isoformat(),
            "last_session": measurement.last_session.isoformat(),
            "warmup_bars": measurement.warmup_bars,
            "vix_coverage": [d.isoformat() if d else None for d in measurement.vix_coverage],
            "verification": verify,
            "notes": list(measurement.notes),
            "event_haircut": measurement.event_haircut,
            "expected_per_year": {},
        }
        for basis, result in measurement.results.items():
            payload.setdefault("results", {}).setdefault(basis, {})
            for arm in ("B", "C"):
                per_session: dict[str, bool] = {}
                for o in result.outcomes:
                    if o.arm != arm:
                        continue
                    key = o.session.isoformat()
                    per_session[key] = per_session.get(key, False) or o.fire
                arm_payload: dict = {"session_count": len(per_session), "conditions": {}}
                for condition in CONDITIONS:
                    per_dir = {}
                    for direction in ("call", "put"):
                        stats = condition_rates(result.for_arm(arm, direction), condition)
                        per_dir[direction] = stats
                    arm_payload["conditions"][condition] = per_dir
                arm_payload["joint"] = {
                    d: joint_rate(result.for_arm(arm, d)) for d in ("call", "put")
                }
                arm_payload["joint_excluding_t6"] = {
                    d: joint_rate_excluding(result.for_arm(arm, d), ("T6",))
                    for d in ("call", "put")
                }
                arm_payload["joint"]["either_direction"] = {
                    "sessions": float(len(per_session)),
                    "fires": float(sum(1 for v in per_session.values() if v)),
                    "joint_rate": (
                        sum(1 for v in per_session.values() if v) / len(per_session)
                        if per_session
                        else 0.0
                    ),
                }
                arm_payload["binding"] = {
                    d: marginal_contribution(
                        result.for_arm(arm, d),
                        RULEBOOK_CONDITIONS,
                        exclude=("T6",) if arm == "B" else (),
                    )
                    for d in ("call", "put")
                }
                payload["results"][basis][arm] = arm_payload
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)

    return 0 if verify.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())