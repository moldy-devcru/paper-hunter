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
from pathlib import Path
from typing import Any

from config.loader import DEFAULT_RULES_PATH, Rulebook, load_rules
from data.event_calendar import EventCalendar
from executor.alpaca_client import BarSeries
from executor.hunt_plan import HuntPlan, build_hunt_plan, summarise, write_hunt_plan
from executor.indicators import ema
from executor.noshot import build_noshots, write_noshots
from executor.position_manager import (
    ArmState,
    MemoryJournalSink,
    PositionManager,
    SqliteJournalSink,
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

    watch_cmd = sub.add_parser("watch", help="intraday trigger loop")
    watch_cmd.add_argument(
        "--plan", default=None, help="plan JSON (default data/plans/<date>.json)"
    )
    watch_cmd.add_argument("--date", default=None)
    watch_cmd.add_argument("--poll-seconds", type=float, default=15.0)
    watch_cmd.add_argument("--stop-et", default="16:00")
    watch_cmd.add_argument("--ticks", type=int, default=None, help="stop after N ticks")

    eod_cmd = sub.add_parser("eod", help="end of day: mark, enforce, journal, NO-SHOTs")
    eod_cmd.add_argument("--plan", default=None)
    eod_cmd.add_argument("--date", default=None)
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
    plan = build_hunt_plan(
        data=provider,
        rules=rules,
        day=day,
        calendar=calendar,
        spot=args.spot,
    )
    out = Path(args.out) if args.out else PLAN_DIR / f"{day.isoformat()}.json"
    write_plan_file(plan, out)
    print(summarise(plan))
    print(f"\nplan written: {out} (sha256 {hashlib.sha256(out.read_bytes()).hexdigest()[:12]})")

    conn = _open_db(args.db)
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

    from executor.alpaca_client import AlpacaClient

    client = AlpacaClient.from_env()
    daily = client.get_daily_bars(rules.strategy.symbol, feed="sip", limit=400)
    chain = client.get_option_chain(rules.strategy.symbol)
    session_bar = _session_bar(daily, day)

    conn = _open_db(args.db)

    # 1. mark arm C's T1 streak (the only place "consecutive EOD" is knowable)
    streak, ema_now = _ema50_streak(daily, day)
    print(f"{day}: close-vs-50EMA streak = {streak} (50EMA={ema_now})")

    # 2. positions + marks
    positions = client.get_positions() if not args.dry_run else []
    for position in positions:
        print(
            f"position {position.symbol} qty={position.qty} "
            f"value={position.market_value} upnl={position.unrealized_pl}"
        )

    # 3. NO-SHOTs from the plan
    drafts: list[Any] = []
    if plan is not None and conn is not None:
        drafts = build_noshots(
            plan,
            setup_bar=session_bar,
            chain=chain,
            rules=rules,
            ts=iso_ts(now),
        )
        ids = write_noshots(conn, drafts)
        print(f"wrote {len(ids)} NO-SHOT row(s): {ids}")

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
                },
                reasoning=(
                    f"EOD close for {day}: marked positions, streak "
                    f"{streak} close(es) below 50EMA, NO-SHOTs written"
                ),
                strategy_version=rules.strategy_version,
                symbol=rules.strategy.symbol,
            ),
        )
        set_meta(conn, "last_eod", {"date": day.isoformat(), "streak": streak})
        print("journal closed for the day")
    return 0


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
