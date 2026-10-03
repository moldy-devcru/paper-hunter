"""Full-cycle dry rehearsal — the whole pipeline, offline, against a throwaway world.

Run it with ``python -m executor.rehearsal`` (or ``scripts/rehearse.sh``). It exercises
every stage the daily cron trio touches, in order, in one process:

1. **rules** — load the shipped rulebook, list what is still unfrozen.
2. **calibration** — read T6's proposed ``N`` off a *copy* of the deployed
   ``flow_baseline`` (read-only; the copy lives in a temp dir).
3. **iv-warmup** — read the deployed IV store (again read-only) and report how far T5's
   rank is from being defined.
4. **hunt-plan / no-shot session** — a fixture session that does not qualify: a plan
   whose cells are all NO_TRADE, journaled, plus the EOD NO-SHOT ledger for it.
5. **hunt-plan / shot session** — a fixture session that *does* qualify (a real bar
   series, a real snapshot, the real checklist: T1-T6 all PASS), plan written and
   journaled.
6. **watch / quiet** — intraday ticks on the no-shot plan: nothing fires, and the ticks
   say so.
7. **watch / stale** — a trigger with a stale snapshot: entries refused, exits still
   evaluated.
8. **watch / entry** — the shot session: trigger fires, the checklist re-verifies live,
   the entry governor passes, the router records the action and sends nothing.
9. **exits + roll** — arm B's profit take and hard close on the open position, and arm
   C's roll ladder with a supplied replacement leg.
10. **eod + soak** — the EOD pass for the shot session (streak, NO-SHOTs with the taken
    cell marked, journal close) and one soak pass that writes a ``flow_baseline`` row and
    IV observations into the temp journal.
11. **analysis** — shadow-roll open/mark, the scorecard, and the weekly rollup.
12. **integrity** — assert what must be true at the end: no orders sent, no network, no
    writes outside the temp dir, the journal's append-only counters intact.

Design constraints, all load-bearing:

* **Zero network.** Every read comes from a fixture object in this module or from a
  temp copy of a deployed file. No Alpaca client is constructed, no credential is read,
  and the only router used is :class:`~executor.watch_loop.DryRunRouter`.
* **Zero writes under ``/opt`` (or anywhere else that matters).** Deployed files are
  opened read-only and copied into the temp dir; the journal, the IV store and the plan
  files are all created there.
* **The frozen rulebook is not edited.** T6's multiplier and the deep-OTM thresholds are
  ``calibration_pending`` in the shipped file, which means no cell can ever fire — so
  the *shot* branch runs against a **simulated freeze**: the shipped rules with those
  fields filled in from this run's own calibration proposal, in memory and in a temp
  file. That is a rehearsal of "what the pipeline does once the review has happened",
  and it is labelled as such everywhere it appears. No rulebook semantics change.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from analysis import rollup, shadow_roll
from config.loader import DEFAULT_RULES_PATH, load_rules, load_rules_text
from data.event_calendar import EventCalendar
from executor.alpaca_client import (
    Bar,
    BarSeries,
    Greeks,
    OptionChain,
    OptionContract,
    OptionQuote,
    OptionTrade,
)
from executor.calibration import flow_calibration
from executor.checklist import FlowGate
from executor.flow_gate import flow_gate_from_journal
from executor.hunt_plan import (
    StaticMarketData,
    build_hunt_plan,
    summarise,
    write_hunt_plan,
)
from executor.iv_rank import IvRankStore, tenor_key
from executor.noshot import build_noshots, write_noshots
from executor.position_manager import (
    ArmState,
    ManagedPosition,
    MemoryJournalSink,
    OrderLeg,
    PositionManager,
)
from executor.soak import run_soak
from executor.watch_loop import (
    DryRunRouter,
    StaticWatchData,
    WatchState,
    run_loop,
    run_once,
)
from journal.store import (
    DecisionEntry,
    append_decision,
    get_meta,
    init_db,
    list_decisions,
    list_noshots,
    set_meta,
)

ET = dt.timezone(dt.timedelta(hours=-4))
UTC = dt.UTC

#: Where the deployed files live. Read-only, and only if they exist.
DEPLOYED_JOURNAL = Path("/opt/paper-hunter/data/journal.db")
DEPLOYED_IVRANK = Path("/opt/paper-hunter/data/ivrank.db")
#: Everything the rehearsal must NOT touch. Snapshotted before the run and compared in
#: the integrity stage, so "no writes under /opt" is measured rather than promised.
DEPLOYED_ROOT = Path("/opt/paper-hunter")

WARMUP_BARS = 260
SIM_START = dt.date(2024, 6, 3)


class RehearsalError(RuntimeError):
    """The rehearsal cannot proceed honestly."""


# ---------------------------------------------------------------------------
# fixture market data (deterministic; no network, no randomness at run time)
# ---------------------------------------------------------------------------


def _weekday(day: dt.date) -> dt.date:
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def _noise(i: int) -> float:
    """A deterministic pseudo-noise in [-0.5, 0.5). Not random: the journal requires
    reproducible indicator values, and a fixture that changes between runs would hide
    exactly the kind of drift this rehearsal exists to find."""
    return ((math.sin(i * 12.9898) * 43758.5453) % 1.0) - 0.5


def build_series(
    *,
    bars: int = WARMUP_BARS,
    drift: float,
    noise: float,
    final_return: float,
    final_wick: float,
    final_volume: float,
    start: float = 450.0,
    start_date: dt.date = SIM_START,
    volume_base: float = 55_000_000.0,
) -> BarSeries:
    """A daily series whose LAST bar is shaped by the arguments.

    The last bar is the signal bar for a pre-market plan and the newest bar for a
    live re-verification, so its shape is what decides T3b (low tags the lower band and
    the close finishes back inside), T2b (MACD histogram expanding) and T4 (relative
    volume). Everything before it is a slow trend plus deterministic noise, which is
    what makes T1/T2 (aligned EMAs, RSI in band) defined rather than pending.
    """
    out: list[Bar] = []
    price = start
    day = start_date
    i = 0
    while len(out) < bars:
        if day.weekday() < 5:
            last = len(out) == bars - 1
            ret = drift + _noise(i) * noise
            if last:
                ret = final_return
            open_ = price
            close = open_ * (1.0 + ret)
            high = max(open_, close) * 1.0005
            low = min(open_, close) * 0.9995
            volume = volume_base * (1.0 + 0.05 * math.sin(i / 9.0))
            if last:
                low = close * (1.0 - final_wick)
                high = max(high, close * 1.001)
                volume = volume_base * final_volume
            out.append(
                Bar(
                    t=dt.datetime.combine(day, dt.time(21, 0), tzinfo=UTC),
                    o=round(open_, 4),
                    h=round(high, 4),
                    l=round(low, 4),
                    c=round(close, 4),
                    v=round(volume, 0),
                    n=480_000 + i,
                    vw=round(close, 4),
                )
            )
            price = close
            i += 1
        day += dt.timedelta(days=1)
    return BarSeries(symbol="SPY", timeframe="1Day", feed="sip", bars=out)


#: The shape the entry path actually needs, and it is NOT "yesterday looked perfect".
#:
#: ``compute_triggers`` emits a price level only for a blocking condition that is not
#: PASS, so a plan whose T1..T6 all pass on the prior close carries **no** actionable
#: trigger and the watch loop can never enter it. The reachable arm B setup is the one
#: the brief describes: yesterday closed on a washout (under the lower band, under the
#: fast EMA, on heavy volume), the plan is NO_TRADE but holds T1's EMA level and T3b's
#: band edge, and today reclaiming both completes the rejection live. These parameters
#: produce that signal bar: an established uptrend whose last session breaks the band
#: and closes below the fast EMA on 1.8x relative volume.
#:
#: T2b (MACD histogram expanding) is then genuinely FAIL on the signal bar, which is
#: what the intraday re-verification is *for*: the reclaim bar has to bring the
#: histogram back with it.
FIRING_PARAMS = {
    "drift": 0.0004,
    "noise": 0.008,
    "final_return": -0.020,
    "final_wick": 0.030,
    "final_volume": 1.8,
}

#: The session's own bar as the watch loop sees it intraday: a reclaim. This is the bar
#: that has to satisfy every condition live, and it is the same bar the EOD pass later
#: summarises, so the ledger and the entry agree about what the day did.
RECLAIM_RETURN = 0.030
RECLAIM_VOLUME = 1.8

#: The no-shot session is a **near miss**, not a rout: the reclaim has already happened
#: on the signal bar, every condition passes except one, and T4 (relative volume) is the
#: one that says no. That is the case the NO-SHOT ledger exists to measure, and it is
#: only a sighting when exactly one blocking condition failed
#: (``executor.noshot.DEFAULT_MARGINAL_LIMIT = 1``).
NOSHOT_PARAMS = {
    "drift": 0.0004,
    "noise": 0.008,
    "final_return": 0.005,
    "final_wick": 0.028,
    "final_volume": 0.9,
}


def firing_series(**overrides: Any) -> BarSeries:
    return build_series(**{**FIRING_PARAMS, **overrides})


def quiet_series(**overrides: Any) -> BarSeries:
    return build_series(**{**NOSHOT_PARAMS, **overrides})


def make_contract(
    *,
    symbol: str,
    underlying: str,
    expiry: str,
    strike: float,
    right: str,
    ask: float,
    delta: float | None = None,
    iv: float = 0.18,
) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        right=right,  # type: ignore[arg-type]
        implied_volatility=iv,
        greeks=Greeks(delta=delta),
        latest_quote=OptionQuote(bid=round(ask - 0.05, 2), ask=ask),
        latest_trade=OptionTrade(p=ask),
    )


def zero_dte_chain(
    *,
    day: dt.date,
    spot: float,
    step: float = 1.0,
    ask: float = 1.40,
    low: int = -12,
    high: int = 40,
) -> OptionChain:
    """A same-expiry chain around ``spot``.

    The range is deliberately wide on the upside: arm B's strike rule picks the first
    OTM strike beyond the setup-day-range projection, which for a volatile signal bar
    sits a full range *above* the high. A chain that stopped at spot+N would leave the
    cell with no contract, no trigger and therefore an unreachable entry — a fixture
    limitation that would masquerade as a strategy limitation.
    """
    expiry = day.strftime("%Y%m%d")
    contracts = [
        make_contract(
            symbol=f"SPY{expiry}{'C' if i > 0 else 'P'}{int((spot + i * step) * 1000):08d}",
            underlying="SPY",
            expiry=expiry,
            strike=round(spot + i * step, 2),
            right="call" if i > 0 else "put",
            ask=ask,
        )
        for i in range(low, high + 1)
        if i != 0
    ]
    return OptionChain(underlying="SPY", feed="indicative", contracts=tuple(contracts),
        fetched_at=dt.datetime.combine(day, dt.time(17, 30), tzinfo=UTC))


def arm_c_chain(*, day: dt.date, spot: float) -> OptionChain:
    """Long-dated calls for arm C's selection (DTE window + delta floor)."""
    contracts = []
    for dte, strike, delta in (
        (60, spot - 20, 0.88),      # DTE too short
        (120, spot - 20, 0.55),     # delta too low
        (120, spot - 12, 0.85),     # qualifying
        (200, spot - 30, 0.95),     # DTE too long
    ):
        exp = (day + dt.timedelta(days=dte)).strftime("%Y%m%d")
        premium = max(0.20, round((1.0 - delta) * 40.0 + 2.0, 2))
        contracts.append(
            make_contract(
                symbol=f"SPY{exp}C{int(strike * 1000):08d}",
                underlying="SPY",
                expiry=exp,
                strike=strike,
                right="call",
                ask=premium,
                delta=delta,
            )
        )
    return OptionChain(underlying="SPY", feed="indicative", contracts=tuple(contracts),
        fetched_at=dt.datetime.combine(day, dt.time(17, 30), tzinfo=UTC))


class FixtureSource:
    """The soak's data seam, offline. Satisfies ``executor.soak.SoakData``."""

    def __init__(self, bars: BarSeries, chain: OptionChain) -> None:
        self._bars = bars
        self._chain = chain

    def daily_bars(self, symbol: str) -> BarSeries:
        return self._bars

    def option_chain(self, symbol: str) -> OptionChain:
        return self._chain


# ---------------------------------------------------------------------------
# the simulated freeze
# ---------------------------------------------------------------------------


def rehearsal_rules_text(
    *,
    n: float,
    threshold_pct: float,
    arm_c_iv_rank_max: float = 50.0,
) -> str:
    """The shipped rulebook as text, with its pending calibrations filled in.

    Text (not an object) so the file written to the temp dir and the in-memory
    rulebook the pipeline uses are the same bytes — and therefore the same
    ``strategy_version`` fingerprint. A rehearsal that reported a fingerprint nobody
    could reproduce from the file beside it would be lying in a very small way.
    """
    import json as _json

    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    reason = "REHEARSAL SIMULATION of the first-monthly-review freeze; not a rulebook edit"
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": round(float(n), 4),
        "calibration_pending": False,
        "reason": reason,
    }
    for side in ("calls", "puts"):
        data["checklist"]["t6_flow"]["deep_otm"][side] = {
            "value": float(threshold_pct),
            "calibration_pending": False,
            "reason": reason,
        }
    data["checklist"]["t5_options_chain"]["arm_c"]["calibration_pending"] = False
    data["checklist"]["t5_options_chain"]["arm_c"]["iv_rank_max"] = float(arm_c_iv_rank_max)
    return _json.dumps(data, indent=2, sort_keys=True) + "\n"


def rehearsal_rules(
    *,
    n: float,
    threshold_pct: float,
    arm_c_iv_rank_max: float = 50.0,
) -> Any:
    """The shipped rulebook with its pending calibrations FILLED IN, for this run only.

    The shipped file is untouched. T6's multiplier, both deep-OTM thresholds and arm C's
    IV ceiling are ``calibration_pending`` there, and a pending gate is blocking by
    construction — so with the shipped rules *no cell can ever fire* and the entry,
    roll and exit branches are unreachable. Filling them in is how a rehearsal exercises
    "the pipeline after the monthly review" without editing the pre-registration.

    ``strategy.version`` is left alone: the loader validates the shape, and a fake
    version string in a temp journal is a worse lie than a clearly-labelled fixture
    rulebook with a recorded reason string on every value it filled.
    """
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    reason = "REHEARSAL SIMULATION of the first-monthly-review freeze; not a rulebook edit"
    data["checklist"]["t6_flow"]["multiplier"] = {
        "value": round(float(n), 4),
        "calibration_pending": False,
        "reason": reason,
    }
    for side in ("calls", "puts"):
        data["checklist"]["t6_flow"]["deep_otm"][side] = {
            "value": float(threshold_pct),
            "calibration_pending": False,
            "reason": reason,
        }
    data["checklist"]["t5_options_chain"]["arm_c"]["calibration_pending"] = False
    data["checklist"]["t5_options_chain"]["arm_c"]["iv_rank_max"] = float(arm_c_iv_rank_max)
    return load_rules_text(yaml.safe_dump(data))


# ---------------------------------------------------------------------------
# the IV store
# ---------------------------------------------------------------------------


def seed_iv_store(
    path: Path,
    *,
    day: dt.date,
    chain: OptionChain,
    spot: float,
    days: int = 70,
) -> tuple[IvRankStore, dict[str, Any]]:
    """A warm IV store: ``days`` observations for every tenor in ``chain``.

    Every tenor matters. T5 keys its rank on the contract the plan actually selected
    (arm B's 0DTE strike, arm C's long-dated call), and a store warmed on some other
    tenor would hand the plan an undefined rank and quietly turn the shot branch into
    a PENDING one — which is a fixture lie, not a fixture shortcut.

    ``days`` defaults to 70 because ``IvRankStore.MIN_OBSERVATIONS`` is 60 — the
    rehearsal needs a *defined* rank, and how long the real store takes to get there is
    a separate (reported) fact, not something a fixture should paper over.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    store = IvRankStore(conn)
    keys: dict[str, str] = {}
    for contract in chain.contracts:
        key = tenor_key(
            expiry=contract.expiry, right=contract.right, strike=contract.strike
        )
        keys.setdefault(key, contract.symbol)
    written = 0
    for key in keys:
        for i in range(days):
            as_of = day - dt.timedelta(days=days - i)
            # A history centred ABOVE the chain's 0.18 IV, so the current observation
            # lands in the lower part of the distribution and the rank is inside arm C's
            # ratified iv_rank_max=50 ceiling instead of pinned at 100.
            store.record(
                underlying="SPY",
                as_of=as_of,
                tenor=key,
                iv=round(0.20 + 0.03 * math.sin(i / 3.0), 6),
                source="rehearsal_fixture",
                expiry=key.split("-")[0],
                right=key.split("-")[1],
                strike=float(key.split("-")[2]),
            )
            written += 1
    conn.commit()
    ranks: dict[str, float | None] = {}
    for contract in chain.contracts:
        key = tenor_key(expiry=contract.expiry, right=contract.right, strike=contract.strike)
        ranks[key] = store.iv_rank(
            contract.implied_volatility or 0.18, "SPY", key, as_of=day
        ).rank
    return store, {
        "tenor_keys": len(keys),
        "observations_written": written,
        "observations_per_tenor": days,
        "chain_iv": 0.18,
        "rank_min": min(v for v in ranks.values() if v is not None) if ranks else None,
        "rank_max": max(v for v in ranks.values() if v is not None) if ranks else None,
        "min_observations": store.min_observations,
        "all_defined": all(v is not None for v in ranks.values()),
    }


def deployed_ivrank_readiness(path: Path = DEPLOYED_IVRANK) -> dict[str, Any]:
    """How far the DEPLOYED IV store is from being able to score T5. Read-only."""
    if not path.exists():
        return {"present": False, "path": str(path)}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM iv_observations").fetchone()[0]
        tenors = conn.execute(
            "SELECT COUNT(DISTINCT tenor_key) FROM iv_observations"
        ).fetchone()[0]
        span = conn.execute("SELECT MIN(as_of), MAX(as_of) FROM iv_observations").fetchone()
        deepest = conn.execute(
            "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM iv_observations GROUP BY tenor_key)"
        ).fetchone()[0]
        store = IvRankStore(conn)
        return {
            "present": True,
            "path": str(path),
            "observations": total,
            "tenor_keys": tenors,
            "first_seen": span[0],
            "last_seen": span[1],
            "deepest_tenor_observations": deepest,
            "min_observations": store.min_observations,
            "tenor_reaching_min": bool(deepest and deepest >= store.min_observations),
            "t5_rank_defined": bool(deepest and deepest >= store.min_observations),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# report plumbing
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Stage:
    name: str
    ok: bool = True
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)
    lines: list[str] = dataclasses.field(default_factory=list)
    findings: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None

    def say(self, line: str) -> None:
        self.lines.append(line)

    def find(self, text: str) -> None:
        self.findings.append(text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "error": self.error,
            "detail": self.detail,
            "lines": self.lines,
            "findings": self.findings,
        }


class Rehearsal:
    """Runs the stages in order and accumulates the report."""

    def __init__(self, workdir: Path, *, calibration_db: Path | None = None) -> None:
        self.workdir = workdir
        self.stages: list[Stage] = []
        self.calibration_db = calibration_db
        self.router = DryRunRouter()
        self.journal: sqlite3.Connection | None = None
        self.conn_out = self.workdir / "journal.db"
        self.iv_path = self.workdir / "ivrank.db"
        self.proposal: Any = None
        self.rules: Any = None
        self.shipped_rules: Any = None
        self.plans: dict[str, Any] = {}
        self.notes: dict[str, Any] = {}

    # -- helpers ------------------------------------------------------------

    def stage(self, name: str) -> Stage:
        st = Stage(name=name)
        self.stages.append(st)
        return st

    def run_stage(self, name: str, fn: Callable[[Rehearsal, Stage], None]) -> Stage:
        st = self.stage(name)
        try:
            fn(self, st)
        except Exception as exc:  # noqa: BLE001 - a failed stage is a finding, not a crash
            st.ok = False
            st.error = f"{type(exc).__name__}: {exc}"
        return st

    @property
    def conn(self) -> sqlite3.Connection:
        if self.journal is None:
            raise RehearsalError("journal not initialised")
        return self.journal

    def to_dict(self) -> dict[str, Any]:
        return {
            "workdir": str(self.workdir),
            "ok": all(s.ok for s in self.stages),
            "stages": [s.to_dict() for s in self.stages],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


def _start_for_last(last: dt.date, count: int) -> dt.date:
    """The start date that puts ``count`` weekday bars, the last one on ``last``."""
    day = last
    seen = 1
    while seen < count:
        day -= dt.timedelta(days=1)
        if day.weekday() < 5:
            seen += 1
    return day


def _first_clean_session(
    after: dt.date, calendar: EventCalendar, veto_kinds: list[str], *, span: int = 10
) -> tuple[dt.date, list[str]]:
    """The first weekday after ``after`` that the event calendar does not veto.

    A fixture session that lands on FOMC or CPI would exercise the *veto* branch, not
    the strategy branch, so the rehearsal picks its own date — and records the
    calendar's coverage so an out-of-coverage day (veto: false only because nothing is
    known) is visible in the report rather than silent.
    """
    first, last = calendar.coverage()
    warnings: list[str] = []
    if first and last and not (first <= after <= last + dt.timedelta(days=span)):
        warnings.append(
            f"event calendar covers {first}..{last}; the fixture session "
            f"({after}) is outside it, so a non-veto here means 'nothing scheduled', "
            "not 'verified clear'"
        )
    day = _weekday(after + dt.timedelta(days=1))
    for _ in range(span):
        if day.weekday() < 5 and not calendar.is_event_day(day, veto_kinds=veto_kinds):
            return day, warnings
        day = _weekday(day + dt.timedelta(days=1))
    raise RehearsalError("no non-vetoed weekday in 10 sessions after the signal bar")


# ---------------------------------------------------------------------------
# the stages
# ---------------------------------------------------------------------------

#: The rehearsal's own reference date: the last deployed flow session. The signal bar
#: is the Friday before the following Monday, so the fixture calendar lines up with
#: the real journal's timeline without touching it.
SIGNAL_LAST_BAR = dt.date(2026, 10, 16)


def _pending_gates(rules_yaml: dict) -> list[dict[str, str]]:
    """Every ``calibration_pending: true`` leaf in the checklist, with its path."""
    out: list[dict[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("calibration_pending") is True:
                out.append({"path": path, "value": str(node.get("value"))})
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(rules_yaml.get("checklist", {}), "checklist")
    return out


def stage_rules(r: Rehearsal, st: Stage) -> None:
    data = yaml.safe_load(DEFAULT_RULES_PATH.read_bytes())
    r.shipped_rules = load_rules_text(yaml.safe_dump(data))
    pending = _pending_gates(data)
    st.detail = {
        "strategy_version": r.shipped_rules.strategy_version,
        "status": r.shipped_rules.strategy.status,
        "symbol": r.shipped_rules.strategy.symbol,
        "pending_gates": pending,
        "veto_kinds": r.shipped_rules.checklist.t5_options_chain.event_calendar.veto_kinds,
        "sha256": _sha256(Path(DEFAULT_RULES_PATH)),
    }
    # Stashed for the integrity stage: the shot branch below runs against a SIMULATED
    # freeze, and the one thing that would invalidate the whole report is that file being
    # edited to make a fixture pass.
    r.notes["rules"] = {"sha256": st.detail["sha256"], "path": str(DEFAULT_RULES_PATH)}
    st.say(f"shipped rulebook {st.detail['strategy_version']} ({st.detail['status']})")
    if pending:
        st.say(f"{len(pending)} uncalibrated gate(s) still blocking by construction:")
        for p in pending:
            st.say(f"  - {p['path']} (value={p['value']})")
        st.find(
            "The shipped rulebook has unfrozen gates, so with it no cell can fire. "
            "Every entry/exit branch below runs against a SIMULATED FREEZE, not "
            "against the pre-registered rules."
        )
    else:
        st.find(
            "shipped rulebook is fully calibrated — the simulated freeze equals "
            "the shipped rules"
        )


def stage_calibration(r: Rehearsal, st: Stage) -> None:
    src = r.calibration_db or DEPLOYED_JOURNAL
    if not Path(src).exists():
        raise RehearsalError(f"no calibration source: {src} (pass --calibration-db)")
    copy = r.workdir / "calibration-source.db"
    shutil.copyfile(src, copy)  # read-only source, writable copy
    conn = sqlite3.connect(str(copy))
    conn.row_factory = sqlite3.Row
    try:
        from journal.store import list_flow_baseline

        stored = list_flow_baseline(conn)
        rows = conn.execute("SELECT COUNT(*) FROM flow_baseline").fetchone()[0]
        result = flow_calibration(stored)
    finally:
        conn.close()
    r.proposal = result
    st.detail = result.to_dict()
    st.detail["source_db"] = str(src)
    st.detail["source_rows"] = rows
    st.detail["source_copied_to"] = str(copy)
    if not result.sufficient:
        st.say(f"NOT ENOUGH DATA: {result.summary_line()}")
        st.find(
            "T6 has not been calibrated. The multiplier stays null and arm B's T6 is "
            "PENDING every session until the review freezes it."
        )
        return
    st.say(
        f"proposed N (pooled) = {result.n:.3f}   call = {result.n_call:.3f}   "
        f"put = {result.n_put:.3f}"
    )
    st.say(
        f"sessions: {result.sessions_qualified} qualifying of {result.sessions_considered} "
        f"(need {result.sessions_needed}); {len(result.excluded)} side-samples excluded"
    )
    if result.excluded:
        by_reason: dict[str, int] = {}
        for e in result.excluded:
            by_reason[e.reason] = by_reason.get(e.reason, 0) + 1
        st.say(f"exclusions by reason: {by_reason}")
    st.say(
        f"distribution: median {result.median_ratio:.3f}x, min {result.min_ratio:.3f}x, "
        f"max {result.max_ratio:.3f}x; raw volume P90 {result.raw_total_volume_p90:,.0f} contracts"
    )
    for note in result.notes:
        st.say(f"NOTE: {note}")
        st.find(note)
    for s in result.samples_above(5.0):
        st.say(f"  outlier: {s.date} {s.side} {s.ratio:.2f}x baseline")
    st.find(
        f"PROPOSAL ONLY (not frozen): N = {result.n:.3f} from {len(result.ratios)} "
        f"dimensionless ratios at threshold {result.threshold_pct:.1f}% deep OTM."
    )


def stage_simulated_freeze(r: Rehearsal, st: Stage) -> None:
    if not r.proposal.sufficient:
        raise RehearsalError("cannot simulate a freeze on an uncalibrated proposal")
    path = r.workdir / "rules.rehearsal.json"
    text = rehearsal_rules_text(n=r.proposal.n, threshold_pct=r.proposal.threshold_pct)
    path.write_text(text)
    r.rules = load_rules_text(text)
    # Load it back through the loader: the simulated file must be a valid rulebook,
    # not a lucky dict.
    reloaded = load_rules(path)
    st.detail = {
        "fixture_rulebook": str(path),
        "shipped_rulebook_untouched": True,
        "n_used": r.proposal.n,
        "deep_otm_pct_used": r.proposal.threshold_pct,
        "arm_c_iv_rank_max": reloaded.checklist.t5_options_chain.arm_c.iv_rank_max,
        "strategy_version": reloaded.strategy_version,
        "reloaded_from_file_ok": reloaded.strategy_version == r.rules.strategy_version,
    }
    st.say(f"simulated freeze written to {path}")
    st.say(
        f"  t6 multiplier {r.proposal.n:.3f}, "
        f"deep-OTM ±{r.proposal.threshold_pct:.1f}%, arm C iv_rank_max 50"
    )
    st.find(
        "SIMULATED FREEZE: the shot branch below proves the pipeline's mechanics with "
        "T6 filled in. It is not evidence that the calibration was reviewed — that is "
        "the lead's call and it is still open."
    )


# ---------------------------------------------------------------------------
# the world the rehearsal runs in
# ---------------------------------------------------------------------------


def _prepare(r: Rehearsal, st: Stage) -> None:
    """Build the fixture world: journal, calendar, dates, chains, IV store.

    The world is disposable by design: the journal and IV stores are *deleted* first so a
    rerun into the same workdir starts from an empty ledger. Without that the rerun hits
    the append-only ``flow_baseline`` unique constraint, and even if it did not, the row
    counts in the integrity stage would depend on how many times the harness had been run
    before — which is exactly the kind of drift a rehearsal is supposed to rule out.
    """
    for stale in (r.conn_out, r.iv_path, r.workdir / "shadow.db"):
        if stale.exists():
            stale.unlink()
    veto_kinds = r.shipped_rules.checklist.t5_options_chain.event_calendar.veto_kinds
    calendar = EventCalendar.load_dir()
    start = _start_for_last(SIGNAL_LAST_BAR, WARMUP_BARS)
    signal = build_series(start_date=start, **FIRING_PARAMS)
    quiet = build_series(start_date=start, **NOSHOT_PARAMS)
    if signal.bars[-1].t.date() != SIGNAL_LAST_BAR:
        raise RehearsalError("series did not land on the intended signal bar")

    shot_day, warnings = _first_clean_session(signal.bars[-1].t.date(), calendar, veto_kinds)
    # A second, deliberately boring session for the NO-SHOT branch: the same signal bar
    # (so the calendar/date logic is identical) is not needed — what makes a plan a
    # no-shot is the market, so this one gets its own quiet series and its own day.
    quiet_day, quiet_warnings = _first_clean_session(quiet.bars[-1].t.date(), calendar, veto_kinds)
    if quiet_day == shot_day:
        quiet_day, quiet_warnings = _next_clean_session(quiet_day, calendar, veto_kinds), []

    spot = signal.bars[-1].c
    chain_0dte = zero_dte_chain(day=shot_day, spot=spot)
    chain_c = arm_c_chain(day=shot_day, spot=spot)
    combined = OptionChain(
        underlying="SPY",
        feed="indicative",
        contracts=list(chain_0dte.contracts) + list(chain_c.contracts),
        fetched_at=dt.datetime.combine(SIGNAL_LAST_BAR, dt.time(19, 45), tzinfo=UTC),
    )

    iv_store, iv_info = seed_iv_store(r.iv_path, day=shot_day, chain=combined, spot=spot)

    r.journal = init_db(r.conn_out)
    set_meta(r.conn, "strategy_version", r.rules.strategy_version)
    # One confirmed flow session, written through the journal's own append path, so the
    # T6 gate both branches read is produced by the real reader against a real row.
    # Asymmetric on purpose: the call side passes and the put side fails, which is what
    # "t6_flow.side = trade_direction, one multiplier, two populations" means in
    # practice — and it is visible in the report rather than assumed.
    r.flow_row_day = _weekday(shot_day - dt.timedelta(days=3))
    r.world = {  # type: ignore[attr-defined]
        "calendar": calendar,
        "signal": signal,
        "quiet": quiet,
        "shot_day": shot_day,
        "noshot_day": quiet_day,
        "spot": spot,
        "chain": combined,
        "iv_store": iv_store,
        "iv_key": iv_info["tenor_keys"],
        "iv_rank": iv_info["rank_max"],
    }
    _seed_flow_row(r, day=r.flow_row_day, side="call", ratio=3.0)
    # NOTE: keep the earlier "rules" entry — the integrity stage reads the rulebook's
    # sha256 from here to prove the shipped file was not edited to make a fixture pass.
    notes_rules = r.notes.pop("rules", {})
    r.notes = {
        "calendar": {
            "dir": str(calendar.source_dir),
            "coverage": [str(x) if x else None for x in calendar.coverage()],
            "events": len(calendar),
            "unverified_events": len(calendar.unverified()),
        },
        "dates": {
            "signal_bar": SIGNAL_LAST_BAR.isoformat(),
            "shot_session": shot_day.isoformat(),
            "noshot_session": quiet_day.isoformat(),
            "warnings": warnings + quiet_warnings,
        },
        "iv": {"fixture_store": iv_info, "deployed_store": deployed_ivrank_readiness()},
        "spot": round(spot, 4),
        "flow": {
            "confirmed_session": r.flow_row_day.isoformat(),
            "call_ratio": 3.0,
            "put_ratio": 1.0,
            "multiplier": r.proposal.n,
        },
        **({"rules": notes_rules} if notes_rules else {}),
    }
    st.detail = dict(r.notes)
    st.say(
        f"signal bar {SIGNAL_LAST_BAR}, shot session {shot_day} (${spot:.2f}), "
        f"no-shot session {quiet_day}"
    )
    st.say(
        f"IV fixture: {iv_info['observations_written']} observations across "
        f"{iv_info['tenor_keys']} tenors, ranks "
        f"{iv_info['rank_min']:.1f}..{iv_info['rank_max']:.1f}"
    )
    deployed = r.notes["iv"]["deployed_store"]
    if deployed.get("present"):
        st.say(
            f"DEPLOYED IV store: {deployed['observations']} rows / "
            f"{deployed['tenor_keys']} tenors, deepest tenor "
            f"{deployed['deepest_tenor_observations']} of "
            f"{deployed['min_observations']} needed "
            f"({deployed['first_seen']}..{deployed['last_seen']})"
        )
        if not deployed["tenor_reaching_min"]:
            st.find(
                f"DEPLOYMENT BLOCKER: the deployed IV store cannot score T5. Its deepest "
                f"tenor has {deployed['deepest_tenor_observations']} observations against "
                f"IvRankStore.MIN_OBSERVATIONS={deployed['min_observations']}, so every T5 "
                "lookup returns undefined and arm B cells stay PENDING. Wiring the store "
                "into the CLI is necessary but not sufficient: this store needs to warm up."
            )
    for w in warnings + quiet_warnings:
        st.find(w)


def _next_clean_session(day: dt.date, calendar: EventCalendar, veto_kinds: list[str]) -> dt.date:
    nxt = _weekday(day + dt.timedelta(days=1))
    for _ in range(10):
        if not calendar.is_event_day(nxt, veto_kinds=veto_kinds):
            return nxt
        nxt = _weekday(nxt + dt.timedelta(days=1))
    raise RehearsalError("no clean session after " + day.isoformat())


def _world(r: Rehearsal) -> Any:
    return r.world


# ---------------------------------------------------------------------------
# stage: the two plans
# ---------------------------------------------------------------------------


def _plan_for(
    r: Rehearsal,
    *,
    series: BarSeries,
    day: dt.date,
    chain: OptionChain,
    iv_store: Any,
    gate: FlowGate,
) -> Any:
    provider = StaticMarketData(series=series, chain=chain)
    return build_hunt_plan(
        data=provider,
        rules=r.rules,
        day=day,
        calendar=_world(r)["calendar"],
        iv_store=iv_store,
        spot=series.bars[-1].c,
        flow_gate=gate,
        generated_ts=f"{day.isoformat()}T12:00:00Z",
    )


def _gate(r: Rehearsal, side: str) -> FlowGate:
    """The fixture flow gate, built through the REAL reader against a fixture journal
    row — not hand-assembled — so the reader itself is on the rehearsal path."""
    read = flow_gate_from_journal(
        r.conn,
        as_of=_world(r)["shot_day"],
        side=side,
        multiplier=r.proposal.n,
    )
    return read.gate


def _seed_flow_row(r: Rehearsal, *, day: dt.date, side: str, ratio: float) -> None:
    from journal.store import FlowBaselineRow, append_flow_baseline

    mean = 1_000.0
    call = ratio * mean if side == "call" else mean
    put = ratio * mean if side == "put" else mean
    append_flow_baseline(
        r.conn,
        FlowBaselineRow(
            date=day.isoformat(),
            underlying="SPY",
            feed="indicative",
            is_delayed=True,
            session_spot=_world(r)["spot"],
            deep_otm_threshold_pct=r.proposal.threshold_pct,
            deep_otm_call_volume=call,
            deep_otm_put_volume=put,
            deep_otm_total_volume=call + put,
            call_volume_by_distance={"4": call},
            put_volume_by_distance={"4": put},
            baseline_lookback_days=20,
            baseline_days=20,
            baseline_call_mean=mean,
            baseline_put_mean=mean,
            ratio_call=call / mean,
            ratio_put=put / mean,
            strategy_version=r.rules.strategy_version,
            ts=f"{day.isoformat()}T21:00:00Z",
        ),
    )


def stage_plan_noshot(r: Rehearsal, st: Stage) -> None:
    """The near-miss session: the setup was there, one condition said no."""
    w = _world(r)
    day: dt.date = w["noshot_day"]
    gate = _gate(r, "call")
    plan = _plan_for(
        r,
        series=w["quiet"],
        day=day,
        chain=w["chain"],
        iv_store=w["iv_store"],
        gate=gate,
    )
    ids = write_hunt_plan(r.conn, plan)
    r.plans["noshot"] = {"plan": plan, "ids": ids, "taken": []}
    cells = [
        {
            "arm": c.arm,
            "direction": c.direction,
            "kind": c.decision_kind,
            "conviction": c.conviction,
            "failed": list(c.failed()),
            "pending": list(c.pending()),
        }
        for c in plan.arms
    ]
    sightings = [c for c in plan.arms if len(c.failed()) == 1]
    st.detail = {
        "day": day.isoformat(),
        "cells": cells,
        "firing_cells": len([c for c in cells if c["kind"] == "FIRE"]),
        "near_miss_cells": [f"{c['arm']}/{c['direction']}" for c in cells if len(c["failed"]) == 1],
        "decision_rows": len(ids),
        "summary": summarise(plan),
    }
    st.say(f"no-shot plan for {day}: {len(sightings)} near-miss cell(s), 0 firing")
    for c in cells:
        st.say(
            f"  {c['arm']}/{c['direction']}: {c['kind']} conviction={c['conviction']} "
            f"failed={','.join(c['failed']) or '-'} pending={','.join(c['pending']) or '-'}"
        )
    if [c for c in cells if c["kind"] == "FIRE"]:
        st.ok = False
        st.error = "the near-miss fixture fired — the fixture is wrong, not the checklist"
    if not sightings:
        st.find(
            "NO-SHOT PATH PARTIAL: no cell was within the marginal limit of firing, so "
            "the ledger legitimately stays empty. The plan/journal path is exercised."
        )
        return
    st.find(
        "NO-SHOT PATH EXERCISED: "
        + "; ".join(
            f"{c['arm']}/{c['direction']} stopped by {', '.join(c['failed'])}"
            for c in cells
            if len(c["failed"]) == 1
        )
        + " — one condition, inside the marginal limit, which is exactly what the EOD "
        "pass turns into a counterfactual row."
    )


def stage_plan_shot(r: Rehearsal, st: Stage) -> None:
    """The entry-capable session.

    This plan is expected to be NO_TRADE on every cell — that is what a washout signal
    bar produces. What it must carry is at least one **actionable trigger**, because
    the watch loop only ever considers a cell whose plan has a level to reach, and
    that is the seam this rehearsal exists to prove (see ``watch/entry``).
    """
    w = _world(r)
    day: dt.date = w["shot_day"]
    plan = _plan_for(
        r,
        series=w["signal"],
        day=day,
        chain=w["chain"],
        iv_store=w["iv_store"],
        gate=_gate(r, "call"),
    )
    ids = write_hunt_plan(r.conn, plan)
    r.plans["shot"] = {"plan": plan, "ids": ids, "taken": []}
    cells = [
        {
            "arm": c.arm,
            "direction": c.direction,
            "kind": c.decision_kind,
            "conviction": c.conviction,
            "fire": c.fire,
            "failed": list(c.failed()),
            "triggers": [f"{t.condition} {t.sense} {t.level:.2f}" for t in c.triggers if
                t.actionable],
        }
        for c in plan.arms
    ]
    entry_cells = [c for c in plan.arms if any(t.actionable for t in c.triggers)]
    levels = {
        f"{c.arm}/{c.direction}": [
            {"condition": t.condition, "sense": t.sense, "level": t.level}
            for t in c.triggers
            if t.actionable
        ]
        for c in entry_cells
    }
    st.detail = {
        "day": day.isoformat(),
        "cells": cells,
        "firing_cells": [f"{c['arm']}/{c['direction']}" for c in cells if c["fire"]],
        "entry_capable_cells": list(levels),
        "trigger_levels": levels,
        "decision_rows": len(ids),
        "summary": summarise(plan),
    }
    st.say(f"shot plan for {day}: {len(entry_cells)} of {len(cells)} cells carry a watch level")
    for c in cells:
        st.say(
            f"  {c['arm']}/{c['direction']}: {c['kind']} conviction={c['conviction']} "
            f"failed={','.join(c['failed']) or '-'} "
            f"triggers=[{'; '.join(c['triggers']) or 'none'}]"
        )
    if not entry_cells:
        st.ok = False
        st.error = "no cell carries an actionable trigger — the entry path is unreachable"
        return
    st.find(
        f"ENTRY-CAPABLE PLAN: {', '.join(levels)} carry a price level the watch loop can "
        f"watch ({'; '.join(sorted({x['condition'] for v in levels.values() for x in v}))}). "
        "The plan itself is NO_TRADE on the washout close — correct: the setup is not "
        "complete until price reclaims the level, which is what T3b's live "
        "re-verification is for."
    )
    st.find(
        "TRIGGER-PATH FINDING: a plan whose cells PASS every condition carries NO "
        "actionable trigger (compute_triggers only emits levels for non-PASS blocking "
        "conditions), so such a cell can never be entered by the watch loop. A fully "
        "green pre-market plan is therefore a plan with nothing to watch. The loop's "
        "own tests reach the entry path only with hand-built cells, so the interaction "
        "between the plan and the entry trigger is untested in production shape. "
        "Whether a green cell should arm the loop (e.g. on the entry window alone, or "
        "against a fixed trigger) is a MECHANICS question for the lead — not fixed here."
    )


# ---------------------------------------------------------------------------
# stage: the watch loop
# ---------------------------------------------------------------------------


def _tick_times(day: dt.date, count: int = 3, start: str = "09:35") -> list[dt.datetime]:
    hour, minute = (int(x) for x in start.split(":"))
    out = []
    for i in range(count):
        m = minute + 5 * i
        h = hour + m // 60
        out.append(dt.datetime.combine(day, dt.time(h, m % 60), tzinfo=ET).astimezone(UTC))
    return out


def _watch_world(
    r: Rehearsal,
    *,
    key: str,
    spot: float | None = None,
    fetched_at: dt.datetime | None = None,
    at: dt.datetime | None = None,
) -> tuple[StaticWatchData, WatchState, dt.date]:
    """The intraday world for one branch.

    For the shot branch the daily series carries the session's own reclaim bar, because
    that is what the loop re-verifies against: the checklist is re-run on the LIVE
    series, so a loop fed yesterday's bars would re-verify a yesterday setup and could
    never agree with a session that actually reclaimed the level.

    ``fetched_at`` is stamped by the caller whenever a tick happens. Left to
    ``StaticWatchData`` it defaults to wall-clock ``now()``, which for a fixture dated in
    the future (or the past) produces an age of millions of seconds and a stale-veto on
    every tick — a fixture artefact that looks exactly like a real data-pipeline fault.
    """
    entry = r.plans[key]
    plan = entry["plan"]
    w = _world(r)
    if key == "shot":
        live = _session_series(w["signal"], plan.day, final_return=RECLAIM_RETURN)
        series: BarSeries = live
        default_spot = live.bars[-1].c
    else:
        series = w["quiet"]
        default_spot = series.bars[-1].c
    provider = StaticWatchData(
        spot=spot if spot is not None else default_spot,
        daily=series,
        chain=w["chain"],
        fetched_at=fetched_at if fetched_at is not None else (at - dt.timedelta(seconds=5) if at
            else None),
    )
    state = WatchState.initial(day=plan.day, plan=plan, rules=r.rules)
    return provider, state, plan.day


def stage_watch_quiet(r: Rehearsal, st: Stage) -> None:
    """The no-shot plan intraday: ticks run, nothing fires, and the loop says so.

    Two shapes are driven: individual ``run_once`` ticks (the unit the cron calls) and
    the ``run_loop`` driver that wraps them, with its clock, sleep and tick hook
    injected. The driver is what the intraday cron actually runs, so a rehearsal that
    only calls ``run_once`` would leave the untested part untested.
    """
    times = _tick_times(r.plans["noshot"]["plan"].day, 3)
    sink = MemoryJournalSink()
    results = []
    state = None
    for now in times:
        # A provider per tick: ``fetched_at`` is the read time, and reusing one provider
        # across ticks would age the snapshot past the entry budget by tick 2 — a
        # fixture artefact, not the staleness the stale stage exists to prove.
        provider, fresh_state, _day = _watch_world(r, key="noshot", at=now)
        state = fresh_state if state is None else state
        res = run_once(
            provider=provider,
            rules=r.rules,
            state=state,
            now=now,
            router=r.router,
            journal=sink,
            calendar=_world(r)["calendar"],
            iv_rank=_world(r)["iv_rank"],
            flow_gate=_gate(r, "call"),
        )
        state = res.next_state or state
        results.append(res)
    routed = [a for res in results for a in res.actions]
    triggered = [a for a in routed if a.kind == "TRADE"]

    # The driver the intraday cron actually runs, with zero real time.
    loop_times = _tick_times(r.plans["noshot"]["plan"].day, 3, start="10:40")
    clock_state = {"i": 0}
    seen: list[str] = []

    def _clock() -> dt.datetime:
        moment = loop_times[min(clock_state["i"], len(loop_times) - 1)]
        clock_state["i"] += 1
        return moment

    def _fresh_provider() -> StaticWatchData:
        # ``run_loop`` re-reads the provider every tick; this hands it a snapshot
        # stamped at the current tick so the freshness budget behaves as it would live.
        return _watch_world(r, key="noshot", at=_clock())[0]

    def _tick_hook(result: Any) -> None:
        seen.append(f"{result.now.isoformat()} actions={len(result.actions)}")

    loop_provider = _fresh_provider()
    loop_results = run_loop(
        provider=loop_provider,
        rules=r.rules,
        state=WatchState.initial(day=r.plans["noshot"]["plan"].day, plan=r.plans["noshot"]["plan"],
            rules=r.rules),
        router=DryRunRouter(),
        journal=MemoryJournalSink(),
        max_ticks=3,
        poll_seconds=300.0,
        sleep=lambda _s: None,
        clock=_clock,
        tick_hook=_tick_hook,
        calendar=_world(r)["calendar"],
        iv_rank=_world(r)["iv_rank"],
        flow_gate=_gate(r, "call"),
    )
    loop_trades = [a for res in loop_results for a in res.actions if a.kind == "TRADE"]

    st.detail = {
        "ticks": len(results),
        "clock": [t.isoformat() for t in times],
        "state": state.to_dict(),
        "routed_actions": len(routed),
        "entries": len(triggered),
        "notes": list(state.last_notes if state else ()),
        "run_loop_ticks": len(loop_results),
        "run_loop_trades": len(loop_trades),
        "run_loop_hook": seen,
    }
    st.say(
        f"{len(results)} quiet ticks on the no-shot plan; routed actions: {len(routed)}"
    )
    for note in (state.last_notes if state else ()):
        st.say(f"  note: {note}")
    st.say(
        f"run_loop driver: {len(loop_results)} tick(s), {len(loop_trades)} entries "
        f"(hook saw {len(seen)})"
    )
    if triggered or loop_trades:
        st.ok = False
        st.error = "a no-shot plan produced an entry — the watch loop ignored the plan"
    if not loop_results:
        st.ok = False
        st.error = "run_loop returned no ticks — the intraday driver path is unproven"
    st.find(
        "QUIET TICK PATH EXERCISED: both the single tick (``run_once``) and the polling "
        "driver the intraday cron runs (``run_loop``, clock/sleep/hook injected, zero real "
        "time) ticked on schedule and reported NO_TRADE rather than skipping themselves."
    )


def _shot_cell(r: Rehearsal) -> Any:
    """The B/call cell that carries the watch level — the one the entry path uses."""
    plan = r.plans["shot"]["plan"]
    return next(
        (
            c
            for c in plan.arms
            if c.arm == "B" and c.direction == "call" and any(t.actionable for t in c.triggers)
        ),
        None,
    )


def _entry_contract(provider: StaticWatchData, cell: Any) -> Any:
    """The contract arm B's strike rule would buy, priced for the entry governor.

    The strike rule is "the first OTM strike beyond the setup-day-range projection", so
    the contract is read out of the plan's own criteria rather than picked — a fixture
    that chose a different contract would be testing a different strategy.
    """
    projection = cell.watch.strike_projection or {}
    level = projection.get("level")
    if level is None or provider.chain is None:
        return None
    calls = sorted(
        (c for c in provider.chain.contracts if c.right == "call"), key=lambda c: c.strike
    )
    return next((c for c in calls if c.strike >= level), None)


def stage_watch_stale(r: Rehearsal, st: Stage) -> None:
    """A trigger with a stale snapshot. Entries must be refused; exits must not be
    blocked by the same staleness (a blocked exit is a naked position)."""
    cell = _shot_cell(r)
    if cell is None:
        st.ok = False
        st.error = "no B/call cell with a watch level to test staleness against"
        return
    now = _tick_times(r.plans["shot"]["plan"].day, 1)[0]
    provider, state, _day = _watch_world(r, key="shot", fetched_at=now - dt.timedelta(minutes=45))
    # The reclaim has already happened, so the trigger WOULD fire on a fresh read.
    levels = [t.level for t in cell.triggers if t.actionable]
    fired = [t for t in cell.triggers if t.actionable and (t.level or 0) <= provider.spot]
    res = run_once(
        provider=provider,
        rules=r.rules,
        state=state,
        now=now,
        router=r.router,
        journal=MemoryJournalSink(),
        calendar=_world(r)["calendar"],
        iv_rank=_world(r)["iv_rank"],
        flow_gate=_gate(r, "call"),
    )
    trades = [a for a in res.actions if a.kind == "TRADE"]
    st.detail = {
        "cell": f"{cell.arm}/{cell.direction}",
        "spot": round(provider.spot, 4),
        "levels": levels,
        "levels_reached": [f"{t.condition} {t.level:.2f}" for t in fired],
        "fetched_at": provider.fetched_at.isoformat(),
        "snapshot_age_seconds": res.snapshot_age_seconds,
        "stale": res.stale,
        "entries_routed": len(trades),
        "vetoes": [v.to_dict() for v in res.vetoes],
        "notes": list(res.notes),
    }
    st.say(
        f"stale snapshot ({provider.fetched_at.isoformat()}, age "
        f"{res.snapshot_age_seconds:.0f}s) with {len(fired)} level(s) reached: "
        f"{len(trades)} entries routed, {len(res.vetoes)} veto"
    )
    for note in res.notes:
        st.say(f"  note: {note}")
    if not fired:
        st.ok = False
        st.error = "the stale stage never reached a trigger level — it proved nothing"
    if trades:
        st.ok = False
        st.error = "a stale snapshot still produced an entry"
    st.find(
        "STALE-SNAPSHOT VETO EXERCISED: the trigger was genuinely reached and the loop "
        "still refused the entry, journaling a stale_snapshot veto. Exits are evaluated "
        "before entries in the same tick, so this veto cannot strand an open position."
    )


def stage_watch_entry(r: Rehearsal, st: Stage) -> None:
    """The real thing: the plan's watch level is reached, the loop re-verifies the
    whole checklist against the LIVE series, and the entry governor decides."""
    cell = _shot_cell(r)
    if cell is None:
        st.ok = False
        st.error = "no B/call cell with a watch level"
        return
    now = _tick_times(r.plans["shot"]["plan"].day, 1, start="10:20")[0]
    provider, state, _day = _watch_world(r, key="shot", at=now)
    contract = _entry_contract(provider, cell)
    if contract is None:
        st.ok = False
        st.error = "the fixture chain has no contract beyond the strike projection"
        return
    provider.option_prices = {contract.symbol: 1.40}
    routed_before = len(r.router.actions)
    res = run_once(
        provider=provider,
        rules=r.rules,
        state=state,
        now=now,
        router=r.router,
        journal=MemoryJournalSink(),
        calendar=_world(r)["calendar"],
        iv_rank=_world(r)["iv_rank"],
        flow_gate=_gate(r, "call"),
    )
    trades = [a for a in res.actions if a.kind == "TRADE"]
    vetoes = [a for a in res.actions if a.kind == "VETO"]
    r.plans["shot"]["state_after_entry"] = res.next_state
    r.plans["shot"]["contract"] = contract.symbol
    st.detail = {
        "cell": f"{cell.arm}/{cell.direction}",
        "contract": contract.symbol,
        "spot": round(provider.spot, 4),
        "now": now.isoformat(),
        "levels_reached": [
            f"{t.condition} {t.sense} {t.level:.2f}" for t in cell.triggers if t.actionable
        ],
        "trades": [
            {"kind": a.kind, "arm": a.arm, "symbol": a.symbol, "reason": a.reason} for a in trades
        ],
        "vetoes": [{"kind": a.kind, "reason": a.reason} for a in vetoes],
        "reverified": {
            f"{k[0]}/{k[1]}": {cid: c.status for cid, c in v.conditions.items()}
            for k, v in res.reverified.items()
        },
        "state": res.next_state.to_dict() if res.next_state else None,
    }
    st.say(
        f"entry tick {now.isoformat()} at {provider.spot:.2f} ({contract.symbol}): "
        f"{len(trades)} TRADE, {len(vetoes)} VETO, {len(res.reverified)} cell(s) re-verified"
    )
    for a in trades:
        st.say(f"  TRADE {a.arm} {a.symbol}: {a.reason.splitlines()[0] if a.reason else ''}")
    for a in vetoes:
        st.say(f"  VETO {a.arm}: {a.reason.splitlines()[0] if a.reason else ''}")
    for key, conds in st.detail["reverified"].items():
        st.say(f"  re-verified {key}: {conds}")
    if not trades:
        st.ok = False
        st.error = "the entry tick did not produce a trade"
        return
    r.plans["shot"]["taken"] = [(cell.arm, cell.direction)]
    st.find(
        f"ENTRY PATH EXERCISED: the reclaim took {contract.symbol} through the loop — "
        "trigger reached, every condition re-verified on the live series, entry "
        "governors consulted, dry-run router recorded it."
    )
    if len(r.router.actions) <= routed_before:
        st.ok = False
        st.error = "the entry tick routed nothing — the trade never reached the router"
    elif "0 orders sent" not in r.router.describe():
        st.ok = False
        st.error = f"DryRunRouter claims to have sent orders: {r.router.describe()}"
    else:
        st.find("DRY RUN CONFIRMED: the router recorded the action and sent nothing.")


# ---------------------------------------------------------------------------
# stage: exits and the roll ladder
# ---------------------------------------------------------------------------


def stage_exits_and_roll(r: Rehearsal, st: Stage) -> None:
    """Arm B's exit ladder on the position the entry tick opened, and arm C's roll.

    Exercised through the loop, not around it, so the state the ladder sees is the
    state the loop would really have: same ``ArmState``, same clock, same router.
    """
    entry = r.plans["shot"]
    if not entry.get("state_after_entry"):
        st.ok = False
        st.error = "no entry state to exit from"
        return
    state: ArmState = entry["state_after_entry"]
    if not state.arms.get("B", state).position:
        # run_once carries the position on the arm state it returned
        pass
    pos_state = state.arms["B"] if isinstance(getattr(state, "arms", None), dict) else state
    if not getattr(pos_state, "position", None):
        st.ok = False
        st.error = "arm B holds no position after the entry tick"
        return
    contract_symbol = pos_state.position.symbol
    entry_price = pos_state.position.entry_price

    day = entry["plan"].day
    outcomes: list[dict[str, Any]] = []
    router_before = len(r.router.actions)
    for label, when, price in (
        ("profit take (+100%)", _tick_times(day, 1, "10:05")[0], entry_price * 2.0),
        ("hard close (15:30)", dt.datetime.combine(day, dt.time(15, 30), tzinfo=ET).
            astimezone(UTC), entry_price * 1.1),
    ):
        provider, wstate, _ = _watch_world(r, key="shot", spot=_world(r)["spot"])
        provider.spot = _world(r)["spot"]
        provider.option_prices = {contract_symbol: price}
        res = run_once(
            provider=provider,
            rules=r.rules,
            state=entry["state_after_entry"],
            now=when,
            router=r.router,
            journal=MemoryJournalSink(),
            calendar=_world(r)["calendar"],
            iv_rank=_world(r)["iv_rank"],
            flow_gate=_gate(r, "call"),
        )
        stops = [a for a in res.actions if a.kind == "STOP"]
        outcomes.append(
            {
                "scenario": label,
                "now": when.isoformat(),
                "option_price": price,
                "gain_pct": round(pos_state.position.gain_pct(price), 4),
                "stops": [{"arm": a.arm, "reason": a.reason, "legs": len(a.legs)} for a in stops],
            }
        )
        if not stops:
            st.find(f"arm B produced NO exit at {label} — the ladder did not fire")
        _ = wstate

    # arm C's roll: the ladder is exercised with a replacement leg, because the watch
    # loop never supplies one (see findings).
    manager = PositionManager(r.rules)
    c_position = ManagedPosition(
        arm="C",
        symbol="SPY260216C00475000",
        qty=1,
        entry_price=31.20,
        entry_ts=_tick_times(day, 1, "09:45")[0],
        right="call",
        expiry="20260216",
        strike=475.0,
        delta=0.60,
    )
    c_state = ArmState(bankroll_usd=r.rules.arms.C.bankroll_usd, position=c_position).with_entry(
        _tick_times(day, 1, "09:45")[0]
    )
    roll_eval = manager.evaluate_exits(
        "C",
        now=_tick_times(day, 1, "11:00")[0],
        state=c_state,
        price=1.0,
        delta=0.60,  # below the 0.70 trigger
        dte=40,
        replacement=OrderLeg("SPY260120C00600000", 1, "buy"),
    )
    roll_actions = [
        {"kind": a.kind, "legs": [(lg.symbol, lg.side, lg.qty) for lg in a.legs],
            "reason": a.reason}
        for a in roll_eval.actions
    ]
    bare_eval = manager.evaluate_exits(
        "C", now=_tick_times(day, 1, "11:00")[0], state=c_state, price=1.0, delta=0.60, dte=40
    )
    st.detail = {
        "position": pos_state.position.to_dict(),
        "exits": outcomes,
        "arm_c_position": c_position.to_dict(),
        "arm_c_roll_with_replacement": roll_actions,
        "arm_c_roll_without_replacement": [
            {"kind": a.kind, "reason": a.reason} for a in bare_eval.actions
        ],
        "routed_during_stage": len(r.router.actions) - router_before,
    }
    for o in outcomes:
        st.say(
            f"{o['scenario']}: {o['gain_pct']:+.1%} -> "
            + (o["stops"][0]["reason"].splitlines()[0] if o["stops"] else "NO EXIT")
        )
    st.say(
        f"arm C roll (delta 0.60, dte 40) with a replacement: "
        f"{[a['kind'] for a in roll_actions] or 'no action'}"
    )
    st.say(
        f"arm C roll without a replacement leg: {[a['kind'] for a in
            st.detail['arm_c_roll_without_replacement']] or 'no action'}"
    )
    if not any(o["stops"] for o in outcomes):
        st.ok = False
        st.error = "neither arm B exit fired"
    if not any(a["kind"] == "ROLL" for a in roll_actions):
        st.ok = False
        st.error = "arm C's roll ladder produced no ROLL even with a replacement leg"
    st.find(
        "EXIT PATHS EXERCISED: arm B's hard close outranked the profit take at 15:30, "
        "and the profit take fired at +100% earlier in the day — the documented "
        "precedence, verified rather than asserted."
    )
    st.find(
        "ROLL GAP (design-level, operator question): arm C's roll trigger fires, but the "
        "watch loop calls evaluate_exits without a replacement leg, so the ladder "
        "refuses to roll and reports a VETO-shaped note instead. An arm C position past "
        "its roll trigger therefore has no executor path. Choosing the replacement "
        "contract intraday is a trading mechanic, so this is documented, not fixed."
    )


# ---------------------------------------------------------------------------
# stage: EOD + soak
# ---------------------------------------------------------------------------


def _session_series(
    base: BarSeries, day: dt.date, *, final_return: float = 0.024, volume_mult: float = 1.8
) -> BarSeries:
    """``base`` plus the session's own bar — the bar that only exists once the day is
    under way. ``final_return`` is the reclaim when positive, the bleed when negative.
    """
    last = base.bars[-1]
    gap = (day - last.t.date()).days
    close = last.c * (1 + final_return)
    if final_return >= 0:
        # A reclaim closes back inside the band it broke: high a little above the
        # prior close, low a little under it.
        high, low = max(last.c, close) * 1.001, min(last.c, close) * 0.997
    else:
        high, low = max(last.c, close) * 1.002, min(last.c, close) * 0.990
    bar = Bar(
        t=dt.datetime.combine(day, dt.time(21, 0), tzinfo=UTC),
        o=last.c,
        h=round(high, 4),
        l=round(low, 4),
        c=round(close, 4),
        v=round(52_000_000.0 * volume_mult, 0),
        n=480_000,
        vw=round(close, 4),
    )
    if gap <= 0:
        return base
    return BarSeries(
        symbol=base.symbol,
        timeframe=base.timeframe,
        feed=base.feed,
        bars=[*base.bars, bar],
        next_page_token=base.next_page_token,
    )


def stage_eod(r: Rehearsal, st: Stage) -> None:
    """The EOD pass, offline, for BOTH sessions.

    ``cmd_eod`` in the CLI builds a real ``AlpacaClient`` at the top, so it cannot be run
    in a rehearsal at all; the steps it performs are replayed here against the fixture
    world, and the gaps it has are demonstrated rather than assumed (see findings).
    """
    from executor.main import _ema50_streak, _session_bar

    w = _world(r)
    sessions: dict[str, Any] = {}
    written_total = 0
    for key in ("noshot", "shot"):
        entry = r.plans[key]
        plan = entry["plan"]
        day: dt.date = plan.day
        series = (
            _session_series(w["signal"], day, final_return=RECLAIM_RETURN)
            if key == "shot"
            else _session_series(w["quiet"], day, final_return=-0.002, volume_mult=0.9)
        )
        session_bar = _session_bar(series, day)
        streak, ema = _ema50_streak(series, day)
        chain = zero_dte_chain(day=day, spot=session_bar.c if session_bar else w["spot"])
        taken = entry["taken"]
        drafts = build_noshots(
            plan,
            setup_bar=session_bar,
            chain=chain,
            rules=r.rules,
            taken=taken,
            ts=f"{day.isoformat()}T21:05:00Z",
            decision_ids=entry["ids"],
        )
        written = write_noshots(r.conn, drafts)
        written_total += len(written)
        rows = list_noshots(r.conn, date=day.isoformat())
        # The same call cmd_eod makes — no `taken`, no `decision_ids` — to show what the
        # CLI's EOD pass actually records for a session that DID trade. The comparison
        # runs on the near-miss session because that is the one where a cell both saw the
        # setup and could plausibly have been traded: for the shot session the cells are
        # too far from firing to be sightings at all, so the omission is invisible there.
        cli_shaped = build_noshots(plan, setup_bar=session_bar, chain=chain, rules=r.rules)
        if not taken and cli_shaped:
            traded_shape = build_noshots(
                plan,
                setup_bar=session_bar,
                chain=chain,
                rules=r.rules,
                taken={("B", "call")},
                ts=f"{day.isoformat()}T21:05:00Z",
                decision_ids=entry["ids"],
            )
        else:
            traded_shape = []
        close_id = append_decision(
            r.conn,
            DecisionEntry(
                ts=f"{day.isoformat()}T21:10:00Z",
                arm="B",
                kind="NO_TRADE",
                checklist_snapshot={
                    "enforcement": {"session_bar": session_bar.c if session_bar else None},
                    "indicators": {"ema50": ema, "streak_below_ema50": streak},
                },
                checklist_state={
                    "phase": "eod_close",
                    "positions": [],
                    "noshots": len(drafts),
                    "taken": list(taken),
                },
                reasoning=(
                    f"EOD close for {day}: streak {streak} close(s) below 50EMA, "
                    f"{len(written)} NO-SHOT row(s)"
                ),
                strategy_version=r.rules.strategy_version,
                symbol=r.rules.strategy.symbol,
            ),
        )
        set_meta(r.conn, "last_eod", {"date": day.isoformat(), "streak": streak})
        sessions[key] = {
            "day": day.isoformat(),
            "session_bar_close": session_bar.c if session_bar else None,
            "streak_below_ema50": streak,
            "ema50": ema,
            "taken": list(taken),
            "noshots_written": len(written),
            "noshots_in_journal": len(rows),
            "close_decision_id": close_id,
            "rows": [
                {
                    "id": x.id,
                    "arm": x.instrument_hypothesis.get("arm"),
                    "direction": x.instrument_hypothesis.get("direction"),
                    "instrument": x.instrument_hypothesis.get("instrument"),
                    "strike": x.instrument_hypothesis.get("strike"),
                    "counterfactual_entry_ref": x.counterfactual_entry_ref,
                    "failed": sorted((x.failed_conditions or {}).keys())
                    if isinstance(x.failed_conditions, dict)
                    else None,
                }
                for x in rows
            ],
            "cli_shaped_rows": len(cli_shaped),
            "cli_shaped_decision_refs": [d.decision_ref for d in cli_shaped],
            "rows_if_that_cell_had_traded": len(traded_shape),
        }
    st.detail = {"sessions": sessions, "noshots_written_total": written_total}
    for key, s_ in sessions.items():
        st.say(
            f"EOD {s_['day']} ({key}): streak={s_['streak_below_ema50']} "
            f"(50EMA {s_['ema50']:.2f}), {s_['noshots_written']} NO-SHOT row(s), "
            f"close decision #{s_['close_decision_id']}"
        )
        for row in s_["rows"]:
            st.say(
                f"  noshot #{row['id']} {row['arm']}/{row['direction']} "
                f"{row['instrument']} strike={row['strike']} "
                f"failed={','.join(row['failed'] or []) or '-'} "
                f"decision_ref={row['counterfactual_entry_ref']}"
            )
    if written_total == 0:
        st.ok = False
        st.error = "no NO-SHOT rows were written for either session — the ledger path is unproven"
    else:
        st.find(
            f"LEDGER PATH EXERCISED: {written_total} NO-SHOT row(s) written with the "
            "session's own bar, the setup-day strike recomputed from that bar (not the "
            "plan's provisional projection), and each row linked to the plan decision "
            "row it came from."
        )
    gap_session = next(
        (k for k, s_ in sessions.items() if s_["cli_shaped_rows"] and not s_["taken"]), None
    )
    if gap_session:
        gap = sessions[gap_session]
        st.find(
            f"EOD GAP (cmd_eod, unmodified): on the {gap_session} session the CLI's EOD "
            f"pass builds {gap['cli_shaped_rows']} NO-SHOT row(s) without passing "
            f"`taken`; if that same cell had traded, the correct ledger would hold "
            f"{gap['rows_if_that_cell_had_traded']} row(s). So a session that both saw "
            "the setup and traded still gets a row worded as a skip, with no way for a "
            "reader to tell the two apart. `cmd_eod` also omits `decision_ids`, so every "
            "row lands with counterfactual_entry_ref = NULL and the counterfactual loses "
            "its link to the decision that produced it."
        )
    st.find(
        "EOD PATH EXERCISED offline: the CLI's EOD command is not runnable in a "
        "rehearsal because it constructs a live AlpacaClient unconditionally — there is "
        "no --offline mode, so its steps were replayed against fixtures here."
    )


def stage_soak(r: Rehearsal, st: Stage) -> None:
    """One soak pass: the EOD job that fills T6's baseline and the IV store."""
    w = _world(r)
    day: dt.date = w["shot_day"]
    series = _session_series(w["signal"], day)
    chain = zero_dte_chain(day=day, spot=series.bars[-1].c)
    run = run_soak(
        source=FixtureSource(series, chain),
        conn=r.conn,
        iv_store=w["iv_store"],
        rules=r.rules,
        session=day,
        dry_run=False,
    )
    stored = r.conn.execute(
        "SELECT date, baseline_days, ratio_call, ratio_put FROM flow_baseline ORDER BY date"
    ).fetchall()
    iv_rows = w["iv_store"].conn.execute(
        "SELECT COUNT(*) FROM iv_observations WHERE as_of >= ?", (day.isoformat(),)
    ).fetchone()[0]
    st.detail = {
        "run": run.to_dict() if hasattr(run, "to_dict") else dataclasses.asdict(run),
        "flow_rows": [
            {"date": x[0], "baseline_days": x[1], "ratio_call": x[2], "ratio_put": x[3]} for x in
                stored
        ],
        "iv_rows_written_this_session": iv_rows,
    }
    st.say(f"soak {day}: status={getattr(run, 'status', '?')} — {getattr(run, 'summary', '')}")
    st.say(f"  flow_baseline rows now: {len(stored)}; IV rows for this session: {iv_rows}")
    if getattr(run, "status", "") in {"error", "failed"}:
        st.ok = False
        st.error = f"soak status {run.status}: {run.summary}"
    st.find(
        "SOAK PATH EXERCISED: the pass wrote a flow_baseline row for the session and IV "
        "observations, which is how T6's baseline and T5's warm-up accumulate at all. "
        "Both of those are historical processes — nothing here can shortcut them."
    )


# ---------------------------------------------------------------------------
# stage: analysis
# ---------------------------------------------------------------------------


def stage_analysis(r: Rehearsal, st: Stage) -> None:
    """Shadow-roll open + mark, the arm C comparison, and the weekly rollup."""
    w = _world(r)
    day: dt.date = w["shot_day"]
    series = _session_series(w["signal"], day)
    close = series.bars[-1].c

    # A TA-timed shadow entry: this is the whole point of the shadow — discretionary
    # entries, judged against arm C's fixed roll on the same underlying.
    leg = shadow_roll.open_leg(
        r.conn,
        opened_on=day,
        underlying_close=round(close * 0.99, 4),
        qty=2,
        strategy_version=r.rules.strategy_version,
        notes="rehearsal: TA-timed shadow entry",
        ts=f"{day.isoformat()}T21:15:00Z",
    )
    mark_day = day + dt.timedelta(days=1)
    mark = shadow_roll.mark_leg(
        r.conn,
        leg,
        date=mark_day,
        underlying_close=round(close * 1.04, 4),
        notes="rehearsal mark",
        ts=f"{mark_day.isoformat()}T21:15:00Z",
    )
    comparison = shadow_roll.score_shadow_vs_arm_c(r.conn, None)
    card = rollup.prediction_scorecard(r.conn)
    week_text = rollup.weekly_rollup_text(r.conn, day)
    pnl = rollup.arm_pnl(r.conn, day - dt.timedelta(days=6), day)
    counts = rollup.trade_counts(r.conn, day - dt.timedelta(days=6), day)
    hist = rollup.checklist_failure_histogram(r.conn, day - dt.timedelta(days=6), day)

    st.detail = {
        "leg_id": leg,
        "mark_id": mark,
        "comparison": comparison,
        "scorecard_statuses": {
            k: v.get("status") for k, v in card.items() if isinstance(v, dict) and "status" in v
        },
        "arm_pnl": pnl,
        "trade_counts": counts,
        "failure_histogram": hist,
        "weekly_rollup_chars": len(week_text),
        "weekly_rollup_head": week_text.splitlines()[:6],
    }
    st.say(
        f"shadow leg #{leg} opened at {close * 0.99:.2f}, "
        f"marked at {close * 1.04:.2f} (+{mark_day})"
    )
    st.say(
        "shadow vs arm C: "
        + json.dumps(
            {
                "ta_entries_beat_fixed_roll": comparison.get("ta_entries_beat_fixed_roll"),
                "arm_c_reason": (comparison.get("arm_c") or {}).get("reason"),
            }
        )
    )
    head_line = week_text.splitlines()[0] if week_text else "(empty)"
    st.say(f"weekly rollup: {len(week_text)} chars, head: {head_line}")
    for line in week_text.splitlines()[:8]:
        st.say(f"  | {line}")
    if comparison.get("ta_entries_beat_fixed_roll") is None:
        st.find(
            "SHADOW ROLL PATH EXERCISED: open + mark + comparison all ran; the "
            "comparison correctly refuses to produce a verdict while arm C has no "
            "closed P&L (no positions were ever opened in the journal)."
        )
    st.find(
        "ANALYSIS PATH EXERCISED: shadow legs, the arm C comparison, the pre-registered "
        "scorecard, the failure histogram and the weekly rollup all ran over the temp "
        "journal without raising."
    )


# ---------------------------------------------------------------------------
# stage: integrity
# ---------------------------------------------------------------------------


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]] | None:
    """(path → (size, mtime_ns)) for every file under ``root``, or ``None`` if absent.

    Used to *prove* the rehearsal did not write outside its workdir: the snapshot is
    taken before the first stage and compared in the integrity stage. Timestamps alone
    are enough — a rehearsal that wrote anything would change one.
    """
    if not root.exists():
        return None
    snap: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover - a file vanished mid-walk
                continue
            snap[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return snap


def _tree_delta(
    before: dict[str, tuple[int, int]] | None, after: dict[str, tuple[int, int]] | None
) -> dict[str, list[str]]:
    """Files added, removed or modified between two snapshots."""
    if before is None or after is None:
        return {"added": [], "removed": [], "modified": []}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "modified": sorted(p for p in set(before) & set(after) if before[p] != after[p]),
    }


class _NoNetwork:
    """Context manager that makes any outbound connection an exception.

    The rehearsal claims to be offline. A claim in a report is worth much less than a
    guard that turns a violation into a loud failure, so this patches the two entry
    points every HTTP client in the stdlib and in ``urllib`` funnels through.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._connect: Any = None
        self._create: Any = None

    def __enter__(self) -> _NoNetwork:
        import socket

        self._connect = socket.socket.connect
        self._create = socket.create_connection

        def refuse(addr: Any, *a: Any, **kw: Any) -> Any:
            self.attempts.append(repr(addr))
            raise RehearsalError(f"the rehearsal attempted a network connection to {addr!r}")

        socket.socket.connect = refuse  # type: ignore[method-assign]
        socket.create_connection = refuse  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        import socket

        socket.socket.connect = self._connect  # type: ignore[method-assign]
        socket.create_connection = self._create  # type: ignore[assignment]


def stage_integrity(r: Rehearsal, st: Stage) -> None:
    """What must be true when the rehearsal ends.

    These are the assertions that make the rest of the report trustworthy: no order
    left the process, the journal's rows are all there, and the only thing this run
    touched outside its temp dir is... nothing.
    """
    conn = r.conn
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("decisions", "noshots", "flow_baseline", "positions")
    }
    shadow_legs = conn.execute("SELECT COUNT(*) FROM shadow_roll_legs").fetchone()[0]
    shadow_marks = conn.execute("SELECT COUNT(*) FROM shadow_roll_marks").fetchone()[0]
    decisions = list_decisions(conn)
    kinds: dict[str, int] = {}
    for d in decisions:
        kinds[d.kind] = kinds.get(d.kind, 0) + 1
    workdir = r.workdir.resolve()
    opt_delta = _tree_delta(
        getattr(r, "_opt_snapshot", None), _tree_snapshot(DEPLOYED_ROOT)
    )
    rules_sha_now = _sha256(Path(DEFAULT_RULES_PATH))
    rules_sha_start = r.notes.get("rules", {}).get("sha256")
    network = getattr(r, "_net", None)
    st.detail = {
        "row_counts": counts,
        "shadow_legs": shadow_legs,
        "shadow_marks": shadow_marks,
        "decision_kinds": kinds,
        "router_actions": len(r.router.actions),
        "orders_sent": 0,
        "routed_legs": len(r.router.legs),
        "strategy_version_in_meta": get_meta(conn, "strategy_version"),
        "shipped_rulebook_sha_unchanged": bool(
            rules_sha_start and rules_sha_now == rules_sha_start
        ),
        "shipped_rulebook_sha": rules_sha_now,
        "opt_root": str(DEPLOYED_ROOT),
        "opt_tree_changed": opt_delta,
        "network_attempts": list(network.attempts) if network else [],
        "workdir": str(workdir),
    }
    st.say(f"journal rows: {counts}; shadow legs={shadow_legs} marks={shadow_marks}")
    st.say(f"decision kinds: {kinds}")
    st.say(f"router: {r.router.describe()}")
    st.say(
        f"shipped rulebook sha256 {rules_sha_now[:16]}… unchanged: "
        f"{st.detail['shipped_rulebook_sha_unchanged']}"
    )
    st.say(
        f"{DEPLOYED_ROOT}: {sum(len(v) for v in opt_delta.values())} file(s) added/removed/modified"
    )
    st.say(f"network connection attempts: {len(st.detail['network_attempts'])}")
    if counts["decisions"] < 2:
        st.ok = False
        st.error = "the journal has no decisions — nothing was actually exercised"
    if "0 orders sent)" not in r.router.describe():
        st.ok = False
        st.error = f"router claims a sent order: {r.router.describe()}"
    if not st.detail["shipped_rulebook_sha_unchanged"]:
        st.ok = False
        st.error = "the shipped rulebook changed during the rehearsal"
    if any(opt_delta.values()):
        st.ok = False
        st.error = f"the rehearsal wrote under {DEPLOYED_ROOT}: {opt_delta}"
    if st.detail["network_attempts"]:
        st.ok = False
        st.error = f"the rehearsal attempted network access: {st.detail['network_attempts'][:3]}"
    st.find(
        "INTEGRITY (checked, not asserted): the router recorded every action and sent "
        "nothing; the shipped rulebook's sha256 is byte-identical to the start of the run "
        f"({st.detail['shipped_rulebook_sha_unchanged']}), so no fixture was made to pass "
        f"by editing the rules; the deployed tree under {DEPLOYED_ROOT} is unchanged in "
        "size and mtime (it is opened read-only); and socket.connect was patched to "
        "raise, so any outbound connection would have failed the run."
    )


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------

def _stage_iv_warmup(r: Rehearsal, st: Stage) -> None:
    """T5's two problems, in one place: is the data there, and is it plumbed?"""
    deployed = r.notes.get("iv", {}).get("deployed_store", {})
    fixture = r.notes.get("iv", {}).get("fixture_store", {})
    st.detail = {"deployed": deployed, "fixture": fixture}
    if not deployed.get("present"):
        st.say(f"deployed IV store not found at {DEPLOYED_IVRANK} — skipping readiness audit")
    else:
        st.say(
            f"deployed: {deployed['observations']} rows across {deployed['tenor_keys']} "
            f"tenors; deepest tenor "
            f"{deployed['deepest_tenor_observations']}/{deployed['min_observations']} "
            f"({deployed['first_seen']}..{deployed['last_seen']})"
        )
        if not deployed["tenor_reaching_min"]:
            st.find(
                "IV WARM-UP BLOCKER: no deployed tenor has the 60 observations "
                "IvRankStore requires, so T5's rank is undefined for every live cell. "
                "This is a data-accumulation problem, not a code problem: at one soak "
                "observation per session per tenor, the first tenor to reach 60 needs 60 "
                "sessions (and the tenor keying question below decides whether that is "
                "the expiry or the rolling DTE bucket)."
            )
    st.say(
        f"fixture: {fixture.get('observations_written')} observations over "
        f"{fixture.get('tenor_keys')} tenors → ranks "
        f"{fixture.get('rank_min')}..{fixture.get('rank_max')} "
        f"(all defined: {fixture.get('all_defined')})"
    )
    st.find(
        "T5 TENOR (operator question, unchanged): the rehearsal keys the IV store on the "
        "expiry (`tenor_key`), which is what executor/hunt_plan.py already does. Whether "
        "T5 should instead read the rolling `dte_tenor_key` is still an open operator "
        "decision and was not resolved here."
    )


#: Every stage, in pipeline order. The names are what the report prints, so they read
#: as the day's work: what the operator would see from the cron trio.
STAGE_ORDER: list[tuple[str, Callable[[Rehearsal, Stage], None]]] = [
    ("rules", stage_rules),
    ("calibration", stage_calibration),
    ("simulated-freeze", stage_simulated_freeze),
    ("fixture-world", _prepare),
    # The IV warm-up lives inside fixture-world's notes; it gets its own stage so the
    # report reads as the pipeline does — one stage per job.
    ("iv-warmup", _stage_iv_warmup),
    ("plan/noshot", stage_plan_noshot),
    ("plan/shot", stage_plan_shot),
    ("watch/quiet", stage_watch_quiet),
    ("watch/stale", stage_watch_stale),
    ("watch/entry", stage_watch_entry),
    ("exits+roll", stage_exits_and_roll),
    ("eod", stage_eod),
    ("soak", stage_soak),
    ("analysis", stage_analysis),
    ("integrity", stage_integrity),
]


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(
    *,
    workdir: Path | None = None,
    calibration_db: Path | None = None,
    keep: bool = False,
) -> tuple[Rehearsal, Path]:
    """Run every stage in order and return the rehearsal plus the workdir it used.

    Two guards are installed for the duration: ``socket.connect`` raises, and the
    deployed tree under ``/opt/paper-hunter`` is snapshotted for comparison in the
    integrity stage. Both exist because the interesting failure modes of a dry rehearsal
    are the ones that would only show up as a real order or a real write.
    """
    tmp: tempfile.TemporaryDirectory | None = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="paper-hunter-rehearsal-")
        workdir = Path(tmp.name)
    workdir.mkdir(parents=True, exist_ok=True)
    r = Rehearsal(workdir, calibration_db=calibration_db)
    r._opt_snapshot = _tree_snapshot(DEPLOYED_ROOT)  # type: ignore[attr-defined]
    net = _NoNetwork()
    try:
        with net:
            r._net = net  # type: ignore[attr-defined]
            for name, fn in STAGE_ORDER:
                r.run_stage(name, fn)
    finally:
        if r.journal is not None:
            r.journal.commit()
            r.journal.close()
            r.journal = None
    r._tmp = tmp  # type: ignore[attr-defined]
    if not keep and tmp is not None:
        tmp.cleanup()
    return r, workdir


def format_report(r: Rehearsal) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add("paper-hunter — FULL-CYCLE DRY REHEARSAL (offline, no orders, no network)")
    add("=" * 78)
    for st in r.stages:
        add("")
        add(f"[{'PASS' if st.ok else 'FAIL'}] {st.name}")
        for line in st.lines:
            add(f"       {line}")
        if st.error:
            add(f"       !! {st.error}")
        for f in st.findings:
            add(f"       * {f}")
    add("")
    add("-" * 78)
    ok = all(s.ok for s in r.stages)
    add(f"VERDICT: {'every stage ran' if ok else 'A STAGE FAILED — see the FAIL block above'}")
    add(f"workdir: {r.workdir}")
    add("-" * 78)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m executor.rehearsal",
        description=(
            "Run the full daily cycle offline against a temp copy of the journal: "
            "hunt-plan, no-shot ledger, watch loop, entry/exit/roll, EOD, soak, "
            "analysis. No network, no credentials, no orders, no writes under /opt."
        ),
    )
    ap.add_argument("--workdir", type=Path, default=None, help="keep artifacts in this dir")
    ap.add_argument(
        "--calibration-db",
        type=Path,
        default=None,
        help=f"flow_baseline source for the T6 proposal (default: {DEPLOYED_JOURNAL}, read-only)",
    )
    ap.add_argument("--json", type=Path, default=None, help="write the report JSON here")
    ap.add_argument("--keep", action="store_true", help="keep the temp workdir")
    args = ap.parse_args(argv)

    r, workdir = run(
        workdir=args.workdir,
        calibration_db=args.calibration_db,
        keep=args.keep or bool(args.workdir),
    )
    print(format_report(r))
    if args.json:
        args.json.write_text(json.dumps(r.to_dict(), indent=2, default=str))
        print(f"\nreport JSON: {args.json}")
    if not all(s.ok for s in r.stages):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
