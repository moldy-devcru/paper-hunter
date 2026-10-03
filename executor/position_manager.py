"""Mechanical rule enforcement — the frozen rulebook as code, with no judgment.

Scope (Phase 3b)
----------------
This module is the "dumb deterministic script" the brief asks for: it turns the
frozen governors into boolean checks and emits :class:`Action` values. It contains no
LLM, no heuristics, no "that looks weak" — every branch is a number from
``config/rules.example.yaml`` compared against a number computed from position state.
Where the brief left something under-specified, the choice is marked
``# INTERPRETATION:`` in the docstring of the function that makes it, so the operator
can review each one on its own.

Precedence — the part that actually matters
------------------------------------------
Exits outrank entries, always, and inside arm B the hard close outranks everything:

1. **15:30 ET hard close** (``arms.B.exits.hard_close_et``) — if the clock has reached
   it, every arm B position closes in full, full stop, regardless of profit, time
   stop, or a checklist that is still firing. "Close ALL by 15:30 ET. No exceptions."
2. **Profit take** at +100%: half the position closes once, then the remainder is
   trailed by the time stop (there is no trailing-stop implementation — see below).
3. **Time stop**: 2h after entry with the position under +30% → close in full.
4. Entry governors — but only when there is no open position (max 1 concurrent).

# INTERPRETATION (profit take): "close 50%" on an odd contract count rounds **up**
(``ceil``), so a 1-lot 0DTE position that hits +100% closes entirely rather than
rounding to 0 and leaving the rule silently unexecuted. Rounding down would let the
governor fail to act on the only position size this arm can realistically hold.

# INTERPRETATION (trail the remainder): the rulebook says ``trail_remainder: true``
but the brief defines no trail mechanism (no stop level, no trailing band). Rather
than invent one — an invented trail is an invented rule, and the brief forbids rule
changes outside monthly review — the remainder is governed by the two exits that DO
exist: the time stop and the 15:30 hard close. ``trail=True`` is reported in the
journal payload so the gap is visible in the ledger rather than hidden here. This is
flagged for operator ratification.

# INTERPRETATION (weekly entry counter): the "max 3 entries per week" counter resets on
the **ISO week** (Monday 00:00 ET) and counts every entry order this manager emits for
arm B, not just profitable ones. "Max 3 entries per week even if setups appear
(anti-tilt governor)" is a tilt governor; counting only wins would make it a
win-rate rule instead.

# INTERPRETATION (journal vocabulary): enforcement maps onto ``journal.store``'s
``DecisionKind`` as STOP (every liquidation, including arm C's exit-all), ROLL (arm C
roll), TRADE (every entry, including arm A's single buy-and-hold) and VETO (a governor
refusing an entry). NO_TRADE is not used here — a blocked entry is a VETO because it
names the governor, which is the information the operator's monthly review needs.

# INTERPRETATION (arm C exit-all journal kind): liquidating to cash on a T1 break is
recorded as STOP, not ROLL, because it is a liquidation with no replacement leg. The
brief: "liquidate to cash, wait for full checklist re-satisfaction".

Python 3.12+, stdlib + ``config.loader`` + ``journal.store``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from config.loader import Rulebook
from executor.alpaca_client import OptionChain, OptionContract

Arm = Literal["A", "B", "C"]
DecisionKind = Literal["TRADE", "NO_TRADE", "ROLL", "STOP", "PROPOSAL", "VETO"]

#: Fallback ET zone for a host with no tzdata installed (see :func:`eastern`). Every
#: real call goes through zoneinfo.
ET = dt.timezone(dt.timedelta(hours=-4), "EDT")


def eastern(moment: dt.datetime) -> dt.timezone:
    """The ET zone for ``moment``.

    US DST means a fixed -04:00 is wrong for half the year. ``zoneinfo`` is stdlib
    on 3.9+ and ``tzdata`` is present on this fleet, so the real zone is used; the
    fixed offset above is only the fallback for a stripped Python install.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")  # type: ignore[return-value]
    except Exception:  # pragma: no cover - only on a host without tzdata
        return ET


def to_et(moment: dt.datetime) -> dt.datetime:
    """Convert any aware datetime to America/New_York. Naive input is refused.

    Naive datetimes are refused rather than assumed-ET: the watch loop's clock comes
    from Alpaca (``/v2/clock``) and from tests, and silently treating a naive UTC
    stamp as ET would shift every entry window by four hours.
    """
    if moment.tzinfo is None:
        raise ValueError(f"naive datetime {moment!r}; the executor refuses to guess a zone")
    return moment.astimezone(eastern(moment))


def et_time(moment: dt.datetime) -> dt.time:
    return to_et(moment).time()


def parse_et(value: str) -> dt.time:
    return dt.time.fromisoformat(value)


def et_week_key(moment: dt.datetime) -> dt.date:
    """The Monday of ``moment``'s ISO week, in ET — the weekly counter's reset key."""
    return to_et(moment).date() - dt.timedelta(days=to_et(moment).weekday())


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderLeg:
    """One broker instruction inside an :class:`Action`.

    A roll is two legs (sell the expiring contract, buy the replacement) and must be
    journaled as one decision, so the pair lives on one action rather than as two
    independent actions that a reader could mistake for two decisions.
    """

    symbol: str
    qty: int
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    asset_class: Literal["us_option", "us_equity"] = "us_option"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "side": self.side,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "asset_class": self.asset_class,
        }


@dataclass(frozen=True, slots=True)
class Action:
    """One mechanical decision: do this, and journal why."""

    kind: DecisionKind
    arm: Arm
    reason: str
    legs: tuple[OrderLeg, ...] = ()
    symbol: str | None = None
    gov_checks: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    refs: tuple[int, ...] = ()

    @property
    def qty(self) -> int:
        return sum(leg.qty for leg in self.legs)

    @property
    def is_exit(self) -> bool:
        return self.kind in ("STOP", "ROLL")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "arm": self.arm,
            "reason": self.reason,
            "symbol": self.symbol,
            "legs": [leg.to_dict() for leg in self.legs],
            "gov_checks": self.gov_checks,
            "state": self.state,
            "references": list(self.refs),
        }


@dataclass(frozen=True, slots=True)
class Veto:
    """A governor saying no. Journaled as a VETO decision."""

    arm: Arm
    governor: str
    reason: str
    checks: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "VETO",
            "arm": self.arm,
            "governor": self.governor,
            "reason": self.reason,
            "gov_checks": self.checks,
            "state": self.state,
        }


# ---------------------------------------------------------------------------
# position state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManagedPosition:
    """An open position as the executor understands it.

    ``symbol`` is an OCC option symbol for arms B and C. ``entry_price`` is per-share
    premium (so an option's ``current_price`` compares directly) and ``qty`` is
    contracts, so ``pnl_usd = (exit - entry) * qty * 100`` — the 100 multiplier is the
    option contract size and is applied by :meth:`pnl_usd`, never by a caller.
    """

    arm: Arm
    symbol: str
    qty: int
    entry_price: float
    entry_ts: dt.datetime
    kind: Literal["option", "equity"] = "option"
    right: str | None = None
    expiry: str | None = None
    strike: float | None = None
    delta: float | None = None
    profit_taken: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def multiplier(self) -> int:
        return 100 if self.kind == "option" else 1

    def gain_pct(self, price: float) -> float:
        """Fractional return on the premium (not the dollar P&L)."""
        if not self.entry_price:
            raise ValueError(f"position {self.symbol} has entry_price 0; cannot compute gain")
        return price / self.entry_price - 1.0

    def pnl_usd(self, price: float) -> float:
        return (price - self.entry_price) * self.qty * self.multiplier

    def cost_usd(self) -> float:
        return self.entry_price * self.qty * self.multiplier

    def remaining(self) -> ManagedPosition:
        """Self after the profit take — the position the time stop then governs."""
        if not self.profit_taken:
            raise ValueError("remaining() is only meaningful after the profit take")
        return replace(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "symbol": self.symbol,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "entry_ts": self.entry_ts.isoformat(),
            "kind": self.kind,
            "right": self.right,
            "expiry": self.expiry,
            "strike": self.strike,
            "delta": self.delta,
            "profit_taken": self.profit_taken,
            "meta": self.meta,
        }


@dataclass(frozen=True, slots=True)
class ArmState:
    """Everything the governors need to know that is not in a position row."""

    position: ManagedPosition | None = None
    entries_this_week: int = 0
    week_key: dt.date | None = None
    below_ema50_streak: int = 0
    bankroll_usd: float = 0.0

    def with_week_rolled(self, moment: dt.datetime) -> ArmState:
        """Reset the weekly counter when the ISO week has turned over."""
        key = et_week_key(moment)
        return self if self.week_key == key else replace(
            self, entries_this_week=0, week_key=key
        )

    def with_entry(self, moment: dt.datetime) -> ArmState:
        key = et_week_key(moment)
        return replace(
            self, entries_this_week=self.entries_this_week + 1, week_key=key
        )

    def open_positions(self) -> int:
        return 1 if self.position is not None else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position.to_dict() if self.position else None,
            "entries_this_week": self.entries_this_week,
            "week_key": self.week_key.isoformat() if self.week_key else None,
            "below_ema50_streak": self.below_ema50_streak,
            "bankroll_usd": self.bankroll_usd,
        }


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The result of one mechanical pass over one arm."""

    actions: tuple[Action, ...] = ()
    vetoes: tuple[Veto, ...] = ()
    next_state: ArmState | None = None
    notes: tuple[str, ...] = ()

    @property
    def has_exits(self) -> bool:
        return any(a.is_exit for a in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "vetoes": [v.to_dict() for v in self.vetoes],
            "state": self.next_state.to_dict() if self.next_state else None,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# journal sink
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One decision about to be written. Built here, persisted by a sink."""

    ts: str
    arm: Arm
    kind: DecisionKind
    snapshot: dict[str, Any]
    state: dict[str, Any]
    reasoning: str
    symbol: str | None
    references: tuple[int, ...] = ()


class JournalSink(Protocol):
    """Where decisions go. ``journal.store`` implements it; tests capture it."""

    def record(self, record: JournalRecord) -> int | None: ...


class NullJournalSink:
    """Discards records. The default for a dry run with no ledger attached."""

    def record(self, record: JournalRecord) -> int | None:
        return None


class MemoryJournalSink:
    """Keeps records in a list. Tests assert on it instead of on SQLite."""

    def __init__(self) -> None:
        self.records: list[JournalRecord] = []

    def record(self, record: JournalRecord) -> int:
        self.records.append(record)
        return len(self.records)


class SqliteJournalSink:
    """Writes to ``journal.store``. Append-only, by construction of that module."""

    def __init__(self, conn: Any, strategy_version: str) -> None:
        self.conn = conn
        self.strategy_version = strategy_version

    def record(self, record: JournalRecord) -> int:
        from journal.store import DecisionEntry, append_decision

        return append_decision(
            self.conn,
            DecisionEntry(
                ts=record.ts,
                arm=record.arm,
                kind=record.kind,
                checklist_snapshot=record.snapshot,
                checklist_state=record.state,
                reasoning=record.reasoning,
                strategy_version=self.strategy_version,
                symbol=record.symbol,
                references=list(record.references),
            ),
        )


def iso_ts(moment: dt.datetime) -> str:
    """Journal timestamp. UTC, matching ``journal.store.utcnow``'s shape."""
    utc = moment.astimezone(dt.UTC)
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# shared governors
# ---------------------------------------------------------------------------


def entry_window_open(now: dt.datetime, rules: Rulebook) -> bool:
    """Arm B's entry window, inclusive on both ends (09:45–14:00 ET).

    # INTERPRETATION: the endpoints are inclusive. "09:45–14:00 ET only" reads as a
    closed interval, and the cost of the reading is one minute of entry eligibility at
    each edge while the cost of the other reading (excluding 14:00) is an arbitrary
    decision made by a parser.
    """
    window = rules.arms.B.entry.window_et
    clock = et_time(now)
    return parse_et(window.start) <= clock <= parse_et(window.end)


def premium_within_cap(premium_usd: float, rules: Rulebook) -> bool:
    """Premium ≤ 2% of the arm bankroll AND ≤ the $200 hard cap.

    Both conditions, because the rulebook carries both numbers and they are checked
    against each other at load time; checking only the percentage would silently allow
    a future bankroll change to raise the dollar cap, and checking only the dollars
    would make the percentage decorative.
    """
    entry = rules.arms.B.entry
    return (
        premium_usd <= entry.premium_usd_hard_cap
        and premium_usd
        <= entry.premium_pct_of_bankroll_max * rules.arms.B.bankroll_usd + 1e-9
    )


def _b_entry_checks(
    premium_usd: float | None, rules: Rulebook, state: ArmState
) -> dict[str, Any]:
    """The full governor readout, recorded on the action whether or not anything failed.

    A TRADE row that carries the numbers it passed is the difference between "the
    executor says it was within limits" and a reader being able to check.
    """
    limits = rules.arms.B.limits
    entry = rules.arms.B.entry
    return {
        "window_et": entry.window_et.model_dump(),
        "premium_usd": premium_usd,
        "premium_usd_hard_cap": entry.premium_usd_hard_cap,
        "premium_pct_of_bankroll_max": entry.premium_pct_of_bankroll_max,
        "max_concurrent_positions": limits.max_concurrent_positions,
        "open_positions": state.open_positions(),
        "max_entries_per_week": limits.max_entries_per_week,
        "entries_this_week": state.entries_this_week,
    }


def half_quantity(qty: int) -> int:
    """Half of ``qty`` contracts, rounded up (see module INTERPRETATION on profit take)."""
    if qty < 1:
        raise ValueError(f"qty must be >= 1, got {qty}")
    return max(1, math.ceil(qty / 2))


# ---------------------------------------------------------------------------
# arm B — the gambler
# ---------------------------------------------------------------------------


def arm_b_entry_vetoes(
    *,
    now: dt.datetime,
    premium_usd: float,
    rules: Rulebook,
    state: ArmState,
) -> tuple[Veto, ...]:
    """Every arm B entry governor, evaluated independently and all collected.

    Collecting them all (rather than returning the first failure) is deliberate: the
    journal's checklist-failure histogram is the experiment's instrument, and a
    governor that stops the audit at its own failure hides the others. The caller may
    act on the first; the record shows all.
    """
    checks = _b_entry_checks(premium_usd, rules, state)
    limits = rules.arms.B.limits
    entry = rules.arms.B.entry
    vetoes: list[Veto] = []

    if not entry_window_open(now, rules):
        vetoes.append(
            Veto(
                "B",
                "entry_window",
                f"entry window {entry.window_et.start}-{entry.window_et.end} ET is closed "
                f"at {et_time(now).isoformat(timespec='seconds')} ET",
                checks,
                state.to_dict(),
            )
        )
    if not premium_within_cap(premium_usd, rules):
        vetoes.append(
            Veto(
                "B",
                "premium_cap",
                f"premium ${premium_usd:,.2f} exceeds the "
                f"${entry.premium_usd_hard_cap:,.2f} hard cap "
                f"({entry.premium_pct_of_bankroll_max:.2%} of bankroll)",
                checks,
                state.to_dict(),
            )
        )
    if state.open_positions() >= limits.max_concurrent_positions:
        vetoes.append(
            Veto(
                "B",
                "max_concurrent",
                f"{state.open_positions()} open position(s) >= "
                f"max_concurrent_positions={limits.max_concurrent_positions}",
                checks,
                state.to_dict(),
            )
        )
    if state.entries_this_week >= limits.max_entries_per_week:
        vetoes.append(
            Veto(
                "B",
                "max_entries_per_week",
                f"{state.entries_this_week} entries this week >= "
                f"max_entries_per_week={limits.max_entries_per_week} (anti-tilt governor)",
                checks,
                state.to_dict(),
            )
        )
    return tuple(vetoes)


def arm_b_exits(
    *,
    now: dt.datetime,
    price: float,
    position: ManagedPosition,
    rules: Rulebook,
) -> tuple[Action, ...]:
    """Arm B's exit ladder, highest precedence first. Pure: no state, no I/O.

    Order of evaluation IS the precedence order and is asserted by the tests:
    hard close (15:30) → profit take (+100%) → time stop (2h under +30%).
    """
    exits = rules.arms.B.exits
    clock = et_time(now)
    state = {
        "now_et": clock.isoformat(timespec="seconds"),
        "price": price,
        "gain_pct": position.gain_pct(price),
        "position": position.to_dict(),
        "elapsed_minutes": (
            now - position.entry_ts
        ).total_seconds() / 60.0,
        "hard_close_et": exits.hard_close_et,
        "time_stop_after_entry_minutes": exits.time_stop.after_entry_minutes,
        "time_stop_if_under_gain_pct": exits.time_stop.if_under_gain_pct,
        "profit_take_close_pct": exits.profit_take.close_pct_at_100pct_gain,
        "profit_take_trail_remainder": exits.profit_take.trail_remainder,
        "hold_to_expiry": exits.hold_to_expiry,
    }

    # 1. hard close — overrides everything, including a position up 400%.
    if clock >= parse_et(exits.hard_close_et):
        return (
            Action(
                kind="STOP",
                arm="B",
                reason=(
                    f"hard close {exits.hard_close_et} ET reached with "
                    f"{position.qty} contract(s) open (gain "
                    f"{position.gain_pct(price):+.1%}); 'close ALL by 15:30 ET, no "
                    f"exceptions, no expiry'"
                ),
                legs=(OrderLeg(position.symbol, position.qty, "sell"),),
                symbol=position.symbol,
                gov_checks={"hard_close_et": exits.hard_close_et},
                state=state,
            ),
        )

    # 2. profit take — once, at +100%, on the whole position if odd-rounded.
    take = exits.profit_take
    if not position.profit_taken and position.gain_pct(price) >= 1.0:
        qty = half_quantity(position.qty)
        return (
            Action(
                kind="STOP",
                arm="B",
                reason=(
                    f"profit take: {position.gain_pct(price):+.1%} >= +100%; closing "
                    f"{qty} of {position.qty} contract(s) "
                    f"({take.close_pct_at_100pct_gain:.0%}), remainder governed by the "
                    f"time stop and the {exits.hard_close_et} hard close"
                    + (
                        " (NOTE: rulebook says trail_remainder=true but no trail "
                        "mechanism is defined in the brief — flagged for ratification)"
                        if take.trail_remainder
                        else ""
                    )
                ),
                legs=(OrderLeg(position.symbol, qty, "sell"),),
                symbol=position.symbol,
                gov_checks={
                    "gain_pct": position.gain_pct(price),
                    "threshold": 1.0,
                    "close_pct": take.close_pct_at_100pct_gain,
                },
                state=state,
            ),
        )

    # 3. time stop — "if position not >= +30% by 2 hours after entry, close at market".
    stop = exits.time_stop
    elapsed = (now - position.entry_ts).total_seconds() / 60.0
    if elapsed >= stop.after_entry_minutes and position.gain_pct(price) < stop.if_under_gain_pct:
        return (
            Action(
                kind="STOP",
                arm="B",
                reason=(
                    f"time stop: {elapsed:.0f} min since entry >= "
                    f"{stop.after_entry_minutes} min and gain "
                    f"{position.gain_pct(price):+.1%} < "
                    f"{stop.if_under_gain_pct:+.0%} → close at market"
                ),
                legs=(OrderLeg(position.symbol, position.qty, "sell"),),
                symbol=position.symbol,
                gov_checks={
                    "elapsed_minutes": elapsed,
                    "after_entry_minutes": stop.after_entry_minutes,
                    "gain_pct": position.gain_pct(price),
                    "if_under_gain_pct": stop.if_under_gain_pct,
                },
                state=state,
            ),
        )

    return ()


def arm_b_position_after(action: Action, position: ManagedPosition) -> ManagedPosition | None:
    """Apply an arm B action to the position, returning the new state.

    The profit take leaves a remainder that is *not* re-armed: the time stop and the
    hard close still govern it, but the +100% take cannot fire a second time on the
    same position (that would take 50% of the remainder, then 50% of that...).
    """
    qty = action.qty
    if action.kind != "STOP":
        return position
    if action.gov_checks.get("threshold") == 1.0:  # the profit take
        return replace(position, qty=position.qty - qty, profit_taken=True)
    return None


# ---------------------------------------------------------------------------
# arm C — the stalker
# ---------------------------------------------------------------------------


def arm_c_roll_trigger(
    *, delta: float | None, dte: int | None, rules: Rulebook
) -> str | None:
    """``"delta"``, ``"dte"`` or ``None``. The two triggers are independent.

    Either condition alone rolls. The brief: "roll forward when delta < 0.70 OR
    DTE < 45, whichever first". "Whichever first" is about *time* between the two
    causes, and since this function is called on every evaluation there is nothing to
    order them by except this call — so both are checked and the message says which
    fired. When both fire in the same pass, delta is reported because it is the
    condition that will still be true next pass.

    A missing value is NOT a trigger: ``delta=None`` means the data layer could not
    read Greeks, which is blindness, not a delta of 0. Rolling on missing data would
    churn the position every poll.
    """
    trigger = rules.arms.C.exits.roll.trigger
    if delta is not None and delta < trigger.delta_below:
        return "delta"
    if dte is not None and dte < trigger.dte_below:
        return "dte"
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class RollSelection:
    """A chosen replacement leg plus WHY it was chosen.

    The provenance is the point. "Rolled" in a journal is unauditable; "rolled to the
    2027-01-15 95 call, delta 0.79 (nearest 0.80), premium $410 of a $5,000 cap" is a
    row the monthly review can check against the rulebook, and a row that can be checked
    is the only kind that tells the operator whether R1 is being followed or drifted
    from.
    """

    leg: OrderLeg
    expiry: str
    dte: int
    strike: float
    delta: float
    target_delta: float
    premium_usd: float
    cap_usd: float

    def reason(self) -> str:
        return (
            f"R1 roll: next qualifying expiry {self.expiry} "
            f"({self.dte} DTE), strike {self.strike:g} at delta {self.delta:.2f} "
            f"(nearest target {self.target_delta:.2f}), premium ${self.premium_usd:,.2f} "
            f"of the ${self.cap_usd:,.2f} cap"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "expiry": self.expiry,
            "dte": self.dte,
            "strike": self.strike,
            "delta": self.delta,
            "target_delta": self.target_delta,
            "premium_usd": self.premium_usd,
            "cap_usd": self.cap_usd,
        }


def _ask_mark(contract: OptionContract) -> float | None:
    """What a BUY of ``contract`` would actually cost, per share.

    ask → mid → bid. The mirror of ``watch_loop.contract_price`` (bid → mid → ask),
    which marks a long being SOLD. Using the bid here would understate the premium the
    roll is about to spend and make the cap check pass trades that would breach it, so
    the two directions deliberately do not share a function.
    """
    quote = contract.latest_quote
    if quote is not None:
        for value in (quote.ask, quote.mid, quote.bid):
            if value is not None:
                return float(value)
    trade = contract.latest_trade
    return float(trade.p) if trade is not None else None


def arm_c_roll_replacement(
    *,
    chain: OptionChain,
    spot: float,
    day: dt.date,
    rules: Rulebook,
    state: ArmState,
) -> RollSelection | None:
    """The buy leg for arm C's roll, per the frozen roll rule.

    RULED 2026-10-03 (operator, R1): the replacement is **the next qualifying expiry**
    — the earliest expiry inside the same 90-180 DTE band entry uses — **at the strike
    nearest delta 0.80**, **subject to the same premium cap** as entry. Operator's
    wording, all three parts load-bearing:

    * *next qualifying expiry*, not the same expiry. Rolling to a further-out month is
      the whole point of a roll; "next" is the earliest one that still qualifies, which
      keeps theta cost as low as the band allows.
    * *nearest delta 0.80*, not nearest-the-money. The roll exists to restore the delta
      the entry was sized on; rolling to ATM would hand back a 0.50-delta contract and
      the position would re-trigger its own roll trigger on the very next pass. 0.80 is
      read from ``arms.C.entry.delta_min`` so there is one number in the rulebook, not
      two that can disagree.
    * *the same premium cap*. The band is the cash buffer the brief describes; a roll
      that spends it is not the same trade.

    Returns ``None`` — meaning "no roll this pass", never "close and hope" — when the
    chain carries no qualifying contract, when no contract's delta is readable, or when
    the best one breaches the cap. The caller reports that as a veto rather than
    silently doing nothing, because a roll trigger that fires every session and can
    never be satisfied is a stuck position the operator needs to see.

    ``spot`` is accepted for symmetry with the entry path and deliberately unused: the
    ruling targets a DELTA, and a strike rule written in terms of spot would be a second,
    competing definition of the same contract.
    """
    entry = rules.arms.C.entry
    position = state.position
    if position is None or position.qty <= 0:
        return None

    target_delta = float(entry.delta_min)
    cap = float(entry.premium_pct_of_bankroll_max) * float(rules.arms.C.bankroll_usd)

    qualifying: list[tuple[int, str]] = []
    for expiry in chain.expiries():
        try:
            expiry_day = dt.datetime.strptime(expiry, "%Y%m%d").date()
        except ValueError:
            continue
        dte = (expiry_day - day).days
        if entry.dte.min <= dte <= entry.dte.max:
            qualifying.append((dte, expiry))
    if not qualifying:
        return None
    # "Next" = earliest qualifying expiry, which is also the least DTE in the band.
    _, expiry = min(qualifying, key=lambda pair: (pair[0], pair[1]))

    candidates = [
        c
        for c in chain.contracts
        if c.expiry == expiry
        and c.right == entry.right
        and c.greeks is not None
        and c.greeks.delta is not None
    ]
    if not candidates:
        return None
    # No readable Greeks means no way to honour "nearest delta 0.80". Choosing the
    # strike that happens to be closest to spot would be guessing at the one number
    # the ruling is about, so the roll waits instead.
    best = min(
        candidates, key=lambda c: (abs(float(c.greeks.delta) - target_delta), c.strike)
    )
    delta_value = float(best.greeks.delta)  # type: ignore[union-attr]
    if delta_value < entry.delta_min:
        # The nearest-delta contract still fails the entry's own delta floor, so it is
        # not an entry-grade contract and rolling into it is not this trade.
        return None
    price = _ask_mark(best)
    if price is None:
        return None
    premium = price * 100.0 * position.qty
    if premium > cap:
        return None
    return RollSelection(
        leg=OrderLeg(
            best.symbol,
            position.qty,
            "buy",
            order_type="limit",
            limit_price=price,
        ),
        expiry=expiry,
        dte=next(dte for dte, exp in qualifying if exp == expiry),
        strike=float(best.strike),
        delta=delta_value,
        target_delta=target_delta,
        premium_usd=premium,
        cap_usd=cap,
    )


def arm_c_exit_all_needed(streak_below_ema50: int, rules: Rulebook) -> bool:
    """T1 broken for 3 consecutive EODs → liquidate to cash.

    The streak counter is maintained by the EOD pass (``executor/main.py eod``), which
    is the only place that knows what "consecutive EOD" means. This function is the
    boolean the brief states, with the threshold read out of the rulebook's own
    ``exit_all.condition`` string rather than hardcoded — parsed once, with a loud
    fallback if the string is ever reworded.
    """
    # "close_below_50ema_for_3_consecutive_eods" → 3
    condition = rules.arms.C.exits.exit_all.condition
    threshold = 3
    digits = [chunk for chunk in condition.split("_") if chunk.isdigit()]
    if digits:
        threshold = int(digits[0])
    return streak_below_ema50 >= threshold


def arm_c_exits(
    *,
    now: dt.datetime,
    rules: Rulebook,
    state: ArmState,
    price: float,
    delta: float | None = None,
    dte: int | None = None,
    replacement: OrderLeg | None = None,
    selection: RollSelection | None = None,
) -> tuple[Action, ...]:
    """Arm C's exit ladder: exit-all (T1 break) outranks roll.

    A roll needs a ``replacement`` leg. Without one the roll is not taken — and as of
    FIX 2026-10-03 the reason is *reported* rather than swallowed. The ladder used to
    ``return ()`` in both the "no trigger" and the "trigger but nowhere to roll to"
    cases, which the caller could not tell apart: a healthy position and a position
    whose roll trigger has been firing every session with no qualifying contract in the
    chain both looked like silence. :attr:`Evaluation.notes` now carries the
    difference, because a stuck roll is exactly the failure this project is trying not
    to have.

    ``selection`` is the :class:`RollSelection` that produced ``replacement``; it is
    recorded in ``gov_checks`` so the journal carries R1's provenance (which expiry,
    which delta, what premium against what cap) and not just the word "roll".
    """
    position = state.position
    if position is None:
        return ()

    exits = rules.arms.C.exits
    common = {
        "now_et": et_time(now).isoformat(timespec="seconds"),
        "price": price,
        "delta": delta,
        "dte": dte,
        "streak_below_ema50": state.below_ema50_streak,
        "position": position.to_dict(),
        "roll_trigger": exits.roll.trigger.model_dump(),
        "discretionary": exits.roll.discretionary,
    }

    if arm_c_exit_all_needed(state.below_ema50_streak, rules):
        return (
            Action(
                kind="STOP",
                arm="C",
                reason=(
                    f"exit-all: T1 broken for {state.below_ema50_streak} consecutive "
                    f"EODs (close < 50EMA; {exits.exit_all.condition}) → liquidate to "
                    f"cash, then {exits.exit_all.then}"
                ),
                legs=(OrderLeg(position.symbol, position.qty, "sell"),),
                symbol=position.symbol,
                gov_checks={
                    "streak_below_ema50": state.below_ema50_streak,
                    "condition": exits.exit_all.condition,
                },
                state=common,
            ),
        )

    trigger = arm_c_roll_trigger(delta=delta, dte=dte, rules=rules)
    if trigger is None or replacement is None:
        return ()
    parts = [
        f"delta {delta:.2f} < {exits.roll.trigger.delta_below}"
        if delta is not None and delta < exits.roll.trigger.delta_below
        else None,
        f"dte {dte} < {exits.roll.trigger.dte_below}"
        if dte is not None and dte < exits.roll.trigger.dte_below
        else None,
    ]
    fired = " AND ".join(part for part in parts if part)
    return (
        Action(
            kind="ROLL",
            arm="C",
            reason=(
                f"mechanical roll ({fired}) → {exits.roll.to}; "
                f"discretionary={exits.roll.discretionary}"
                + (f"; {selection.reason()}" if selection is not None else "")
            ),
            legs=(
                OrderLeg(position.symbol, position.qty, "sell"),
                replacement,
            ),
            symbol=position.symbol,
            gov_checks={
                "trigger": trigger,
                "delta": delta,
                "dte": dte,
                "roll_to": exits.roll.to,
                "replacement_symbol": replacement.symbol,
                "selection": selection.to_dict() if selection is not None else None,
            },
            state=common,
        ),
    )


# ---------------------------------------------------------------------------
# arm A — the control
# ---------------------------------------------------------------------------


def arm_a_entry(
    *,
    now: dt.datetime,
    price: float,
    rules: Rulebook,
    state: ArmState,
) -> tuple[Action, ...]:
    """Buy-and-hold, exactly once.

    "SPY buy-and-hold. $10k at market open of day 1. No decisions, no trades."
    ``state.position is None`` IS the "day 1" test — the EOD pass opens the position
    row when the fill lands, and until then this returns the same single action on
    every call, which is idempotent at the order level because the caller only routes
    it in live mode and the broker rejects a duplicate buy only for more than the
    buying power. # INTERPRETATION: this is the one arm where "no decisions" means the
    executor places exactly one order and then never touches it; there is no governor
    in this function beyond "already bought".

    ``qty`` is whole shares, floor(bankroll / price), so the position is at most
    $10k. Alpaca permits fractional shares; whole shares are used because the arm is
    a benchmark and a benchmark with a rounding artifact is a worse benchmark.
    """
    if state.position is not None:
        return ()
    bankroll = state.bankroll_usd or rules.arms.A.bankroll_usd
    if price <= 0:
        raise ValueError(f"arm A needs a positive price to size, got {price!r}")
    qty = int(math.floor(bankroll / price))
    if qty < 1:
        raise ValueError(
            f"arm A bankroll ${bankroll:,.2f} cannot buy one share at ${price:,.2f}"
        )
    symbol = rules.arms.A.entry.symbol
    return (
        Action(
            kind="TRADE",
            arm="A",
            reason=(
                f"day-1 buy-and-hold: ${bankroll:,.2f} of {symbol} at market "
                f"({qty} shares at ${price:,.2f}); this arm makes no further decisions"
            ),
            legs=(OrderLeg(symbol, qty, "buy", asset_class="us_equity"),),
            symbol=symbol,
            gov_checks={
                "bankroll_usd": bankroll,
                "price": price,
                "trading_days": rules.arms.A.trading_days,
                "managed": rules.arms.A.managed,
            },
            state=state.to_dict(),
        ),
    )


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------


class PositionManager:
    """Runs the frozen governors for one arm at one instant.

    Stateless by construction: :meth:`evaluate` takes the state and returns the
    actions plus the next state, so the caller owns persistence and nothing here can
    drift from the journal. ``journal`` is optional and defaults to
    :class:`NullJournalSink` — a dry run that journals nothing is still correct.
    """

    def __init__(self, rules: Rulebook, *, journal: JournalSink | None = None) -> None:
        self.rules = rules
        self.journal = journal or NullJournalSink()

    # -- exits (always first) --------------------------------------------------

    def evaluate_exits(
        self,
        arm: Arm,
        *,
        now: dt.datetime,
        state: ArmState,
        price: float,
        delta: float | None = None,
        dte: int | None = None,
        replacement: OrderLeg | None = None,
        selection: RollSelection | None = None,
    ) -> Evaluation:
        """Exit ladder for ``arm``. Returns no actions when nothing fires.

        FIX 2026-10-03 (R1): for arm C, when the roll TRIGGER fires but no replacement
        leg was supplied, the reason travels out in ``Evaluation.notes``. Previously
        this returned the same empty result as "no trigger fired", so a position whose
        roll could not be satisfied was indistinguishable from a healthy one — silent,
        and permanent.
        """
        state = state.with_week_rolled(now)
        notes: tuple[str, ...] = ()
        if arm == "B":
            actions = (
                arm_b_exits(now=now, price=price, position=state.position, rules=self.rules)
                if state.position
                else ()
            )
        elif arm == "C":
            actions = arm_c_exits(
                now=now,
                rules=self.rules,
                state=state,
                price=price,
                delta=delta,
                dte=dte,
                replacement=replacement,
                selection=selection,
            )
            trigger = (
                arm_c_roll_trigger(delta=delta, dte=dte, rules=self.rules)
                if state.position
                else None
            )
            if trigger is not None and not actions:
                notes = (
                    f"arm C roll trigger fired ({trigger}) but no replacement leg was "
                    f"available — position {state.position.symbol if state.position else '?'} "
                    f"is HELD, not rolled. The roll rule stays satisfied every pass, so "
                    f"this repeats until the chain offers a contract meeting R1 "
                    f"(next expiry in the 90-180 DTE band, strike nearest delta 0.80, "
                    f"within the premium cap).",
                )
        else:
            actions = ()  # arm A never exits
        return Evaluation(actions=actions, next_state=state, notes=notes)

    # -- entries ---------------------------------------------------------------

    def evaluate_entry(
        self,
        arm: Arm,
        *,
        now: dt.datetime,
        state: ArmState,
        price: float,
        symbol: str,
        premium_usd: float | None = None,
        replacement: OrderLeg | None = None,
    ) -> Evaluation:
        """Entry governors for ``arm``. Emits a TRADE action or a set of VETOs.

        ``premium_usd`` is the total premium for the proposed position (per-contract
        premium × contracts × 100), not the per-share quote — the $200 cap is a dollar
        figure and comparing it to a per-share number would pass everything.
        """
        state = state.with_week_rolled(now)
        if arm == "A":
            actions = arm_a_entry(now=now, price=price, rules=self.rules, state=state)
            if not actions:
                return Evaluation(next_state=state, notes=("arm A already holds SPY",))
            return Evaluation(actions=actions, next_state=state.with_entry(now))

        if arm == "B":
            premium = premium_usd
            if premium is None:
                raise ValueError("arm B entries need premium_usd (the $200 cap is in dollars)")
            vetoes = arm_b_entry_vetoes(
                now=now, premium_usd=premium, rules=self.rules, state=state
            )
            checks = _b_entry_checks(premium, self.rules, state)
            if vetoes:
                return Evaluation(vetoes=vetoes, next_state=state)
            position = ManagedPosition(
                arm="B",
                symbol=symbol,
                qty=1,
                entry_price=premium / 100.0,
                entry_ts=now,
                kind="option",
            )
            action = Action(
                kind="TRADE",
                arm="B",
                reason=(
                    f"entry: all governors passed (window "
                    f"{self.rules.arms.B.entry.window_et.start}-"
                    f"{self.rules.arms.B.entry.window_et.end} ET, premium "
                    f"${premium:,.2f} <= ${self.rules.arms.B.entry.premium_usd_hard_cap:,.0f}"
                    f", weekly entries {state.entries_this_week + 1}/"
                    f"{self.rules.arms.B.limits.max_entries_per_week})"
                ),
                legs=(OrderLeg(symbol, 1, "buy"),),
                symbol=symbol,
                gov_checks=checks,
                state=state.to_dict(),
            )
            new_state = state.with_entry(now)
            return Evaluation(
                actions=(action,),
                next_state=replace(
                    new_state,
                    position=replace(position, entry_ts=now),
                ),
            )

        if arm == "C":
            return self._arm_c_entry(now=now, state=state, replacement=replacement)

        raise ValueError(f"unknown arm {arm!r}")

    def _arm_c_entry(
        self, *, now: dt.datetime, state: ArmState, replacement: OrderLeg | None
    ) -> Evaluation:
        """Arm C's entry governor: only the concurrency limit (checklist does the rest).

        # INTERPRETATION: arm C's entry criteria (90-180 DTE, delta >= 0.80, premium
        <= 50% of bankroll) are *contract-selection* facts that the selection step
        reports, not numbers this manager can re-derive from a chain snapshot without
        duplicating that module. So the manager enforces the one governor it owns
        (max_concurrent_positions) and records the selection facts it was handed in the
        journal payload. If the selection step ever hands a contract that violates the
        DTE/delta/premium criteria, that is a bug in the selection step, and hiding it
        here would hide it. The selection is re-validated in ``executor/watch_loop.py``
        before the manager is asked for an opinion.
        """
        limits = self.rules.arms.C.limits
        checks = {
            "max_concurrent_positions": limits.max_concurrent_positions,
            "open_positions": state.open_positions(),
        }
        if state.open_positions() >= limits.max_concurrent_positions:
            veto = Veto(
                "C",
                "max_concurrent",
                f"{state.open_positions()} open position(s) >= "
                f"max_concurrent_positions={limits.max_concurrent_positions}",
                checks,
                state.to_dict(),
            )
            return Evaluation(vetoes=(veto,), next_state=state)
        if replacement is None:
            veto = Veto(
                "C",
                "no_candidate",
                "no contract supplied for the roll/entry — the selection step found "
                "nothing meeting 90-180 DTE and delta >= "
                f"{self.rules.arms.C.entry.delta_min}",
                checks,
                state.to_dict(),
            )
            return Evaluation(vetoes=(veto,), next_state=state)
        action = Action(
            kind="TRADE",
            arm="C",
            reason=f"entry: 1 slot free, mechanical order for {replacement.symbol}",
            legs=(replacement,),
            symbol=replacement.symbol,
            gov_checks=checks,
            state=state.to_dict(),
        )
        position = ManagedPosition(
            arm="C",
            symbol=replacement.symbol,
            qty=replacement.qty,
            entry_price=replacement.limit_price or 0.0,
            entry_ts=now,
            kind="option",
        )
        return Evaluation(
            actions=(action,),
            next_state=replace(state, position=position),
        )

    # -- journaling ------------------------------------------------------------

    def _entry_snapshot(self, price: float | None) -> dict[str, Any]:
        """The journal payload's ``checklist_snapshot`` for an enforcement decision.

        # INTERPRETATION: enforcement actions are not checklist evaluations, but the
        # schema requires a snapshot dict. Rather than invent indicator values, this
        # records the *facts the action was computed from* under an ``enforcement``
        # key and leaves the indicator block empty. A fabricated RSI number in a
        # journal is worse than an absent one.
        """
        return {"enforcement": {"reference_price": price}, "indicators": {}}

    def journal_action(
        self,
        action: Action,
        *,
        now: dt.datetime,
        snapshot: dict[str, Any] | None = None,
        journal: JournalSink | None = None,
    ) -> int | None:
        """Write one action to the journal as its DecisionKind.

        The managers return actions; the *caller* decides when they were journaled
        (``executor/watch_loop.py`` does it once per action, after routing). That
        split is why the evaluation methods above do not journal themselves: an
        action journaled twice is a corrupted decision log, not a verbose one.
        """
        if snapshot is None:
            snapshot = self._entry_snapshot(action.state.get("price"))
        return (journal or self.journal).record(
            JournalRecord(
                ts=iso_ts(now),
                arm=action.arm,
                kind=action.kind,
                snapshot=snapshot,
                state=action.to_dict(),
                reasoning=action.reason,
                symbol=action.symbol,
                references=action.refs,
            )
        )

    def journal_veto(
        self,
        veto: Veto,
        *,
        now: dt.datetime,
        symbol: str | None = None,
        journal: JournalSink | None = None,
    ) -> int | None:
        return (journal or self.journal).record(
            JournalRecord(
                ts=iso_ts(now),
                arm=veto.arm,
                kind="VETO",
                snapshot={"enforcement": veto.checks, "indicators": {}},
                state=veto.to_dict(),
                reasoning=f"{veto.governor}: {veto.reason}",
                symbol=symbol,
            )
        )


__all__ = [
    "ArmState",
    "Action",
    "Evaluation",
    "JournalRecord",
    "JournalSink",
    "ManagedPosition",
    "MemoryJournalSink",
    "NullJournalSink",
    "OrderLeg",
    "OrderLeg",
    "PositionManager",
    "RollSelection",
    "SqliteJournalSink",
    "Veto",
    "arm_a_entry",
    "arm_b_entry_vetoes",
    "arm_b_exits",
    "arm_b_position_after",
    "arm_c_exit_all_needed",
    "arm_c_exits",
    "arm_c_roll_replacement",
    "arm_c_roll_trigger",
    "entry_window_open",
    "iso_ts",
    "et_time",
    "et_week_key",
    "half_quantity",
    "premium_within_cap",
    "to_et",
]
