"""CLI — ``hunt-plan`` / ``watch`` / ``eod``. Dry-run is the default, always.

Three entrypoints mirroring the brief's "Signal cadence":

* ``hunt-plan`` (pre-market, 08:30–09:15 ET) — recompute the EOD indicator stack, run
  the event-calendar veto check, build today's watchlist, journal one decision row per
  (arm, direction) cell, write the plan JSON the intraday loop will read.
* ``watch`` (intraday, continuous) — the trigger-price loop from
  :mod:`executor.watch_loop`, reading the plan ``hunt-plan`` wrote.
* ``eod`` (16:15 ET) — mark positions, enforce anything still open, journal the day's
  close, write the NO-SHOT entries.

Dry-run by construction
-----------------------
``--dry-run`` is ON by default and ``--live`` is required to turn it off, and the two
are mutually exclusive so ``--live --dry-run`` is an argparse error rather than a
last-flag-wins surprise. In dry run the order router is :class:`DryRunRouter`: no
credential is read, no request is made, nothing is placed. ``--live`` additionally
requires the paper credentials AND a live ``/v2/account`` read that *proves* a paper
account; the account id is printed in a confirmation banner before the first order,
and an account that reports live (or unproven) is refused outright.

This is the third layer of the kill switch described in
:mod:`executor.alpaca_trading` — URL allowlist, no live env var, account proof — and
the only one a human actually touches, so it is also the one that prints its work.

# RULED 2026-10-02 (operator): **autonomous execution within the frozen rules.** "That
is the whole point" — the rulebook is the pre-registration, and a per-trade approval
step would reintroduce exactly the discretion the experiment is built to exclude. So
there is deliberately no approval flag anywhere in this CLI: once ``--live`` is passed
(after the paper-account proof), ``watch`` places entry orders unattended and ``eod``
closes positions unattended, governed only by the rulebook plus the kill switch. The
oversight that remains is the kill switch, the journal, and the monthly review — not a
human in the loop per trade.

# INTERPRETATION: no command writes to the journal unless ``--db`` is passed. A
pre-market plan that journals rows into a database nobody named is a surprise, and the
ledger is the experiment's primary artifact.

# INTERPRETATION: ``watch`` reads the plan from ``--plan`` rather than rebuilding it.
The plan is an artifact of the pre-market pass; if the intraday loop rebuilt its own,
the two passes could disagree about the day's levels and neither would be wrong, which
is exactly the kind of invisible drift a pre-registered experiment cannot have. The
plan file's sha256 is recorded in the run banner so a reader can tie the session to
the exact file it ran on.

Python 3.12+, stdlib + the rest of the repo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from config.loader import DEFAULT_RULES_PATH, Rulebook, load_rules
from data.event_calendar import EventCalendar
from executor.alpaca_client import BarSeries
from executor.bar_windows import bar_window
from executor.flow_gate import POLICIES as FLOW_GATE_POLICIES  # noqa: F401
from executor.hunt_plan import HuntPlan, build_hunt_plan, summarise, write_hunt_plan
from executor.indicators import ema
from executor.noshot import build_noshots, write_noshots
from executor.position_manager import (
    ArmState,
    MemoryJournalSink,
    PositionManager,
    SqliteJournalSink,
    eastern,
    iso_ts,
    to_et,
)
from executor.watch_loop import (
    AlpacaWatchData,
    DryRunRouter,
    LiveOrderRouter,
    WatchState,
    run_loop,
)

PLAN_DIR = Path("data/plans")

#: Daily bars the EOD pass needs. 400 is comfortably more than the 50 the EMA needs and
#: more than a season of sessions, so the streak walk and any future indicator have room.
#: Named, not inlined, because it is half of the ``bar_window`` call below — a limit whose
#: window was never named is the exact bug class this pair exists to prevent.
EOD_BARS_LIMIT = 400

#: The EOD pass reads consolidated SIP daily bars (it judges closes, not prints).
EOD_BAR_FEED = "sip"

#: `FLOW_GATE_POLICIES` (imported above from `executor.flow_gate`) is the single source
#: of the accepted T6 policy names.
#:
#: FIX 2026-10-03 (Monday readiness): it used to be a hand-copied literal
#: ``("none", "last-confirmed")`` that had fallen out of date with the module it mirrors.
#: `executor/flow_gate.py` has accepted four names since R3 was implemented
#: (``none``/``carry_forward``/``next_day_only``, with ``last-confirmed`` as a deprecated
#: alias for ``carry_forward``) — and the rulebook's own frozen
#: ``checklist.t6_flow.intraday_policy`` is ``carry_forward``. So the CLI REJECTED the
#: ruled policy by name while its own help text advertised it, and any caller following
#: either the rulebook or `paper-hunter watch --help` got "invalid choice: carry_forward".
#: It surfaced only because the new `scripts/watch.service` passes the policy explicitly;
#: the duplicate had been invisible while both callers defaulted around it.
#:
#: One implementation, same as the T6 calibration reader/formula mismatch the rehearsal
#: fixed. Aliased rather than renamed so existing importers of `FLOW_GATE_POLICIES` keep
#: working, and imported from the module that owns the behaviour rather than re-derived.

#: The session open, ET. A decision journaled before it is the pre-market plan's; one
#: at or after it is intraday machinery (the watch loop's re-verifications, entries,
#: exits). Used to pick the plan row each counterfactual links to — see
#: :func:`_plan_decision_ids`.
PLAN_CUTOFF_ET = dt.time(9, 30)


class CliError(RuntimeError):
    """Anything that stops the run, with a message meant for a human."""


# ---------------------------------------------------------------------------
# plan (de)serialisation
# ---------------------------------------------------------------------------


def write_plan_file(plan: HuntPlan, path: Path) -> Path:
    """Write ``plan.to_dict()`` as pretty JSON with a provenance header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = plan.to_dict()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_plan_file(path: Path) -> HuntPlan:
    """Reconstruct a :class:`HuntPlan` from ``write_plan_file``'s JSON.

    Deliberately a real reconstruction rather than a duck-typed wrapper: the watch
    loop reads ``cell.checklist.conditions[...]`` and ``cell.watch.strike_projection``
    off these objects, so a partially-parsed plan would fail deep inside a tick instead
    of at load time.
    """
    from executor.checklist import ChecklistResult, ConditionResult

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cells = []
    for raw in payload.get("arms", []):
        checklist_raw = raw.get("checklist", {})
        conditions = {
            cid: ConditionResult(
                id=c["id"],
                status=c["status"],
                detail=c["detail"],
                blocking=bool(c.get("blocking", True)),
            )
            for cid, c in checklist_raw.get("conditions", {}).items()
        }
        checklist = ChecklistResult(
            arm=checklist_raw["arm"],
            direction=checklist_raw["direction"],
            conditions=conditions,
            veto_reasons=tuple(checklist_raw.get("veto_reasons", [])),
            indicators=checklist_raw.get("indicators", {}),
            fire=bool(checklist_raw.get("fire")),
        )
        watch_raw = raw.get("watch_levels", {})
        cells.append(
            _arm_plan_from_dict(raw, checklist, watch_raw)
        )
    return HuntPlan(
        day=dt.date.fromisoformat(payload["day"]),
        symbol=payload["symbol"],
        generated_ts=payload["generated_ts"],
        strategy_version=payload["strategy_version"],
        config_sha256=payload.get("config_sha256", ""),
        arms=tuple(cells),
        event_day_veto=bool(payload.get("event_day_veto")),
        event_reasons=tuple(payload.get("event_reasons", [])),
        notes=tuple(payload.get("notes", [])),
    )


def _arm_plan_from_dict(raw: dict[str, Any], checklist: Any, watch_raw: dict[str, Any]) -> Any:
    from executor.hunt_plan import ArmPlan, TriggerPrice, WatchLevels

    watch = WatchLevels(
        close=watch_raw.get("close", 0.0),
        ema_fast=watch_raw.get("ema_fast"),
        ema_slow=watch_raw.get("ema_slow"),
        bollinger_upper=watch_raw.get("bollinger_upper"),
        bollinger_middle=watch_raw.get("bollinger_middle"),
        bollinger_lower=watch_raw.get("bollinger_lower"),
        prior_high=watch_raw.get("prior_high"),
        prior_low=watch_raw.get("prior_low"),
        arm_criteria=watch_raw.get("arm_criteria", {}),
        arm_b_criteria=watch_raw.get("arm_b_criteria", {}),
        strike_projection=watch_raw.get("strike_projection"),
        notes=tuple(watch_raw.get("notes", [])),
    )
    triggers = tuple(
        TriggerPrice(
            condition=t.get("condition", ""),
            level=t.get("level"),
            sense=t.get("sense"),
            status=t.get("status", "unknown"),
            reason=t.get("reason", ""),
        )
        for t in raw.get("triggers", [])
    )
    return ArmPlan(
        arm=raw["arm"],
        direction=raw["direction"],
        checklist=checklist,
        triggers=triggers,
        watch=watch,
        decision_kind=raw.get("decision_kind", "NO_TRADE"),
        conviction=int(raw.get("conviction", 1)),
        reasoning=raw.get("reasoning", ""),
        snapshot_dict=raw.get("snapshot", {}),
    )


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-hunter",
        description="SPY paper-trading executor (paper-only by construction)",
    )
    parser.add_argument("--rules", default=str(DEFAULT_RULES_PATH), help="frozen rulebook path")
    parser.add_argument("--db", default=None, help="SQLite journal path; omit to not journal")
    parser.add_argument(
        "--now",
        default=None,
        help="override the clock (ISO-8601, ET-naive is refused). Testing/offline replay.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="plan and journal, place no orders (DEFAULT)",
    )
    mode.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        help=(
            "place PAPER orders. Requires ALPACA_PAPER_KEY/ALPACA_PAPER_SECRET and a "
            "/v2/account read that proves a paper account; refuses otherwise."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_cmd = sub.add_parser("hunt-plan", help="pre-market: build and journal today's plan")
    plan_cmd.add_argument("--date", default=None, help="session date (default: today ET)")
    plan_cmd.add_argument("--out", default=None, help="plan JSON path")
    plan_cmd.add_argument(
        "--open-arm-a",
        action="store_true",
        help="also place arm A's one buy-and-hold order (live mode only)",
    )
    plan_cmd.add_argument(
        "--spot", type=float, default=None, help="override the reference spot price"
    )
    plan_cmd.add_argument(
        "--ivrank",
        default=None,
        help=(
            "IV-rank store path, so T5 has a rank to read (default: the store's own "
            "path). FIX 2026-10-03: hunt-plan previously passed no store, so T5 was "
            "PENDING in every cell and every plan was NO_TRADE by construction."
        ),
    )
    plan_cmd.add_argument(
        "--flow-gate",
        choices=FLOW_GATE_POLICIES,
        default=None,
        help=(
            "T6 input policy. RULED 2026-10-03 (R3): defaults to the rulebook's "
            "checklist.t6_flow.intraday_policy, which is 'carry_forward' — the newest "
            "flow_baseline row dated before the session, carried forward however old, "
            "with its age recorded as sessions_stale. 'next_day_only' accepts only the "
            "immediately preceding session and leaves T6 PENDING on a gap. 'none' hands "
            "the plan no flow gate at all (the pre-2026-10-03 behaviour). 'last-"
            "confirmed' is a deprecated alias of 'carry_forward'."
        ),
    )

    watch_cmd = sub.add_parser("watch", help="intraday trigger loop")
    watch_cmd.add_argument(
        "--plan", default=None, help="plan JSON (default data/plans/<date>.json)"
    )
    watch_cmd.add_argument("--date", default=None)
    watch_cmd.add_argument("--poll-seconds", type=float, default=15.0)
    watch_cmd.add_argument("--stop-et", default="16:00")
    watch_cmd.add_argument("--ticks", type=int, default=None, help="stop after N ticks")
    watch_cmd.add_argument(
        "--ivrank",
        default=None,
        help="IV-rank store path (default: the store's own path)",
    )
    watch_cmd.add_argument(
        "--iv-rank",
        type=float,
        default=None,
        help=(
            "IV rank to re-verify T5 with. Omit to read the store for the call-side ATM "
            "tenor; the value is printed every session so the journal's T5 can be tied "
            "to it"
        ),
    )
    watch_cmd.add_argument(
        "--flow-gate",
        choices=FLOW_GATE_POLICIES,
        default="none",
        help="T6 input policy; see executor/flow_gate.py. Default 'none' = no gate.",
    )
    watch_cmd.add_argument(
        "--flow-side",
        choices=("call", "put"),
        default="call",
        help="which trade side the T6 gate is read for when --flow-gate is used",
    )

    eod_cmd = sub.add_parser("eod", help="end of day: mark, enforce, journal, NO-SHOTs")
    eod_cmd.add_argument("--plan", default=None)
    eod_cmd.add_argument("--date", default=None)
    eod_cmd.add_argument(
        "--offline",
        action="store_true",
        help=(
            "run the EOD pass against a fixture transport instead of the network: no "
            "credential, no request, same parsing and same failures. Reads the day's "
            "bars and the option chain from --offline-fixture"
        ),
    )
    eod_cmd.add_argument(
        "--offline-fixture",
        default=None,
        help=(
            "JSON file of {request path: payload} (or {\"routes\": {...}}), shaped like "
            "Alpaca's documented responses: /v2/stocks/SPY/bars as a flat 'bars' list "
            "and /v1beta1/options/snapshots/SPY as a symbol-keyed 'snapshots' object. "
            "Required by --offline; an unrouted request raises 404 like the real API"
        ),
    )
    return parser


def _parse_now(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.UTC)
    text = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise CliError("--now must carry a UTC offset (e.g. 2026-10-02T09:50:00-04:00)")
    return parsed


def _session_day(now: dt.datetime) -> dt.date:
    return to_et(now).date()


def _rules(args: argparse.Namespace) -> Rulebook:
    rules = load_rules(args.rules)
    pending = rules.pending_calibrations
    if pending and rules.strategy.status != "FROZEN":
        print(
            f"note: rulebook status is {rules.strategy.status} with "
            f"{len(pending)} calibration(s) pending: {'; '.join(pending)}",
            file=sys.stderr,
        )
    return rules


def _calendar(rules: Rulebook) -> EventCalendar | None:
    directory = Path(rules.checklist.t5_options_chain.event_calendar.calendar_dir)
    if not directory.exists():
        return None
    try:
        return EventCalendar.load_dir(directory)
    except Exception as exc:  # noqa: BLE001 - a broken calendar is reported, not fatal
        print(f"warning: event calendar unavailable ({exc}); T5 veto disabled", file=sys.stderr)
        return None


def _sink(conn: Any, rules: Rulebook) -> Any:
    """The journal sink for this run: SQLite when a ``--db`` was named, else in-memory."""
    if conn is None:
        return MemoryJournalSink()
    return SqliteJournalSink(conn, rules.strategy_version)


def _open_db(path: str | None) -> Any:
    if not path:
        return None
    from journal.store import init_db

    return init_db(path)


def _iv_store(path: str | None) -> Any:
    """Open the IV-rank store read-only, or return ``None`` if there is nothing to open.

    # FIX (2026-10-03, full-cycle rehearsal): ``hunt-plan`` and ``watch`` never opened
    # the store, so T5 evaluated PENDING on every cell ("iv_rank unavailable") and arm
    # B could not fire from the CLI under any market conditions. PENDING is honest but
    # it is also blocking, so an unwired input is a silent strategy halt.

    Read-only on purpose: a missing store returns ``None`` instead of creating an empty
    database, so a mistyped path cannot leave behind a file that looks like history.
    """
    from executor.iv_rank import DEFAULT_DB_PATH, IvRankStore

    target = Path(path) if path else Path(DEFAULT_DB_PATH)
    if not target.exists():
        return None
    import sqlite3

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return IvRankStore(conn)


def _flow_gate(args: argparse.Namespace, conn: Any, rules: Rulebook, day: dt.date) -> Any:
    """T6's gate for this run, or ``None`` under the ``none`` policy.

    RULED 2026-10-03 (R3): the default is the RULEBOOK's
    ``checklist.t6_flow.intraday_policy`` (``carry_forward``), not a constant in this
    file. The ruling is meant to live in the rulebook where the version bump that
    changes it is already required — duplicating the default here would give the
    operator two places to flip a policy and no way to tell which one the journal used.

    Prints the read (source session, ratio, status, staleness) because "which session
    did T6 read" is the first question anyone will ask of a journal row that hinges on
    this gate, and "how stale was it" is the second.
    """
    from executor.flow_gate import normalise_policy

    policy = normalise_policy(
        getattr(args, "flow_gate", None) or rules.checklist.t6_flow.intraday_policy
    )
    if policy == "none" or conn is None:
        return None
    from executor.flow_gate import flow_gate_from_journal

    t6 = rules.checklist.t6_flow
    multiplier = None if t6.multiplier.calibration_pending else float(t6.multiplier.value)
    read = flow_gate_from_journal(
        conn,
        as_of=day,
        side=getattr(args, "flow_side", "call"),
        multiplier=multiplier,
        lookback_days=t6.baseline_lookback_days,
        policy=policy,
    )
    print(read.summary_line())
    return read.gate


def _watch_iv_rank(
    args: argparse.Namespace, plan: HuntPlan, rules: Rulebook
) -> tuple[dict[str, float] | float | None, str]:
    """The IV rank(s) ``watch`` re-verifies T5 with, and where each came from.

    RULED 2026-10-03 (operator, R4): T5's rank is per tenor, and the tenor differs per
    cell — arm B's 0DTE call and arm C's 150-DTE call are different series whose
    warm-up states differ. This used to collapse them to ONE number (the newest ranked
    observation in the store, i.e. an arbitrary tenor) and forward it to every cell, so
    a rank that was warm for one contract was asserted for all of them.

    Precedence, per cell:
      1. ``--iv-rank`` — the operator states it, for every cell, unchanged.
      2. the rank the pre-market plan recorded for THAT cell, with its tenor key.
      3. the newest tenor in the store whose rank is DEFINED. Falling back to "newest
         overall" is what made the old value arbitrary, so a cell with no plan-time read
         is left PENDING instead.

    Returns a mapping keyed by ``ArmPlan.key`` (``"B/call"``), a bare float when
    ``--iv-rank`` pinned one, or ``None`` when nothing resolved.

    # FIX 2026-10-03 (live seam sweep): step 3 read ``o.rank`` off each object from
    # ``store.observations()``. That accessor returns :class:`~executor.iv_rank.IvObservation`
    # — a stored *reading* — which has no ``rank`` attribute at all; ``rank`` lives on
    # :class:`~executor.iv_rank.IvRankResult`, the thing ``store.iv_rank()`` RETURNS.
    # So the branch raised ``AttributeError: 'IvObservation' object has no attribute
    # 'rank'`` on every store that had rows — i.e. on exactly the stores worth having,
    # and never on the empty ones the tests were written against. It only showed up when
    # the store finally held real backfilled rows.
    #
    # The rank is now computed the way the rest of the repo computes it: newest stored IV
    # for a tenor, scored through ``store.iv_rank`` (the same call
    # ``scripts/verify_t5_warm.py`` measures with). An observation is a reading; a rank is
    # a reading scored against a window, and only the store can do the scoring.
    """
    if getattr(args, "iv_rank", None) is not None:
        return float(args.iv_rank), "--iv-rank (operator supplied)"
    store = _iv_store(getattr(args, "ivrank", None))
    observations: list[Any] = []
    if store is not None:
        try:
            observations = list(store.observations(rules.strategy.symbol))
        except Exception as exc:  # noqa: BLE001 - a broken store must not stop the session
            return None, f"IV store unreadable ({exc})"
    resolved: dict[str, float] = {}
    notes: list[str] = []
    for cell in plan.arms:
        value = cell.snapshot_dict.get("iv_rank")
        if isinstance(value, (int, float)):
            resolved[cell.key] = float(value)
            key = cell.snapshot_dict.get("iv_rank_tenor_key")
            notes.append(f"{cell.key} from plan ({key or 'tenor not recorded'})")
    if not resolved:
        if store is None:
            return None, "no IV store"
        if not observations:
            return None, "IV store has no observations"
        best: tuple[str, str, float] | None = None
        newest: dict[str, Any] = {}
        for obs in observations:
            prior = newest.get(obs.tenor_key)
            if prior is None or obs.as_of > prior.as_of:
                newest[obs.tenor_key] = obs
        for key, obs in newest.items():
            try:
                result = store.iv_rank(
                    obs.iv, rules.strategy.symbol, key, as_of=obs.as_of
                )
            except Exception:  # noqa: BLE001 - one unreadable tenor must not stop the rest
                continue
            if result.rank is None:
                continue
            candidate = (obs.as_of, key, float(result.rank))
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is None:
            return None, (
                f"IV store has {len(observations)} observation(s) across "
                f"{len(newest)} tenor(s) but no tenor has reached MIN_OBSERVATIONS — "
                f"T5 cannot be evaluated until the history warms up"
            )
        # No plan-time read for any cell, so there is no tenor to be faithful to.
        # Say that rather than inventing one: under R4 the newest tenor is as likely
        # to be the wrong one as the right one, and a wrong-but-warm rank is worse
        # than a PENDING that says why.
        return (
            best[2],
            f"IV store tenor {best[1]} as of {best[0]} "
            f"(no plan-time read for this session — tenor may not match the cell)",
        )
    return resolved, "; ".join(notes) if notes else "plan cells"


def _print_window_arms(
    args: argparse.Namespace, rules: Rulebook, *, t5_defined: bool | None = None
) -> list[str]:
    """Print and return the arms this window may trade (RULED 2026-10-03, R5).

    Printed at startup rather than only on the first entry, because an arm that is
    silently missing from a run is indistinguishable from an arm that found no setups
    — and those two facts need completely different responses from the operator.
    """
    active = rules.active_arms(t5_iv_rank_defined=t5_defined)
    inert = rules.inert_arms(t5_iv_rank_defined=t5_defined)
    print(f"window arms active: {', '.join(active)}")
    for arm in inert:
        gate = rules.window.arm_b_gate
        print(
            f"  arm {arm} is INERT for this window — {gate.requires} is not satisfied "
            f"({gate.status}). It will not place positions and will not be scored as a "
            f"missed setup. {gate.note}"
        )
    return active


def _router(args: argparse.Namespace, rules: Rulebook) -> Any:
    """The order router for this run. Dry run never reads a credential."""
    if args.dry_run:
        return DryRunRouter()
    from executor.alpaca_trading import AlpacaTradingClient

    # AlpacaTradingClient.from_env raises naming the missing variable, never its value.
    client = AlpacaTradingClient.from_env()
    return LiveOrderRouter(client)


# ---------------------------------------------------------------------------
# hunt-plan
# ---------------------------------------------------------------------------


def cmd_hunt_plan(args: argparse.Namespace, now: dt.datetime) -> int:
    rules = _rules(args)
    day = _session_day(now)
    from executor.alpaca_client import AlpacaClient

    client = AlpacaClient.from_env()
    provider = AlpacaWatchData(client)
    calendar = _calendar(rules)
    conn = _open_db(args.db)
    plan = build_hunt_plan(
        data=provider,
        rules=rules,
        day=day,
        calendar=calendar,
        spot=args.spot,
        iv_store=_iv_store(getattr(args, "ivrank", None)),
        flow_gate=_flow_gate(args, conn, rules, day),
    )
    out = Path(args.out) if args.out else PLAN_DIR / f"{day.isoformat()}.json"
    write_plan_file(plan, out)
    print(summarise(plan))
    print(f"\nplan written: {out} (sha256 {hashlib.sha256(out.read_bytes()).hexdigest()[:12]})")

    if conn is not None:
        ids = write_hunt_plan(conn, plan)
        print(f"journaled {len(ids)} plan decision(s): {sorted(ids)}")

    if args.open_arm_a:
        _arm_a(args, rules, plan, now, conn)
    return 0


def _arm_a(
    args: argparse.Namespace,
    rules: Rulebook,
    plan: HuntPlan,
    now: dt.datetime,
    conn: Any,
) -> None:
    """Arm A's single buy-and-hold, or a dry-run description of it."""
    sink = _sink(conn, rules)
    manager = PositionManager(rules, journal=sink)
    router = _router(args, rules)
    state = ArmState(bankroll_usd=rules.arms.A.bankroll_usd)
    reference = plan.arms[0].watch.close if plan.arms else 0.0
    evaluation = manager.evaluate_entry(
        "A", now=now, state=state, price=reference, symbol=rules.arms.A.entry.symbol
    )
    for action in evaluation.actions:
        manager.journal_action(action, now=now, journal=sink)
        router.route(action)
        print(f"arm A: {action.reason}")
    if not evaluation.actions:
        print("arm A: already holds SPY — no action")
    print(f"router: {router.describe()}")


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


def cmd_watch(args: argparse.Namespace, now: dt.datetime) -> int:
    rules = _rules(args)
    day = dt.date.fromisoformat(args.date) if args.date else _session_day(now)
    plan_path = Path(args.plan) if args.plan else PLAN_DIR / f"{day.isoformat()}.json"
    if not plan_path.exists():
        raise CliError(
            f"no plan at {plan_path}; run `hunt-plan` first (the intraday loop reads the "
            f"pre-market artifact rather than rebuilding it)"
        )
    plan = load_plan_file(plan_path)
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()[:12]
    print(f"watch {day} {plan.symbol} plan={plan_path} (sha256 {digest}) v{plan.strategy_version}")

    from executor.alpaca_client import AlpacaClient

    provider = AlpacaWatchData(AlpacaClient.from_env())
    router = _router(args, rules)
    conn = _open_db(args.db)
    sink = _sink(conn, rules)
    state = WatchState.initial(day=day, plan=plan, rules=rules)
    iv_rank, iv_source = _watch_iv_rank(args, plan, rules)
    flow_gate = _flow_gate(args, conn, rules, day)
    if iv_rank is None:
        print(
            "warning: no IV rank for T5 re-verification (no --iv-rank, and the plan "
            "cells carry none) — T5 evaluates PENDING and blocks every arm B/C entry; "
            "pass --iv-rank, or re-run hunt-plan with --ivrank so the store is read",
            file=sys.stderr,
        )
    elif isinstance(iv_rank, Mapping):
        if not iv_rank:
            print(
                "warning: plan recorded no IV rank for any cell — T5 evaluates PENDING "
                "and blocks every arm B/C entry (R4: no fallback to an arbitrary tenor)",
                file=sys.stderr,
            )
        else:
            print(f"T5 iv_rank per cell (source: {iv_source}):")
            for key, value in sorted(iv_rank.items()):
                print(f"  {key}: {value:.2f}")
    else:
        print(f"T5 iv_rank={iv_rank:.2f} (source: {iv_source})")
    _print_window_arms(args, rules, t5_defined=bool(iv_rank))
    results = run_loop(
        provider=provider,
        rules=rules,
        state=state,
        router=router,
        journal=sink,
        calendar=_calendar(rules),
        poll_seconds=args.poll_seconds,
        stop_at_et=args.stop_et,
        max_ticks=args.ticks,
        iv_rank=iv_rank,
        flow_gate=flow_gate,
    )
    for result in results:
        line = (
            f"{to_et(result.now).isoformat(timespec='seconds')} "
            f"age={result.snapshot_age_seconds:.0f}s stale={result.stale} "
            f"exits={len(result.exit_actions)} entries={len(result.entry_actions)} "
            f"vetoes={len(result.vetoes)} journaled={result.journaled}"
        )
        print(line)
        for note in result.notes:
            print(f"    · {note}")
    print(router.describe())
    return 0


# ---------------------------------------------------------------------------
# eod
# ---------------------------------------------------------------------------


def cmd_eod(args: argparse.Namespace, now: dt.datetime) -> int:
    rules = _rules(args)
    day = dt.date.fromisoformat(args.date) if args.date else _session_day(now)
    plan_path = Path(args.plan) if args.plan else PLAN_DIR / f"{day.isoformat()}.json"
    plan = load_plan_file(plan_path) if plan_path.exists() else None

    client = _eod_client(args)
    if args.offline:
        print("OFFLINE: market data comes from the fixture transport; nothing was fetched")
    # FIX 2026-10-03 (live seam sweep): this read was `limit=400` with NO window, which
    # the live route answers with zero bars and HTTP 200 — the same shape that made
    # `hunt-plan` die with "daily series has no bars" and made the intraday spot read
    # raise. It surfaced here as `InsufficientData: EMA(50) needs at least 50 values,
    # got 0`, i.e. downstream of the actual fault: the EOD pass read nothing and the
    # indicator stack blamed itself. MEASURED today, paper creds, `1Day`/sip:
    # limit-only -> 0 bars; end-only -> 0 bars; bar_window(400) -> 400 bars.
    #
    # One policy, imported: `bar_window` lives in `executor.bar_windows` because
    # `executor.watch_loop`, this function, and `executor.soak._AlpacaSource` all needed
    # the same answer and each had found (or not found) it for itself.
    daily = client.get_daily_bars(
        rules.strategy.symbol,
        feed=EOD_BAR_FEED,
        limit=EOD_BARS_LIMIT,
        **bar_window(timeframe="1Day", feed=EOD_BAR_FEED, limit=EOD_BARS_LIMIT),
    )
    chain = client.get_option_chain(rules.strategy.symbol)
    session_bar = _session_bar(daily, day)

    conn = _open_db(args.db)

    # 1. mark arm C's T1 streak (the only place "consecutive EOD" is knowable)
    streak, ema_now = _ema50_streak(daily, day)
    print(f"{day}: close-vs-50EMA streak = {streak} (50EMA={ema_now})")

    # 2. positions + marks
    positions = (
        client.get_positions() if (not args.dry_run and not args.offline) else []
    )
    for position in positions:
        print(
            f"position {position.symbol} qty={position.qty} "
            f"value={position.market_value} upnl={position.unrealized_pl}"
        )

    # 3. NO-SHOTs from the plan
    #
    # FIX 2026-10-03 (full-cycle rehearsal §3 items 8/9): the EOD pass now passes both
    # `taken` and `decision_ids` to ``build_noshots``. Without `taken`, a session that
    # both saw the setup AND traded still wrote a row worded as a skip; without
    # `decision_ids` every row landed with counterfactual_entry_ref = NULL, severing
    # the link between a counterfactual and the decision that produced it. Both facts
    # are read out of the journal itself — the ledger cannot claim a skip when the
    # journal records the trade.
    drafts: list[Any] = []
    taken: list[tuple[str, str]] = []
    decision_ids: dict[tuple[str, str], int] = {}
    if plan is not None and conn is not None:
        session_decisions = _session_decisions(conn, day)
        taken_keys, unmapped = _taken_keys(session_decisions)
        taken = sorted(taken_keys)
        decision_ids = _plan_decision_ids(plan, session_decisions, day)
        print(
            f"session journal: {len(session_decisions)} decision(s); traded "
            f"{sorted(f'{a}/{d}' for a, d in taken) or 'nothing'}; plan rows linked "
            f"{len(decision_ids)}/{len(plan.arms)}"
        )
        for row in unmapped:
            print(
                f"warning: TRADE decision {row} could not be mapped to an "
                "(arm, direction) key — its cell is treated as NOT taken, so a cell "
                "that traded can still be recorded as a skip",
                file=sys.stderr,
            )
        drafts = build_noshots(
            plan,
            setup_bar=session_bar,
            chain=chain,
            rules=rules,
            taken=taken,
            decision_ids=decision_ids,
            ts=iso_ts(now),
        )
        ids = write_noshots(conn, drafts)
        print(f"wrote {len(ids)} NO-SHOT row(s): {ids}")
        # INTERPRETATION: a sighting whose plan row is missing from the journal is still
        # written — it is real evidence that the setup was sighted — but it is reported
        # loudly, because an unattributed counterfactual cannot be traced back to the
        # decision that produced it. Refusing the row instead would delete a genuine
        # sighting over a bookkeeping gap, which is the worse failure for a ledger whose
        # job is to record what was *not* done.
        unlinked = [
            f"{d.arm}/{d.direction}" for d in drafts if d.decision_ref is None
        ]
        if unlinked:
            print(
                f"warning: counterfactual(s) with no decision link: {unlinked} — no "
                "pre-market plan row was journaled for those cells, so these rows land "
                "with counterfactual_entry_ref = NULL and cannot be attributed",
                file=sys.stderr,
            )

    # 4. journal close
    if conn is not None:
        from journal.store import DecisionEntry, append_decision, set_meta

        append_decision(
            conn,
            DecisionEntry(
                ts=iso_ts(now),
                arm="B",
                kind="NO_TRADE",
                checklist_snapshot={
                    "enforcement": {"session_bar": session_bar.c if session_bar else None},
                    "indicators": {"ema50": ema_now, "streak_below_ema50": streak},
                },
                checklist_state={
                    "phase": "eod_close",
                    "positions": [p.__dict__ for p in positions],
                    "noshots": len(drafts),
                    "taken": [f"{arm}/{direction}" for arm, direction in taken],
                },
                reasoning=(
                    f"EOD close for {day}: marked positions, streak "
                    f"{streak} close(es) below 50EMA, NO-SHOTs written"
                    + (
                        f"; traded {[f'{a}/{d}' for a, d in taken]}"
                        if taken
                        else "; nothing traded"
                    )
                ),
                strategy_version=rules.strategy_version,
                symbol=rules.strategy.symbol,
            ),
        )
        set_meta(conn, "last_eod", {"date": day.isoformat(), "streak": streak})
        print("journal closed for the day")
    return 0


def _eod_client(args: argparse.Namespace) -> Any:
    """The EOD pass's market-data client: live, or a fixture transport offline.

    # INTERPRETATION: ``--offline`` is a *transport* swap, not a second code path. The
    offline client is a real :class:`~executor.alpaca_client.AlpacaClient` over
    :class:`~executor.alpaca_client.MockTransport`, so the same parsing, the same
    doc-verified query-parameter contract and the same failure modes run — including
    the 404 an absent fixture route raises, which is the whole point of running the
    real CLI offline rather than replaying its steps. Credentials are placeholders and
    are never sent anywhere.
    """
    from executor.alpaca_client import AlpacaClient

    if not args.offline:
        return AlpacaClient.from_env()

    path = getattr(args, "offline_fixture", None)
    if not path:
        raise CliError(
            "--offline needs --offline-fixture <file.json>: a JSON object mapping "
            "Alpaca request paths to canned payloads (or {\"routes\": {...}}), shaped "
            "like the documented responses — /v2/stocks/SPY/bars (flat 'bars' list) "
            "and /v1beta1/options/snapshots/SPY (symbol-keyed 'snapshots'). No fixture "
            "means no data, and a silent empty series would produce a streak of 0."
        )
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    routes = raw.get("routes", raw) if isinstance(raw, dict) else None
    if not isinstance(routes, dict):
        raise CliError(f"{path}: expected a JSON object of request path -> payload")

    from executor.alpaca_client import MockTransport

    return AlpacaClient(
        transport=MockTransport(routes), key="offline-fixture", secret="offline-fixture"
    )


def _et_tz(day: dt.date) -> dt.tzinfo:
    """The ET zone for ``day`` — DST-correct, because the plan cutoff is a wall clock."""
    return eastern(dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.UTC))


def _et_date(ts: str) -> dt.date | None:
    """The ET session date of a decision's UTC timestamp, or ``None`` if unparseable."""
    try:
        moment = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return to_et(moment).date() if moment.tzinfo is not None else None


def _session_decisions(conn: Any, day: dt.date) -> list[Any]:
    """Every journal decision written on the ET session ``day``, in journal order."""
    from journal.store import list_decisions

    return [entry for entry in list_decisions(conn) if _et_date(entry.ts) == day]


def _occ_right(symbol: str | None) -> str | None:
    """``"C"``/``"P"`` parsed out of an OCC contract symbol, or ``None``.

    OCC 2024-01: 1–6 char root, 6-digit ``YYMMDD``, ``C``/``P``, 8-digit strike. The
    strike and the date are checked as digits so an equity ticker (which has no right
    letter) returns ``None`` rather than a letter from the middle of a name.
    """
    if not symbol or len(symbol) < 17:
        return None
    right, expiry, strike = symbol[-9], symbol[-15:-9], symbol[-8:]
    if right in ("C", "P") and expiry.isdigit() and strike.isdigit():
        return right
    return None


def _taken_keys(decisions: Sequence[Any]) -> tuple[set[tuple[str, str]], list[str]]:
    """``(arm, direction)`` keys that ended up with a position, plus unmappable rows.

    Read from the journal's own TRADE rows — the ledger's record of truth — rather than
    from broker positions, because a position opened and closed inside the session is a
    trade the broker no longer reports and the NO-SHOT cell still must not be recorded
    as a skip. :class:`~executor.alpaca_trading` writes one TRADE decision per routed
    entry action, live and dry.

    # INTERPRETATION: the direction comes from the contract's OCC right, so arm B's put
    cell and call cell are told apart by what was actually bought. Arm C is a long-call
    arm in this rulebook, so a ``P`` there would be a rulebook violation rather than a
    parsing problem — it is mapped as a put and shows up as a cell key no plan carries,
    which is visible rather than silently folded into the call cell.
    """
    taken: set[tuple[str, str]] = set()
    unmapped: list[str] = []
    for entry in decisions:
        if entry.kind != "TRADE" or entry.arm not in ("B", "C"):
            continue
        right = _occ_right(entry.symbol)
        if right is None:
            unmapped.append(f"#{entry.id} {entry.arm} {entry.symbol or '<no symbol>'}")
            continue
        taken.add((entry.arm, "put" if right == "P" else "call"))
    return taken, unmapped


def _plan_decision_ids(
    plan: HuntPlan, decisions: Sequence[Any], day: dt.date
) -> dict[tuple[str, str], int]:
    """``{(arm, direction): decision_id}`` for the plan rows this session ran on.

    Each counterfactual links to the decision that produced it, so this has to pick the
    *plan* row and not any other row for the same cell: the watch loop journals its own
    NO_TRADE / PROPOSAL / VETO rows for the same ``(arm, direction)`` intraday.

    # INTERPRETATION: the pre-market plan is what runs before the session open, so a
    decision journaled at or after 09:30 ET is intraday machinery and is skipped. The
    newest pre-open row for a cell wins, because a plan rewritten before the open
    supersedes the one it replaced — and it is still the decision the day's execution
    was actually taken against.
    """
    cutoff = dt.datetime.combine(day, PLAN_CUTOFF_ET, tzinfo=_et_tz(day))
    ids: dict[tuple[str, str], int] = {}
    for cell in plan.arms:
        best: int | None = None
        for entry in decisions:
            if entry.arm != cell.arm or entry.kind != cell.decision_kind or entry.id is None:
                continue
            moment = dt.datetime.fromisoformat(entry.ts.replace("Z", "+00:00"))
            if moment >= cutoff:
                continue
            if best is None or entry.id > best:
                best = entry.id
        if best is not None:
            ids[cell.key] = best
    return ids


def _session_bar(daily: BarSeries, day: dt.date) -> Any:
    for bar in reversed(daily.bars):
        if bar.t.date() == day:
            return bar
    return None


def _ema50_streak(daily: BarSeries, day: dt.date) -> tuple[int, float | None]:
    """Consecutive EOD closes below the 50EMA, counting back from ``day``.

    # INTERPRETATION: the EMA is taken over the full daily series *including* the
    session being judged — the same series the checklist's T1 reads, so the exit rule
    and the entry rule cannot disagree about what the 50EMA is. Bars with no EMA value
    (the first 49) are skipped; a bar with a value is either below or above, so the
    backwards walk terminates. If the session's own bar is not in the series yet (an
    EOD pass run before Alpaca publishes the daily bar), the walk starts from the
    previous session — which is stated here rather than silently producing a streak
    that includes no session data.
    """
    aligned = ema([b.c for b in daily.bars], 50)
    rows = [
        (bar.t.date(), bar.c, value)
        for bar, value in zip(daily.bars, aligned, strict=False)
        if value is not None and bar.t.date() <= day
    ]
    if not rows:
        return 0, None
    streak = 0
    for _date, close, value in reversed(rows):
        if close < value:
            streak += 1
        else:
            break
    return streak, rows[-1][2]


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "offline", False) and not args.dry_run:
            raise CliError(
                "--offline cannot be combined with --live: offline mode reads fixtures "
                "and journals locally; it never routes an order"
            )
        if not args.dry_run:
            # Fail fast on a missing credential before any work happens. The refusal
            # names the variable, never its value, and happens before a single bar is
            # fetched — so "--live with no keys" cannot half-run a session.
            from executor.alpaca_trading import AlpacaTradingClient

            AlpacaTradingClient.from_env()
        now = _parse_now(args.now)
        if args.command == "hunt-plan":
            return cmd_hunt_plan(args, now)
        if args.command == "watch":
            return cmd_watch(args, now)
        if args.command == "eod":
            return cmd_eod(args, now)
        parser.error(f"unknown command {args.command!r}")
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a CLI prints the failure, it does not traceback at users
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
