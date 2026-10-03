"""Read T6's flow gate out of the journal.

`flow_baseline` is the only place deep-OTM 0DTE volume history exists, and T6 is the
one checklist condition that reads it. This module is the seam between the two, kept
separate from :mod:`executor.checklist` (which must not know about SQLite) and from
:mod:`executor.soak` (which writes rows, and whose writes are append-only).

**Which session T6 reads is now RULED (2026-10-03, operator ruling R3): carry-forward.**
`t6_flow.evaluation` is `EOD_only`, so at 10:15 on a Tuesday there is no confirmed flow
row *for* Tuesday — the newest confirmed row is Monday's. Reading Monday's ratio as
Tuesday's gate input is a policy (it makes the gate one session stale, which is exactly
what an EOD-only gate has to mean), and the operator ratified it on that reasoning. The
alternative, ``next_day_only``, stays selectable: it accepts only the immediately
preceding session's row and leaves T6 PENDING on a gap, so a holiday or a failed soak
stops the gate rather than carrying a two-day-old regime forward. Flipping the default
is a one-line rulebook change plus a strategy-version bump — not a code edit.

**The carry-forward's cost is measured, not asserted.** Every read records
``sessions_stale`` on its reason string and in :meth:`GateRead.to_dict`, because the
caveat attached to the ruling is that a carried ratio describes a *prior-session*
regime and nothing about it describes today's flow. A caveat that cannot be counted is
a caveat nobody can check at the monthly review.

Nothing here writes.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
from typing import Any, Literal

from executor.checklist import FlowGate
from journal.store import list_flow_baseline

#: Policies ``--flow-gate`` accepts.
#:
#: ``none`` — the pre-2026-10-03 behaviour: the loop is handed no gate, T6 evaluates
#:   PENDING on an unknown read, and arm B cannot fire. Kept because it is the honest
#:   "I have no flow data" setting, not because it is a good default.
#: ``carry_forward`` — RULED 2026-10-03 (R3). The newest confirmed row from a session
#:   before this one, however old. The rulebook default; the reason always carries the
#:   age.
#: ``next_day_only`` — the strict alternative: only the immediately preceding session's
#:   row qualifies, and a gap leaves T6 PENDING.
#:
#: ``last-confirmed`` is retained as an alias of ``carry_forward`` because it was the
#: name in the CLI's help text before the ruling named the thing properly; it resolves
#: to the same behaviour so an old command line does not suddenly mean ``none``.
POLICIES: tuple[str, ...] = ("none", "carry_forward", "next_day_only", "last-confirmed")

#: The two real behaviours behind the four accepted names.
POLICY_ALIASES: dict[str, str] = {
    "none": "none",
    "carry_forward": "carry_forward",
    "last-confirmed": "carry_forward",
    "next_day_only": "next_day_only",
}


def normalise_policy(policy: str) -> str:
    """Resolve a CLI name to one of ``none`` / ``carry_forward`` / ``next_day_only``."""
    resolved = POLICY_ALIASES.get(policy)
    if resolved is None:
        raise FlowGateError(
            f"unknown flow-gate policy {policy!r}; accepted: {', '.join(POLICIES)}"
        )
    return resolved


class FlowGateError(ValueError):
    """The gate could not be read honestly."""


@dataclasses.dataclass(frozen=True, slots=True)
class GateRead:
    """The gate plus where it came from — a bare ratio cannot be audited."""

    gate: FlowGate
    source_date: str | None
    side: str
    multiplier: float | None
    reason: str
    #: How many trading sessions between the row this read used and the session being
    #: gated. ``None`` when no row was used. Under ``carry_forward`` this is the
    #: ruling's day-stale cost, made countable: 1 is the normal case, and anything
    #: higher means a soak run or a session was missed.
    sessions_stale: int | None = None
    policy: str = "carry_forward"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.gate.status,
            "ratio": self.gate.ratio,
            "eod_confirmed": self.gate.eod_confirmed,
            "source_date": self.source_date,
            "side": self.side,
            "multiplier": self.multiplier,
            "reason": self.reason,
            "sessions_stale": self.sessions_stale,
            "policy": self.policy,
        }

    def summary_line(self) -> str:
        ratio = "None" if self.gate.ratio is None else f"{self.gate.ratio:.3f}x"
        stale = "n/a" if self.sessions_stale is None else str(self.sessions_stale)
        return (
            f"T6 flow gate ({self.side}): status={self.gate.status} ratio={ratio} "
            f"eod_confirmed={self.gate.eod_confirmed} source={self.source_date or 'none'} "
            f"N={self.multiplier if self.multiplier is not None else 'calibration_pending'} "
            f"policy={self.policy} sessions_stale={stale} — {self.reason}"
        )


def _sessions_between(earlier: str, later: str) -> int:
    """Calendar days between two ISO dates, floored at 1.

    Weekends make this an over-count of *sessions* (a Monday-after-Friday gap is 3
    calendar days, 1 session), which is the safe direction: the number is there to show
    staleness growing, not to be an exact trading-calendar count. The exact count is
    available from the rows themselves if it is ever needed.
    """
    try:
        gap = (dt.date.fromisoformat(later) - dt.date.fromisoformat(earlier)).days
    except ValueError:
        return 1
    return max(gap, 1)


def _as_day(value: dt.date | str) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def flow_gate_from_journal(
    conn: sqlite3.Connection,
    *,
    as_of: dt.date | str,
    side: Literal["call", "put"],
    multiplier: float | None = None,
    lookback_days: int = 20,
    include_session: bool = False,
    policy: str = "carry_forward",
) -> GateRead:
    """Build T6's :class:`~executor.checklist.FlowGate` from the newest usable row.

    ``include_session=False`` (the default) reads the newest row dated **before**
    ``as_of``: T6 is ``EOD_only``, so the session being traded has no confirmed row yet,
    and a same-session row cannot exist without looking into the future.

    ``policy`` is RULED 2026-10-03 (R3) and selects between the two ratified readings of
    "before this session":

    ``carry_forward``
        Take the newest confirmed row however old it is, and record how old. The
        operator's ruling, on the reasoning that a carried ratio is a prior-session
        *regime* read and that refusing to trade is a different strategy from trading
        on stale data.

    ``next_day_only``
        Accept only the immediately preceding session's row. A gap — a holiday, a soak
        run that did not happen — leaves T6 PENDING naming the gap, instead of carrying
        a regime forward across it. This is the stricter reading and it stays available
        because the ruling was a judgement about staleness, not a fact, and the
        monthly review is where that judgement gets tested.

    ``multiplier`` is T6's frozen N. ``None`` (the shipped, un-frozen rulebook) produces
    a ``pending`` read whose reason names the calibration, because a gate that cannot
    compare against a threshold has not evaluated anything.
    """
    if side not in ("call", "put"):
        raise FlowGateError(f"side must be 'call' or 'put', got {side!r}")
    policy = normalise_policy(policy)
    day = _as_day(as_of)
    rows = list_flow_baseline(conn, before=None if include_session else day)
    if not rows:
        return GateRead(
            gate=FlowGate(status="pending", ratio=None, eod_confirmed=False),
            source_date=None,
            side=side,
            multiplier=multiplier,
            reason=(
                "no flow_baseline row on or before "
                f"{day} — the soak has not written a session yet, so there is no "
                f"confirmed flow to gate on"
            ),
            policy=policy,
        )
    row = rows[-1]
    staleness = _sessions_between(row.date, day)

    if policy == "next_day_only":
        # The ruling's strict branch. Decided BEFORE reading the ratio, because a gate
        # that reports a ratio it is about to refuse would put a number in the journal
        # that no decision used.
        if staleness > 1:
            return GateRead(
                gate=FlowGate(status="pending", ratio=None, eod_confirmed=False),
                source_date=row.date,
                side=side,
                multiplier=multiplier,
                reason=(
                    f"policy next_day_only: newest confirmed row {row.date} is "
                    f"{staleness} calendar day(s) before {day}, not the immediately "
                    f"preceding session — T6 left PENDING rather than carrying a "
                    f"stale regime across the gap"
                ),
                sessions_stale=staleness,
                policy=policy,
            )

    field = "ratio_call" if side == "call" else "ratio_put"
    ratio = getattr(row, field, None)
    baseline_days = int(row.baseline_days or 0)
    needed = int(row.baseline_lookback_days or lookback_days)
    stale_note = (
        ""
        if staleness <= 1
        else (
            f" [carry-forward: {staleness} calendar day(s) stale — the rulebook's "
            f"day-stale caveat applies to this read]"
        )
    )

    if ratio is None:
        return GateRead(
            gate=FlowGate(status="pending", ratio=None, eod_confirmed=False),
            source_date=row.date,
            side=side,
            multiplier=multiplier,
            reason=(
                f"newest row {row.date} has no {field} — it was written before a "
                f"baseline existed"
            ),
            sessions_stale=staleness,
            policy=policy,
        )
    if baseline_days < needed:
        return GateRead(
            gate=FlowGate(status="pending", ratio=None, eod_confirmed=False),
            source_date=row.date,
            side=side,
            multiplier=multiplier,
            reason=(
                f"newest row {row.date} computes its ratio over a {baseline_days}-session "
                f"baseline, not {needed} — not the ratified 20-session ratio"
            ),
            sessions_stale=staleness,
            policy=policy,
        )
    if multiplier is None:
        return GateRead(
            gate=FlowGate(status="pending", ratio=float(ratio), eod_confirmed=True),
            source_date=row.date,
            side=side,
            multiplier=None,
            reason=(
                "t6_flow.multiplier is calibration_pending (N not frozen) — the ratio is "
                "readable but there is no threshold to compare it against"
            ),
            sessions_stale=staleness,
            policy=policy,
        )
    value = float(ratio)
    passed = value >= float(multiplier)
    return GateRead(
        gate=FlowGate(
            status="pass" if passed else "fail",
            ratio=value,
            eod_confirmed=True,
        ),
        source_date=row.date,
        side=side,
        multiplier=float(multiplier),
        reason=(
            f"newest confirmed session {row.date}, {needed}-session baseline, "
            f"{'passes' if passed else 'fails'} N={float(multiplier):.3f}"
            f"{stale_note}"
        ),
        sessions_stale=staleness,
        policy=policy,
    )


__all__ = [
    "POLICIES",
    "POLICY_ALIASES",
    "FlowGateError",
    "GateRead",
    "flow_gate_from_journal",
    "normalise_policy",
]
