"""Intraday watch loop — Phase 3b. Injectable, dry-runnable, no network in tests.

The tick
--------
One pass of :func:`run_once` does exactly this, in this order, and the order is the
design:

1. **Fetch.** One :class:`WatchSnapshot` from the injected provider, stamped with the
   moment the data was read.
2. **Age it.** ``now - snapshot.fetched_at`` against :data:`DEFAULT_MAX_SNAPSHOT_AGE_SECONDS`.
   The brief forbids stale-checklist entries: "checklist re-verified with live data at
   trigger time (stale-checklist entries are forbidden)". A snapshot older than the
   budget cannot support an *entry*, whatever it says.
3. **Exits first.** :class:`~executor.position_manager.PositionManager` runs the frozen
   exit ladder for every open position. Exits outrank entries because an exit that
   waits behind a checklist evaluation is an exit that can be skipped by a data glitch.
4. **Entries.** Only if the snapshot is fresh: for each plan cell whose price trigger
   has fired, re-run the FULL checklist against the live snapshot. A trigger that
   fires on a cell that no longer passes is a VETO, journaled with the fresh values.
5. **Journal.** Every action, veto and re-verification goes to the sink.

# INTERPRETATION (staleness budget): the rulebook has no field for "how old may the
snapshot be", and inventing a config key is a rule change. The budget is therefore a
documented module constant — 60 seconds — chosen against the loop's own cadence
(default poll 15s) so three consecutive bad polls trip it rather than one. **Staleness
blocks entries only.** Exits always run: refusing to close a position because a quote
is old is the one behaviour that could lose real paper money, and the alternative
(fabricating a fresh number) is worse. When the option quote itself is missing, the
manager is called with the entry price as the mark, which makes the gain 0% — the
profit take (+100%) cannot fire on a missing quote, the time stop and the 15:30 hard
close still can, and the journal records that the mark was unavailable.

# INTERPRETATION (trigger direction): a plan cell's triggers carry a ``sense``
("above"/"below"). A trigger counts as fired when spot has reached the level *in the
trade direction of the cell*: for a call cell, a "below" trigger (e.g. "close back
inside the lower band") fires when spot is above the level; for a put cell the mirror.
Cells with no actionable trigger price are not watchable and are skipped — the brief's
cadence is "trigger-price watch on the plan's levels", and there is no level to watch.

# INTERPRETATION (re-entry): a cell that has already produced an entry action this
session is not re-entered on a later tick, even if spot re-crosses the trigger. Arm B
governs itself with max 1 concurrent + 3/week, but arm C's roll could otherwise
re-enter the same cell forever, and "one position per cell per session" is the
mechanical reading of "Max 1 concurrent position" plus "wait for full checklist
re-satisfaction" (the checklist has to be re-run from a PENDING/FAIL state, and a
cell that fired and traded is not in one).

Dry-run
-------
:class:`DryRunRouter` is the default. It records what *would* have been sent and sends
nothing, so ``python -m executor.main watch`` is safe with no credentials at all. The
live path requires ``--live`` on the CLI, which additionally demands the paper keys and
fetches ``/v2/account`` before the first order (see :mod:`executor.alpaca_trading`).

Python 3.12+, stdlib + the rest of ``executor/``.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from config.loader import Rulebook
from data.event_calendar import EventCalendar
from executor.alpaca_client import BarSeries, OptionChain, OptionContract
from executor.checklist import ChecklistResult, evaluate
from executor.hunt_plan import ArmKey, ArmPlan, HuntPlan
from executor.position_manager import (
    Action,
    ArmState,
    Evaluation,
    JournalRecord,
    JournalSink,
    MemoryJournalSink,
    NullJournalSink,
    OrderLeg,
    PositionManager,
    RollSelection,
    Veto,
    et_time,
    iso_ts,
    to_et,
)
from executor.snapshot_builder import SnapshotResult, build_snapshot

#: Documented staleness budget for entry decisions (see module INTERPRETATION).
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 60.0

#: Default intraday cadence. The brief says "continuous"; 15s is ~4 polls/minute,
#: comfortably inside the 200 calls/min free-tier budget for the handful of calls a
#: tick makes, and fast enough that a 15:30 hard close is never late.
DEFAULT_POLL_SECONDS = 15.0

#: Loop stop time, ET. 16:00 is the cash close; the 15:30 hard close means an arm B
#: position is already flat by then, and arm C is marked but not sold here.
DEFAULT_STOP_ET = "16:00"


class WatchLoopError(RuntimeError):
    """Raised when a tick cannot be run honestly (no plan, unusable provider)."""


# ---------------------------------------------------------------------------
# provider seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchSnapshot:
    """One read of the world, stamped with when it was read.

    ``fetched_at`` is the loop's own clock at read time, not a bar timestamp: the
    staleness check is about how old *this read* is, and a daily bar can be a day old
    while the snapshot is two seconds old (the daily series is the checklist's history;
    ``spot`` is the live price).
    """

    fetched_at: dt.datetime
    symbol: str
    spot: float
    daily: BarSeries
    chain: OptionChain | None = None
    price_source: str = "last intraday bar close"
    notes: tuple[str, ...] = ()

    def age_seconds(self, now: dt.datetime) -> float:
        return (now - self.fetched_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.isoformat(),
            "symbol": self.symbol,
            "spot": self.spot,
            "daily_bars": len(self.daily),
            "chain_contracts": len(self.chain) if self.chain is not None else None,
            "price_source": self.price_source,
            "notes": list(self.notes),
        }


@runtime_checkable
class WatchDataProvider(Protocol):
    """What the loop needs. Two methods; tests inject a fixture implementation."""

    def watch_snapshot(self, symbol: str) -> WatchSnapshot: ...

    def option_price(self, symbol: str, *, snapshot: WatchSnapshot) -> float | None: ...


class StaticWatchData:
    """Offline provider: everything handed in at construction. No I/O."""

    def __init__(
        self,
        *,
        spot: float,
        daily: BarSeries,
        chain: OptionChain | None = None,
        fetched_at: dt.datetime | None = None,
        option_prices: dict[str, float] | None = None,
        price_source: str = "fixture",
    ) -> None:
        self.spot = spot
        self.daily = daily
        self.chain = chain
        self.fetched_at = fetched_at
        self.option_prices = dict(option_prices or {})
        self.price_source = price_source
        self.snapshots: list[WatchSnapshot] = []

    def watch_snapshot(self, symbol: str) -> WatchSnapshot:
        if self.daily.symbol != symbol:
            raise WatchLoopError(
                f"StaticWatchData holds {self.daily.symbol}, asked for {symbol}"
            )
        snapshot = WatchSnapshot(
            fetched_at=self.fetched_at or dt.datetime.now(dt.UTC),
            symbol=symbol,
            spot=self.spot,
            daily=self.daily,
            chain=self.chain,
            price_source=self.price_source,
        )
        self.snapshots.append(snapshot)
        return snapshot

    def option_price(self, symbol: str, *, snapshot: WatchSnapshot) -> float | None:
        if symbol in self.option_prices:
            return self.option_prices[symbol]
        return contract_price(contract_by_symbol(snapshot.chain, symbol))


class AlpacaWatchData:
    """Live provider over the read-only :class:`~executor.alpaca_client.AlpacaClient`.

    Kept separate from the trading client on purpose: the loop's data reads and its
    order placement have different safety properties, and a data read should not
    require the order module's credentials path.

    # INTERPRETATION: ``spot`` is the close of the most recent 1-minute IEX bar. On
    the free tier that is the only realtime equity price available (the research note
    is explicit), and a stale-by-15s last trade is better than nothing for trigger
    watching. ``fetched_at`` is stamped after the read completes, so the staleness
    budget measures read latency, not bar age.
    """

    def __init__(
        self,
        client: Any,
        *,
        timeframe: str = "1Min",
        feed: str = "iex",
        daily_feed: str = "sip",
        daily_limit: int = 400,
    ) -> None:
        self.client = client
        self.timeframe = timeframe
        self.feed = feed
        self.daily_feed = daily_feed
        self.daily_limit = daily_limit

    def watch_snapshot(self, symbol: str) -> WatchSnapshot:
        started = dt.datetime.now(dt.UTC)
        intraday = self.client.get_intraday_bars(
            symbol, timeframe=self.timeframe, feed=self.feed, limit=5
        )
        if not intraday.bars:
            raise WatchLoopError(
                f"no {self.timeframe} bars for {symbol}; cannot watch a trigger without a price"
            )
        daily = self.client.get_daily_bars(symbol, feed=self.daily_feed, limit=self.daily_limit)
        chain = self.client.get_option_chain(symbol)
        return WatchSnapshot(
            fetched_at=dt.datetime.now(dt.UTC),
            symbol=symbol,
            spot=intraday.bars[-1].c,
            daily=daily,
            chain=chain,
            price_source=f"{self.timeframe}/{self.feed} last bar close",
            notes=(f"read started {started.isoformat(timespec='seconds')}",),
        )

    def option_price(self, symbol: str, *, snapshot: WatchSnapshot) -> float | None:
        return contract_price(contract_by_symbol(snapshot.chain, symbol))


def contract_by_symbol(chain: OptionChain | None, symbol: str) -> OptionContract | None:
    if chain is None:
        return None
    for contract in chain:
        if contract.symbol == symbol:
            return contract
    return None


def contract_price(contract: OptionContract | None) -> float | None:
    """Mark for an option contract, in the direction that liquidates a long.

    bid → mid → ask, in that order. Selling a long option fills near the bid, so bid
    first; asking first would mark exits ~1 spread too high, which biases the time
    stop and the profit take in the direction of holding. Documented because the
    choice is not neutral.
    """
    if contract is None:
        return None
    quote = contract.latest_quote
    if quote is not None:
        if quote.bid is not None:
            return quote.bid
        if quote.mid is not None:
            return quote.mid
        if quote.ask is not None:
            return quote.ask
    trade = contract.latest_trade
    return trade.p if trade is not None else None


# ---------------------------------------------------------------------------
# order routing
# ---------------------------------------------------------------------------


class OrderRouter(Protocol):
    """Where an :class:`Action` goes. Dry-run and live are both implementations."""

    def route(self, action: Action) -> tuple[str, ...]:
        """Submit the action's legs. Returns order ids (empty in dry run)."""

    def describe(self) -> str: ...


class DryRunRouter:
    """Records what would have been sent and sends nothing.

    This is the default everywhere in this module: a tick with no router, a test, or a
    CLI run without ``--live`` cannot place an order even if a bug asks it to.
    """

    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.legs: list[OrderLeg] = []

    def route(self, action: Action) -> tuple[str, ...]:
        self.actions.append(action)
        self.legs.extend(action.legs)
        return ()

    def describe(self) -> str:
        return f"DRY RUN ({len(self.actions)} action(s) recorded, 0 orders sent)"


class LiveOrderRouter:
    """Submits actions to the paper account, behind a one-way proof of paper-ness.

    The :class:`PaperConfirmation` is fetched once at construction and re-checked
    before every action batch. It is not cached forever: an account whose trading
    permissions change mid-session should stop taking orders, and re-reading
    ``/v2/account`` per batch is one call the budget can afford.

    Multi-leg actions (a roll) submit leg 0 first and leg 1 second; if leg 1 fails the
    exception propagates with leg 0 already filled, which is the honest failure mode of
    a mechanical two-order roll. It is not wrapped in anything clever on purpose.
    """

    def __init__(self, client: Any, *, banner: bool = True) -> None:
        self.client = client
        self.confirmation = client.assert_paper_account()
        self.submitted: list[dict[str, Any]] = []
        if banner:
            print(self.confirmation.banner())

    def route(self, action: Action) -> tuple[str, ...]:
        if self.confirmation.account_id != self.client.assert_paper_account().account_id:
            raise WatchLoopError(
                "account changed mid-session; refusing further orders "
                f"(was {self.confirmation.account_id})"
            )
        ids: list[str] = []
        for leg in action.legs:
            order = self.client.submit_order(
                symbol=leg.symbol,
                qty=leg.qty,
                side=leg.side,
                order_type=leg.order_type,
                limit_price=leg.limit_price,
                asset_class=leg.asset_class,
            )
            ids.append(order.id)
            self.submitted.append(
                {"action": action.kind, "arm": action.arm, "order": order.to_dict()}
            )
        return tuple(ids)

    def describe(self) -> str:
        return (
            f"PAPER LIVE ({self.confirmation.banner()}) — {len(self.submitted)} order(s) sent"
        )


# ---------------------------------------------------------------------------
# loop state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchState:
    """What the loop carries from tick to tick.

    ``arms`` is a plain dict so a caller can hold the three arms' states without the
    union boilerplate; it is replaced (never mutated) so a :class:`TickResult` is a
    value, not a window onto a moving object.
    """

    day: dt.date
    plan: HuntPlan
    arms: dict[str, ArmState] = field(default_factory=dict)
    traded_cells: frozenset[ArmKey] = frozenset()
    #: Cells already given their one green-plan re-verification this session (R2).
    #: Mutated in place by the watch loop via :func:`_green_cell_armed` rather than
    #: threaded through ``TickResult``; ``run_loop`` re-seeds it per session.
    green_checked: set[ArmKey] = field(default_factory=set)
    ticks: int = 0
    last_notes: tuple[str, ...] = ()

    @classmethod
    def initial(
        cls, *, day: dt.date, plan: HuntPlan, rules: Rulebook
    ) -> WatchState:
        return cls(
            day=day,
            plan=plan,
            arms={
                "A": ArmState(bankroll_usd=rules.arms.A.bankroll_usd),
                "B": ArmState(bankroll_usd=rules.arms.B.bankroll_usd),
                "C": ArmState(bankroll_usd=rules.arms.C.bankroll_usd),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "ticks": self.ticks,
            "traded_cells": [list(k) for k in sorted(self.traded_cells)],
            "arms": {arm: s.to_dict() for arm, s in sorted(self.arms.items())},
            "last_notes": list(self.last_notes),
        }


@dataclass(frozen=True, slots=True)
class TickResult:
    """One tick's complete, inspectable outcome."""

    now: dt.datetime
    snapshot_age_seconds: float
    stale: bool
    exit_actions: tuple[Action, ...] = ()
    entry_actions: tuple[Action, ...] = ()
    vetoes: tuple[Veto, ...] = ()
    reverified: dict[ArmKey, ChecklistResult] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    journaled: int = 0
    next_state: WatchState | None = None

    @property
    def actions(self) -> tuple[Action, ...]:
        return self.exit_actions + self.entry_actions

    @property
    def quiet(self) -> bool:
        """No action, no veto, nothing re-verified — a tick that did nothing."""
        return not (self.actions or self.vetoes or self.reverified)

    def to_dict(self) -> dict[str, Any]:
        return {
            "now_et": to_et(self.now).isoformat(timespec="seconds"),
            "snapshot_age_seconds": self.snapshot_age_seconds,
            "stale": self.stale,
            "exit_actions": [a.to_dict() for a in self.exit_actions],
            "entry_actions": [a.to_dict() for a in self.entry_actions],
            "vetoes": [v.to_dict() for v in self.vetoes],
            "reverified": {f"{k[0]}/{k[1]}": r.to_dict() for k, r in self.reverified.items()},
            "notes": list(self.notes),
            "journaled": self.journaled,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def trigger_fired(cell: ArmPlan, spot: float) -> tuple[str, ...]:
    """Which of the cell's actionable trigger levels ``spot`` has reached.

    Sense is interpreted relative to the cell's trade direction: a call cell wants to
    be *above* an "above" level and *above* a "below" level (the band-rejection level
    is "close back inside", which for a call means price recovered above the lower
    band). A put cell is the mirror.
    """
    want_above = cell.direction == "call"
    fired: list[str] = []
    for trigger in cell.triggers:
        if not trigger.actionable or trigger.level is None or not trigger.sense:
            continue
        if want_above and spot >= trigger.level:
            fired.append(f"{trigger.condition} {trigger.sense} {trigger.level:.2f}")
        elif not want_above and spot <= trigger.level:
            fired.append(f"{trigger.condition} {trigger.sense} {trigger.level:.2f}")
    return tuple(fired)


def reverify_cell(
    cell: ArmPlan,
    *,
    snapshot: WatchSnapshot,
    rules: Rulebook,
    calendar: EventCalendar | None = None,
    iv_rank: float | None = None,
    flow_gate: Any | None = None,
) -> ChecklistResult:
    """Re-run the full checklist for ``cell`` against the live snapshot.

    The whole point of the intraday loop: the plan's checklist verdict was computed
    pre-market from yesterday's bars. This rebuilds the snapshot from the live series
    and evaluates the arm's conditions again, so what gets journaled at trigger time
    is what the system believes *now*.
    """
    chain = snapshot.chain
    result: SnapshotResult = build_snapshot(
        daily=snapshot.daily,
        chain=chain,
        calendar=calendar,
        veto_kinds=rules.checklist.t5_options_chain.event_calendar.veto_kinds,
        as_of=to_et(snapshot.fetched_at).date(),
        spot=snapshot.spot,
        iv_rank=iv_rank,
        flow_gate=flow_gate,
        ema_fast=rules.checklist.t1_ema_alignment.ema_fast,
        ema_slow=rules.checklist.t1_ema_alignment.ema_slow,
        rsi_period=rules.checklist.t2_rsi.period,
        macd_fast=rules.checklist.t2b_macd.fast,
        macd_slow=rules.checklist.t2b_macd.slow,
        macd_signal=rules.checklist.t2b_macd.signal,
        bb_lookback_days=rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release.lookback_days,
        bb_squeeze_percentile=rules.checklist.t3_bollinger.satisfied_if_any_of.squeeze_release.bandwidth_below_percentile,
        relvol_lookback_days=rules.checklist.t4_volume.lookback_days,
        cross_guard_hours=rules.checklist.t2b_macd.fresh_cross_guard.max_age_hours,
    )
    return evaluate(result.snapshot, rules, cell.direction, cell.arm)  # type: ignore[arg-type]


def _entry_leg(cell: ArmPlan, *, symbol: str, price: float | None) -> OrderLeg:
    """Build the buy leg for an entry. Size is 1 contract for both active arms.

    # INTERPRETATION: one contract per position. The brief caps arm B's premium in
    dollars and arm C's in percent of bankroll, but never states a contract count; 1
    is the smallest thing that is a trade, and the premium cap is what actually limits
    size. Scaling to more contracts is a rule change, not an implementation detail.
    """
    return OrderLeg(
        symbol=symbol,
        qty=1,
        side="buy",
        order_type="limit" if price is not None else "market",
        limit_price=price,
    )


def _arm_b_contract(
    cell: ArmPlan, snapshot: WatchSnapshot
) -> tuple[OrderLeg, float] | None:
    """Pick arm B's contract and its total premium, or ``None`` if the chain can't say.

    Strike selection re-uses the plan's own projection: the cell's watch levels carry
    ``strike_projection`` (the "first OTM strike beyond the setup-day range
    projection"), and the loop picks the first contract beyond that level from the
    LIVE chain. The plan's strike choice is not reused — it was made from a
    pre-market chain and the brief's cadence says re-verify with live data.
    """
    projection = cell.watch.strike_projection or {}
    level = projection.get("level")
    if level is None or snapshot.chain is None:
        return None
    want_call = cell.direction == "call"
    candidates = [
        c
        for c in snapshot.chain
        if c.expiry == snapshot.chain.contracts[0].expiry  # nearest expiry only (0DTE)
        and ((c.strike > level) if want_call else (c.strike < level))
        and c.right == cell.direction
    ]
    if not candidates:
        return None
    contract = min(candidates, key=lambda c: abs(c.strike - level))
    price = contract_price(contract) or None
    premium = (price or 0.0) * 100.0
    return _entry_leg(cell, symbol=contract.symbol, price=price), premium


# ---------------------------------------------------------------------------
# the tick
# ---------------------------------------------------------------------------


def _iv_rank_for_cell(
    iv_rank: float | Mapping[str, float] | None,
    cell: ArmPlan,
) -> float | None:
    """Resolve the IV rank for one cell.

    R4 (2026-10-03): T5's rank is read per tenor, and the tenors differ per cell — arm
    B's 0DTE call and arm C's 150-DTE call are different series with different warm-up
    states. The CLI used to compute ONE number and forward it to every cell, so a rank
    that warmed up for one contract was asserted for all of them. Accepted forms:
    ``None`` (no read at all — T5 PENDING, which is the honest default), a bare float
    (one number for every cell, kept for tests and for ``--iv-rank``), or a mapping
    keyed by :data:`executor.hunt_plan.ArmKey` (an ``(arm, direction)`` tuple).
    """
    if iv_rank is None:
        return None
    if isinstance(iv_rank, Mapping):
        value = iv_rank.get(cell.key)
        return float(value) if isinstance(value, (int, float)) else None
    return float(iv_rank)


def _t5_iv_rank_defined(
    iv_rank: float | Mapping[str, float] | None, plan: HuntPlan | None
) -> bool:
    """Whether the rulebook's arm-B gate condition (``t5_iv_rank_defined``) holds.

    RULED 2026-10-03 (operator, R5): the window opens arms A + C; arm B waits for T5.
    The gate is checked against what this session ACTUALLY resolved, not against a
    stored flag, so a run that failed to read the store cannot inherit yesterday's
    "warm" answer and quietly start trading arm B.

    Any cell's rank counts, not just arm B's: the condition the ruling names is "T5
    can be evaluated", and T5 is one checklist condition shared by both arms. Once a
    tenor is warm the store keeps it warm; the question is whether we can read it.
    """
    if isinstance(iv_rank, Mapping):
        if any(isinstance(v, (int, float)) for v in iv_rank.values()):
            return True
        return False
    if isinstance(iv_rank, (int, float)):
        return True
    if plan is not None:
        for cell in plan.arms:
            if isinstance(cell.snapshot_dict.get("iv_rank"), (int, float)):
                return True
    return False


def _green_cell_armed(
    cell: ArmPlan,
    state: WatchState,
    snapshot: Any,
    now: dt.datetime,
    rules: Rulebook,
) -> bool:
    """Whether a cell whose plan-time triggers all decayed is armed anyway (R2).

    RULED 2026-10-03 (operator): "a green plan arms the watch loop on the entry window
    alone". A cell qualifies when its PLAN checklist fired (it is green as of the
    pre-market read) and the current time is inside the arm's own ``entry.window_et``.

    RULED 2026-10-03 (operator, second ruling): **arm C declares a window too**,
    09:45-15:30 ET. Until then this function treated a missing window as the whole
    session — a placeholder nobody had ruled, which also meant R2's "the entry window
    alone" had no boundary to be alone *to* for the arm R1's roll can leave past its
    trigger. The window is read through
    :func:`executor.position_manager.arm_entry_window_open`, the same helper the entry
    governors use, so the loop and the manager cannot disagree about the boundary.

    The window bounds ENTRIES. R1's roll is not gated by it: a position already past
    its roll trigger is managed whenever the loop runs.

    THROTTLE, and this is an interpretation rather than a ruling: each green cell is
    re-verified ONCE per session, on the first tick inside its window, not on every
    tick. Un-throttled, a green cell that does not fire writes a NO_TRADE row every 15
    seconds for 4h15m — ~1000 identical rows per cell per day, which
    ``analysis.rollup.checklist_failure_histogram`` would then report as T1 (or
    whichever condition failed) vetoing ~99% of all decisions. In a project whose whole
    claim is honest measurement, a corrupted veto histogram is a worse outcome than a
    missed entry, and a missed entry is at least visible as a NO-SHOT row. Lifting the
    throttle is a one-line change here.
    """
    if cell.key in state.green_checked:
        return False
    from executor.position_manager import arm_entry_window_open

    if not arm_entry_window_open(cell.arm, now, rules):
        return False
    if not _cell_plan_green(cell):
        return False
    object.__setattr__(state, "green_checked", state.green_checked | {cell.key})
    return True


def _roll_selection_for(
    arm_state: ArmState,
    snapshot: WatchSnapshot,
    day: dt.date,
    rules: Rulebook,
    provider: WatchDataProvider,
) -> tuple[RollSelection | None, str | None]:
    """Pick arm C's roll replacement from the tick's own chain (R1).

    The chain rides along on :class:`WatchSnapshot` — ``AlpacaWatchData`` already
    fetched it for the contract quote — so the roll costs no extra network call. A
    missing chain means no roll this pass, which the manager reports.

    Returns ``(selection, error)``. The error is returned rather than swallowed so a
    selection bug shows up in the tick's notes instead of presenting as "no
    replacement available" — the two look identical from the outside and only one of
    them is a bug.
    """
    from executor.position_manager import arm_c_roll_replacement

    if snapshot.chain is None:
        return None, None
    try:
        selection = arm_c_roll_replacement(
            chain=snapshot.chain,
            spot=snapshot.spot,
            day=day,
            rules=rules,
            state=arm_state,
        )
    except Exception as exc:  # noqa: BLE001 - a roll-selection bug must not kill a tick
        # The tick continues, so the exits that already ran are not lost with it.
        return None, f"arm C roll selection raised {type(exc).__name__}: {exc}"
    return selection, None


def _cell_plan_green(cell: ArmPlan) -> bool:
    """Whether the cell's pre-market checklist fired.

    Read from the plan's own snapshot rather than re-evaluated: the point is "the plan
    was green", which is a fact about 08:30, not a second opinion about now. Arm A is
    excluded because it is a pre-market single order with no intraday checklist.
    """
    if cell.arm == "A":
        return False
    # `cell.checklist` is the plan-time evaluation; the fallback to `fire` covers a
    # plan loaded from a file written before either field was recorded.
    checklist = getattr(cell, "checklist", None)
    if checklist is not None and hasattr(checklist, "fire"):
        return bool(checklist.fire)
    return bool(cell.snapshot_dict.get("plan_green", False))


def run_once(
    *,
    provider: WatchDataProvider,
    rules: Rulebook,
    state: WatchState,
    now: dt.datetime,
    router: OrderRouter | None = None,
    journal: JournalSink | None = None,
    manager: PositionManager | None = None,
    calendar: EventCalendar | None = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    iv_rank: float | Mapping[str, float] | None = None,
    flow_gate: Any | None = None,
) -> TickResult:
    """One intraday tick. Pure-ish: no sleep, no clock read, no network of its own.

    ``now`` is injected so a test can drive the clock to 15:29:59 and 15:30:00 and
    prove the hard close fires on the right side of the boundary. ``router`` defaults
    to :class:`DryRunRouter`, so calling this without one cannot place an order.
    """
    router = router or DryRunRouter()
    journal = journal or NullJournalSink()
    manager = manager or PositionManager(rules, journal=journal)
    symbol = rules.strategy.symbol

    snapshot = provider.watch_snapshot(symbol)
    age = snapshot.age_seconds(now)
    stale = age > max_snapshot_age_seconds or age < -1.0
    notes: list[str] = []
    if stale:
        notes.append(
            f"snapshot age {age:.1f}s exceeds the {max_snapshot_age_seconds:.0f}s entry "
            f"budget (or the clock moved backwards) — entries refused this tick; "
            f"exits still evaluated"
        )

    arms = dict(state.arms)
    exit_actions: list[Action] = []
    entry_actions: list[Action] = []
    vetoes: list[Veto] = []
    reverified: dict[ArmKey, ChecklistResult] = {}
    journaled = 0
    traded_now: set[ArmKey] = set()

    # -- 3. exits first -------------------------------------------------------
    for arm in ("B", "C"):
        arm_state = arms.get(arm, ArmState())
        position = arm_state.position
        if position is None:
            continue
        price = provider.option_price(position.symbol, snapshot=snapshot)
        if price is None:
            notes.append(
                f"{arm}: no quote for {position.symbol}; marked at entry price for the "
                f"exit ladder (profit take cannot fire on a missing quote; time stop "
                f"and hard close still can)"
            )
            mark = position.entry_price
        else:
            mark = price
        delta, dte = _greeks_for(position, snapshot)
        # RULED 2026-10-03 (operator, R1): arm C's roll needs a replacement leg, and
        # selecting it requires the option chain — which the watch provider does not
        # currently expose. Until it does, the roll cannot be taken live and the
        # manager says so in a note rather than holding the position in silence. This
        # is the one place the intraday loop is known-incomplete, and it is named in
        # `docs/ratification.md` rather than papered over.
        selection = roll_error = None
        if arm == "C":
            selection, roll_error = _roll_selection_for(
                arm_state, snapshot, state.day, rules, provider
            )
            if roll_error is not None:
                notes.append(roll_error)
        evaluation = manager.evaluate_exits(
            arm,  # type: ignore[arg-type]
            now=now,
            state=arm_state,
            price=mark,
            delta=delta,
            dte=dte,
            replacement=selection.leg if selection is not None else None,
            selection=selection,
        )
        notes.extend(evaluation.notes)
        arms[arm] = evaluation.next_state or arm_state
        for action in evaluation.actions:
            journaled += (
                1 if manager.journal_action(
                    action,
                    now=now,
                    snapshot={
                        "enforcement": {
                            "reference_price": mark,
                            "quote_available": price is not None,
                        },
                        "indicators": {},
                    },
                    journal=journal,
                )
                else 0
            )
            router.route(action)
            exit_actions.append(action)
            arms[arm] = _apply_exit(arm, arms[arm], action)
        notes.extend(evaluation.notes)

    # -- 4. entries ------------------------------------------------------------
    # RULED 2026-10-03 (operator, R2): a GREEN plan arms the watch loop on the entry
    # window alone. Previously a cell whose pre-market triggers had all decayed was
    # skipped before any live re-verification, so a plan that was genuinely green at
    # 08:30 became unreachable the moment the day's first trigger decayed — the loop
    # could only ever enter on a trigger it had already seen, which is the opposite of
    # "the checklist decides". "Green" here means the PLAN's checklist fired; whether
    # it still fires is exactly the question the live re-verification exists to answer,
    # so re-verifying costs one evaluation and the answer is journalled either way.
    window_arms = rules.active_arms(
        t5_iv_rank_defined=_t5_iv_rank_defined(iv_rank, state.plan)
    )
    for cell in state.plan.arms:
        if cell.arm not in ("B", "C"):
            continue  # arm A is a pre-market single order, handled by main.py
        if cell.key in state.traded_cells:
            continue
        if cell.arm not in window_arms:
            # R5: the window defers this arm. Skipped BEFORE any evaluation so it
            # costs nothing and produces no journal row per tick — a deferred arm
            # leaving a NO_TRADE trail would read at the monthly review as an arm
            # that was trading and finding nothing.
            continue
        arm_state = arms.get(cell.arm, ArmState())
        fired_triggers = trigger_fired(cell, snapshot.spot)
        if not fired_triggers:
            if not _green_cell_armed(cell, state, snapshot, now, rules):
                continue
            fired_triggers = ("green_plan_entry_window",)
        if stale:
            veto = Veto(
                cell.arm,  # type: ignore[arg-type]
                "stale_snapshot",
                f"trigger fired ({'; '.join(fired_triggers)}) but the snapshot is "
                f"{age:.1f}s old — stale-checklist entries are forbidden",
                {"snapshot_age_seconds": age, "triggers": list(fired_triggers)},
                arm_state.to_dict(),
            )
            journaled += 1 if manager.journal_veto(veto, now=now, journal=journal) else 0
            vetoes.append(veto)
            continue

        # live re-verification of the whole checklist
        result = reverify_cell(
            cell,
            snapshot=snapshot,
            rules=rules,
            calendar=calendar,
            iv_rank=_iv_rank_for_cell(iv_rank, cell),
            flow_gate=flow_gate,
        )
        reverified[cell.key] = result
        journaled += _journal_reverification(
            journal, cell, result, now=now, fired=fired_triggers, snapshot=snapshot
        )
        if not result.fire:
            veto = Veto(
                cell.arm,  # type: ignore[arg-type]
                "checklist_reverification",
                f"trigger fired ({'; '.join(fired_triggers)}) but the live checklist "
                f"does not: {'; '.join(result.veto_reasons)}",
                {"triggers": list(fired_triggers), "conditions": result.to_dict()["conditions"]},
                arm_state.to_dict(),
            )
            journaled += 1 if manager.journal_veto(veto, now=now, journal=journal) else 0
            vetoes.append(veto)
            continue

        evaluation = _entry_evaluation(
            manager,
            cell=cell,
            now=now,
            arm_state=arm_state,
            snapshot=snapshot,
        )
        arms[cell.arm] = evaluation.next_state or arm_state
        vetoes.extend(evaluation.vetoes)
        for veto in evaluation.vetoes:
            journaled += 1 if manager.journal_veto(veto, now=now, journal=journal) else 0
        for action in evaluation.actions:
            router.route(action)
            entry_actions.append(action)
            journaled += (
                1 if manager.journal_action(
                    action, now=now, snapshot=result.indicators, journal=journal
                ) else 0
            )
            traded_now.add(cell.key)

    next_state = replace(
        state,
        arms=arms,
        ticks=state.ticks + 1,
        traded_cells=state.traded_cells | frozenset(traded_now),
        last_notes=tuple(notes),
    )
    if journaled:
        notes.append(f"journaled {journaled} decision entr(ies)")
    return TickResult(
        now=now,
        snapshot_age_seconds=age,
        stale=stale,
        exit_actions=tuple(exit_actions),
        entry_actions=tuple(entry_actions),
        vetoes=tuple(vetoes),
        reverified=reverified,
        notes=tuple(notes),
        journaled=journaled,
        next_state=next_state,
    )


def _entry_evaluation(
    manager: PositionManager,
    *,
    cell: ArmPlan,
    now: dt.datetime,
    arm_state: ArmState,
    snapshot: WatchSnapshot,
) -> Evaluation:
    """Ask the manager for an entry opinion, per arm's own governor shape."""
    if cell.arm == "B":
        picked = _arm_b_contract(cell, snapshot)
        if picked is None:
            veto = Veto(
                "B",
                "no_candidate",
                "no OTM contract beyond the setup-day range projection in the live "
                "chain (nearest expiry) — nothing to buy, nothing journaled as a trade",
                {"spot": snapshot.spot, "projection": cell.watch.strike_projection},
                arm_state.to_dict(),
            )
            return Evaluation(actions=(), vetoes=(veto,), next_state=arm_state)
        leg, premium = picked
        return manager.evaluate_entry(
            "B",
            now=now,
            state=arm_state,
            price=snapshot.spot,
            symbol=leg.symbol,
            premium_usd=premium,
        )
    # Arm C: the selection step owns DTE/delta/premium; hand the manager the leg.
    leg = cell.watch.arm_criteria.get("order_leg")
    replacement = None
    if isinstance(leg, OrderLeg):
        replacement = leg
    else:
        replacement = _arm_c_contract(cell, snapshot)
    return manager.evaluate_entry(
        "C",
        now=now,
        state=arm_state,
        price=snapshot.spot,
        symbol=replacement.symbol if replacement else "",
        replacement=replacement,
    )


def _arm_c_contract(cell: ArmPlan, snapshot: WatchSnapshot) -> OrderLeg | None:
    """First live contract meeting arm C's entry criteria (90-180 DTE, delta >= 0.80).

    Re-derived from the live chain rather than trusted from the plan — the same
    discipline as arm B's strike. Premium is capped at 50% of the bankroll; if the
    cheapest qualifying contract breaches that, ``None`` is returned and the manager's
    "no_candidate" veto fires, because "no affordable contract" is a veto with a
    reason, not a silent skip.
    """
    # FIX (2026-10-03, full-cycle rehearsal): the attribute is ``arm_c_criteria``
    # (only ``to_dict()`` spells it ``arm_c_criteria``), so this line raised
    # AttributeError on every arm C entry attempt.
    entry = cell.watch.arm_criteria
    bankroll = float(entry.get("bankroll_usd", 0.0)) or 0.0
    # FIX (2026-10-03, full-cycle rehearsal): the criteria dict the plan writes is
    # keyed ``premium_pct_of_bankroll_max``; this read ``premium_pct_max``, which the
    # plan never writes, so the cap silently defaulted to 0.5 and the ``max_premium``
    # below was zero. The DTE band and the delta floor are read from the plan's own
    # criteria too, so the live selection is driven by the rulebook rather than by
    # literals repeated here (the shipped values are unchanged: 90-180 DTE, delta 0.80).
    max_premium = float(entry.get("premium_pct_of_bankroll_max", 0.5)) * bankroll
    dte_min = int(entry.get("dte_min", 90))
    dte_max = int(entry.get("dte_max", 180))
    delta_min = float(entry.get("delta_min", 0.80))
    candidates: list[tuple[float, OrderLeg]] = []
    for contract in snapshot.chain or ():
        if contract.right != cell.direction:
            continue
        dte = _dte_from_expiry(contract.expiry, snapshot)
        if dte is None or not (dte_min <= dte <= dte_max):
            continue
        delta = contract.greeks.delta if contract.greeks else None
        if delta is None or delta < delta_min:
            continue
        price = contract_price(contract)
        if price is None or price * 100.0 > max_premium or max_premium <= 0:
            continue
        candidates.append(
            (price, _entry_leg(cell, symbol=contract.symbol, price=price))
        )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _greeks_for(position: Any, snapshot: WatchSnapshot) -> tuple[float | None, int | None]:
    """Delta and DTE for an open position, from the live chain when it is there."""
    contract = contract_by_symbol(snapshot.chain, position.symbol)
    if contract is None:
        return (position.delta, _dte_from_expiry(position.expiry, snapshot))
    delta = contract.greeks.delta if contract.greeks else position.delta
    return (delta, _dte_from_expiry(contract.expiry, snapshot))


def _dte_from_expiry(expiry: str | None, snapshot: WatchSnapshot) -> int | None:
    if not expiry or len(expiry) != 8:
        return None
    try:
        expiry_date = dt.datetime.strptime(expiry, "%Y%m%d").date()
    except ValueError:
        return None
    return (expiry_date - to_et(snapshot.fetched_at).date()).days


def _apply_exit(arm: str, arm_state: ArmState, action: Action) -> ArmState:
    """Apply a journaled exit to the carried state.

    Arm B's profit take leaves a remainder (flagged so it cannot re-fire); every other
    STOP flattens. A ROLL swaps the symbol — and does NOT reset ``profit_taken``
    semantics because arm C has no profit take; the new contract is the position.
    """
    from executor.position_manager import arm_b_position_after

    if action.kind == "STOP":
        if arm == "B":
            remaining = arm_b_position_after(action, arm_state.position)  # type: ignore[arg-type]
            return replace(arm_state, position=remaining)
        return replace(arm_state, position=None)
    if action.kind == "ROLL":
        position = arm_state.position
        buy_leg = next((leg for leg in action.legs if leg.side == "buy"), None)
        if position is None or buy_leg is None:
            return replace(arm_state, position=None)
        # FIX 2026-10-03 (R1): the replacement's own identity is carried across, not
        # just its symbol. Previously the new position inherited the OLD contract's
        # delta, DTE, expiry and strike — so the tick after a roll re-evaluated the
        # roll trigger against the pre-roll Greeks (delta 0.60, DTE 40), the trigger
        # fired again immediately, and the position would have rolled on every single
        # pass until the chain ran dry. A roll that re-triggers its own trigger is not
        # a roll; the Greeks are the state the trigger reads, so they are part of the
        # state a roll has to update.
        selection = (action.gov_checks or {}).get("selection") or {}
        meta = dict(position.meta)
        if selection:
            meta["roll"] = {
                "from_symbol": position.symbol,
                "from_delta": position.delta,
                "from_dte": (action.gov_checks or {}).get("dte"),
                "to_symbol": buy_leg.symbol,
                "selection": selection,
            }
        return replace(
            arm_state,
            position=replace(
                position,
                symbol=buy_leg.symbol,
                entry_price=buy_leg.limit_price or position.entry_price,
                qty=buy_leg.qty,
                delta=selection.get("delta"),
                strike=selection.get("strike"),
                expiry=(
                    f"{selection['expiry']}"
                    if selection.get("expiry")
                    else position.expiry
                ),
                # Arm C is a long call and never takes profit, so the flag is cleared:
                # leaving it set would make the new leg look like a position already
                # scaled out.
                profit_taken=False,
                meta=meta,
            ),
        )
    return arm_state


def _journal_reverification(
    journal: JournalSink,
    cell: ArmPlan,
    result: ChecklistResult,
    *,
    now: dt.datetime,
    fired: tuple[str, ...],
    snapshot: WatchSnapshot,
) -> int:
    """Journal the live re-verification as a VETO-or-NO_TRADE decision.

    The brief: "human-readable rationale in journal at decision time" for every trigger.
    This is that row — the trigger fired, here is what the checklist said about it *now*.
    It is a NO_TRADE when the checklist still fires (the trade may proceed if the
    governors agree) and a VETO when it does not.
    """
    record = JournalRecord(
        ts=iso_ts(now),
        arm=cell.arm,  # type: ignore[arg-type]
        kind="NO_TRADE" if not result.fire else "PROPOSAL",
        snapshot=result.indicators,
        state={
            "phase": "intraday_reverification",
            "snapshot": snapshot.to_dict(),
            "triggers_fired": list(fired),
            "checklist": result.to_dict(),
        },
        reasoning=(
            f"intraday re-verification of {cell.arm}/{cell.direction} at trigger "
            f"({'; '.join(fired)}): fire={result.fire}"
            + ("" if result.fire else f" — {'; '.join(result.veto_reasons)}")
        ),
        symbol=None,
    )
    # A count, not a row id: ``journaled`` in a TickResult is "how many decisions this
    # tick wrote", and summing store ids would report the store's length instead.
    return 1 if journal.record(record) else 0


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def run_loop(
    *,
    provider: WatchDataProvider,
    rules: Rulebook,
    state: WatchState,
    router: OrderRouter | None = None,
    journal: JournalSink | None = None,
    manager: PositionManager | None = None,
    calendar: EventCalendar | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    stop_at_et: str = DEFAULT_STOP_ET,
    max_ticks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], dt.datetime] | None = None,
    tick_hook: Callable[[TickResult], None] | None = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    iv_rank: float | Mapping[str, float] | None = None,
    flow_gate: Any | None = None,
) -> list[TickResult]:
    """Poll :func:`run_once` until the stop time, ``max_ticks``, or an error.

    ``sleep``, ``clock`` and ``tick_hook`` are injected so a test can run four ticks
    with zero real time and assert on each one. A tick that raises is recorded as a
    note on the *next* tick's result and the loop continues: a transient Alpaca 502 at
    14:55 should not silently end the session, and it must not be swallowed either —
    the errors surface in the returned results and, when a journal is attached, the
    caller can write them.

    ``iv_rank`` and ``flow_gate`` are forwarded to every tick's re-verification. FIX
    (2026-10-03, full-cycle rehearsal): they were accepted by :func:`run_once` and NOT
    forwarded here, so the CLI path re-verified every cell with ``iv_rank=None`` and no
    flow gate — T5 and T6 both evaluate PENDING on a missing read, and both are
    blocking for arm B, so the loop could never enter a position no matter what the
    caller knew. A parameter that is accepted and dropped is worse than one that does
    not exist.
    Returns every tick result, so a whole session's reasoning is inspectable after the
    fact rather than only in the journal.
    """
    clock = clock or (lambda: dt.datetime.now(dt.UTC))
    stop_time = dt.time.fromisoformat(stop_at_et)
    results: list[TickResult] = []
    errors: list[str] = []
    while True:
        now = clock()
        if et_time(now) >= stop_time:
            break
        if max_ticks is not None and len(results) >= max_ticks:
            break
        try:
            result = run_once(
                provider=provider,
                rules=rules,
                state=state,
                now=now,
                router=router,
                journal=journal,
                manager=manager,
                calendar=calendar,
                max_snapshot_age_seconds=max_snapshot_age_seconds,
                iv_rank=iv_rank,
                flow_gate=flow_gate,
            )
        except Exception as exc:  # noqa: BLE001 - a tick must not kill the session
            message = f"tick error at {to_et(now).isoformat(timespec='seconds')}: {exc}"
            errors.append(message)
            results.append(
                TickResult(now=now, snapshot_age_seconds=0.0, stale=True, notes=(message,))
            )
        else:
            if errors:
                result = replace(result, notes=result.notes + tuple(errors))
                errors.clear()
            state = result.next_state or state
            results.append(result)
            if tick_hook is not None:
                tick_hook(result)
        sleep(poll_seconds)
    return results


__all__ = [
    "DEFAULT_MAX_SNAPSHOT_AGE_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_STOP_ET",
    "AlpacaWatchData",
    "DryRunRouter",
    "LiveOrderRouter",
    "MemoryJournalSink",
    "OrderRouter",
    "StaticWatchData",
    "TickResult",
    "WatchDataProvider",
    "WatchLoopError",
    "WatchSnapshot",
    "WatchState",
    "contract_price",
    "reverify_cell",
    "run_loop",
    "run_once",
    "trigger_fired",
]
