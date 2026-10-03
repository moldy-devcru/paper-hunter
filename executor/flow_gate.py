"""Read T6's flow gate out of the journal.

`flow_baseline` is the only place deep-OTM 0DTE volume history exists, and T6 is the
one checklist condition that reads it. This module is the seam between the two, kept
separate from :mod:`executor.checklist` (which must not know about SQLite) and from
:mod:`executor.soak` (which writes rows, and whose writes are append-only).

**Which session T6 reads is an open operator question, not a settled mechanic.**
`t6_flow.evaluation` is `EOD_only`, so at 10:15 on a Tuesday there is no confirmed flow
row *for* Tuesday — the newest confirmed row is Monday's. Reading Monday's ratio as
Tuesday's gate input is a policy (it makes the gate one session stale, which is exactly
what an EOD-only gate has to mean), and the alternative — refusing to fire until a
same-session EOD row exists, i.e. never firing intraday — is a different strategy. The
default below is the first; the CLI exposes the choice as ``--flow-gate`` rather than
burying it, and no caller is wired to it implicitly.

Nothing here writes.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
from typing import Any, Literal

from executor.checklist import FlowGate
from journal.store import list_flow_baseline

#: Policies ``--flow-gate`` accepts. ``none`` = the pre-existing behaviour (the loop is
#: handed no gate, T6 evaluates PENDING on an unknown read, arm B cannot fire).
POLICIES: tuple[str, ...] = ("none", "last-confirmed")


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.gate.status,
            "ratio": self.gate.ratio,
            "eod_confirmed": self.gate.eod_confirmed,
            "source_date": self.source_date,
            "side": self.side,
            "multiplier": self.multiplier,
            "reason": self.reason,
        }

    def summary_line(self) -> str:
        ratio = "None" if self.gate.ratio is None else f"{self.gate.ratio:.3f}x"
        return (
            f"T6 flow gate ({self.side}): status={self.gate.status} ratio={ratio} "
            f"eod_confirmed={self.gate.eod_confirmed} source={self.source_date or 'none'} "
            f"N={self.multiplier if self.multiplier is not None else 'calibration_pending'} "
            f"— {self.reason}"
        )


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
) -> GateRead:
    """Build T6's :class:`~executor.checklist.FlowGate` from the newest usable row.

    ``include_session=False`` (the default) reads the newest row dated **before**
    ``as_of``: T6 is ``EOD_only``, so the session being traded has no confirmed row yet,
    and a same-session row cannot exist without looking into the future.

    ``multiplier`` is T6's frozen N. ``None`` (the shipped, un-frozen rulebook) produces
    a ``pending`` read whose reason names the calibration, because a gate that cannot
    compare against a threshold has not evaluated anything.
    """
    if side not in ("call", "put"):
        raise FlowGateError(f"side must be 'call' or 'put', got {side!r}")
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
        )
    row = rows[-1]
    field = "ratio_call" if side == "call" else "ratio_put"
    ratio = getattr(row, field, None)
    baseline_days = int(row.baseline_days or 0)
    needed = int(row.baseline_lookback_days or lookback_days)

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
        ),
    )


__all__ = [
    "POLICIES",
    "FlowGateError",
    "GateRead",
    "flow_gate_from_journal",
]
