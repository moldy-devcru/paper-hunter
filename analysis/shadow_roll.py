"""The 4th shadow-sim, costless — prediction #3's comparison arm.

The brief, prediction #3:

    "Arm C tracks A with leverage-amplified variance; the question is whether TA entries
     pick better roll points than a fixed quarterly roll (a 4th shadow-sim, costless,
     can run in the journal as hypothetical)."

So this module maintains a **hypothetical** leg: buy exposure, hold to the next
quarterly expiry, roll, repeat. No order is placed, no premium is paid, no broker is
touched. It exists only as rows in the journal so the comparison survives a bad week.

What it is, precisely
--------------------
The shadow leg is held in **contract units, 1 unit = 100 shares of notional**, and its
P&L is measured on the *underlying* (SPY close to close). That is the whole point of
the comparison, and it is also where the honesty lives:

* It is **not** an option mark. It is "what would the underlying exposure have done".
  Arm C is a deep-ITM long call, so a *delta-weighted* comparison of the two is
  available (`analysis.rollup` labels that `underlying_delta_proxy`); this module stays
  on the underlying entirely and says so on every row (``basis='underlying_notional'``).
* Raw P&L is **not** comparable between the shadow leg and arm C — the two may have
  very different notional deployed. The fair comparison is *return on capital
  deployed*, which :func:`compare_with_arm_c` computes and labels as such.

# INTERPRETATION: what "quarterly roll" means.
The brief says "fixed quarterly roll" without pinning the date. This module uses the
standard equity-index quarterly cycle — the third Friday of March, June, September and
December, matching SPY's monthly expirations (:func:`quarterly_expiry`). The rule is
*mechanical and calendar-based*, never TA-based: that is the whole character of the
comparison arm. A leg is opened on the session after the expiry it just rolled past
(i.e. it holds *through* the expiry it names), so the schedule is a pure function of
the calendar and replaying it a year later gives the same answer.

# INTERPRETATION: lineage, not mutation.
A roll does not close a row; it appends a new leg with ``supersedes_leg_id`` pointing
at the leg it replaced. A leg counts as closed when some later leg supersedes it. The
tables therefore have no UPDATE or DELETE path at all (triggers, like the rest of the
journal), and the chain of rolls is a plain walk of the foreign keys.

Python 3.12+, stdlib + the journal connection.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Any

#: Basis recorded on every mark row. There is exactly one, and it is a constant, so a
#: reader can grep for it.
MARK_BASIS = "underlying_notional"

#: Shares of SPY notional per contract unit. Matches the 100-share option multiplier.
SHARES_PER_UNIT = 100.0

#: (quarter-ending month, quarter index) for the standard equity quarterly cycle.
_QUARTER_MONTHS = {1: 3, 2: 6, 3: 9, 4: 12}

__all__ = [
    "MARK_BASIS",
    "SHARES_PER_UNIT",
    "ShadowLeg",
    "ShadowRollError",
    "compare_with_arm_c",
    "is_quarterly_expiry",
    "leg_pnl",
    "mark_leg",
    "next_quarterly_expiry",
    "open_leg",
    "quarterly_expiry",
    "shadow_roll_status",
]


class ShadowRollError(ValueError):
    """Raised when a shadow-roll row cannot be written honestly."""


# ---------------------------------------------------------------------------
# the calendar
# ---------------------------------------------------------------------------


def quarterly_expiry(year: int, quarter: int) -> dt.date:
    """The third Friday of March / June / September / December of ``year``."""
    if quarter not in _QUARTER_MONTHS:
        raise ShadowRollError(f"quarter must be 1..4, got {quarter}")
    month = _QUARTER_MONTHS[quarter]
    day = dt.date(year, month, 1)
    # weekday(): Monday=0 ... Friday=4. Advance to the first Friday, then +14 days.
    day += dt.timedelta(days=(4 - day.weekday()) % 7)
    return day + dt.timedelta(days=14)


def is_quarterly_expiry(day: dt.date) -> bool:
    """True if ``day`` is one of the four quarterly expiries."""
    return any(quarterly_expiry(day.year, q) == day for q in _QUARTER_MONTHS)


def next_quarterly_expiry(day: dt.date) -> dt.date:
    """The first quarterly expiry strictly after ``day``.

    Strictly after, on purpose: on an expiry date itself the roll is still the leg
    that opened for that expiry, so the next one is the one in the future.
    """
    for year in (day.year, day.year + 1):
        for quarter in (1, 2, 3, 4):
            expiry = quarterly_expiry(year, quarter)
            if expiry > day:
                return expiry
    raise ShadowRollError(f"no quarterly expiry found after {day.isoformat()}")


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _conn(store: Any) -> sqlite3.Connection:
    """Accept a connection, or any object that holds one.

    Mirrors how the rest of the project is injected: callers pass the ``conn`` from
    ``journal.store.init_db``. A store object exposing ``conn``/``connection`` also
    works so an orchestrator can be handed the store rather than its handle.
    """
    if hasattr(store, "execute"):
        return store
    for attr in ("conn", "connection"):
        inner = getattr(store, attr, None)
        if hasattr(inner, "execute"):
            return inner
    raise ShadowRollError(f"cannot get a sqlite connection from {store!r}")


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stamp(ts: str | None) -> str:
    from journal.store import utcnow

    return ts or utcnow()


def open_leg(
    store: Any,
    *,
    opened_on: dt.date | str,
    underlying_close: float,
    qty: float,
    strategy_version: str,
    expiry: dt.date | str | None = None,
    supersedes_leg_id: int | None = None,
    notes: str | None = None,
    ts: str | None = None,
) -> int:
    """Append one shadow leg. Returns its id.

    ``expiry`` defaults to the next quarterly expiry after ``opened_on`` — the fixed
    schedule. A caller may pass an explicit expiry only to replay a leg that is being
    reconstructed from the journal; a *different* expiry than the calendar says is a
    TA-timed roll, which would make the comparison arm a copy of arm C.
    """
    if underlying_close <= 0:
        raise ShadowRollError(f"underlying_close must be positive, got {underlying_close}")
    if qty < 0:
        raise ShadowRollError(f"qty must be >= 0 contract units, got {qty}")
    conn = _conn(store)
    day = _as_date(opened_on)
    exp = _as_date(expiry) if expiry is not None else next_quarterly_expiry(day)
    if exp <= day:
        raise ShadowRollError(
            f"expiry {exp.isoformat()} must be after the open date {day.isoformat()}"
        )
    if supersedes_leg_id is not None:
        prior = conn.execute(
            "SELECT id, expiry FROM shadow_roll_legs WHERE id = ?", (supersedes_leg_id,)
        ).fetchone()
        if prior is None:
            raise ShadowRollError(f"no shadow leg {supersedes_leg_id} to supersede")
    stamp = _stamp(ts)
    cur = conn.execute(
        """
        INSERT INTO shadow_roll_legs (
            ts, opened_on, expiry, underlying_close, qty, strategy_version,
            supersedes_leg_id, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stamp,
            day.isoformat(),
            exp.isoformat(),
            float(underlying_close),
            float(qty),
            strategy_version,
            supersedes_leg_id,
            notes,
            stamp,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def leg_pnl(entry_close: float, close: float, qty: float) -> float:
    """P&L of a leg in dollars: (close - entry) x 100 x contract units."""
    return (float(close) - float(entry_close)) * SHARES_PER_UNIT * float(qty)


def mark_leg(
    store: Any,
    leg_id: int,
    *,
    date: dt.date | str,
    underlying_close: float,
    notes: str | None = None,
    ts: str | None = None,
) -> int:
    """Append a mark for an open leg. Returns the mark id.

    The mark is priced on the **underlying** close — the shadow sim holds the
    underlying exposure, not a contract, and the ``basis`` column says so on every row
    so a later reader cannot mistake it for an option fill.
    """
    conn = _conn(store)
    row = conn.execute(
        "SELECT id, underlying_close, qty FROM shadow_roll_legs WHERE id = ?", (leg_id,)
    ).fetchone()
    if row is None:
        raise ShadowRollError(f"no shadow leg {leg_id}")
    pnl = leg_pnl(row["underlying_close"], underlying_close, row["qty"])
    stamp = _stamp(ts)
    cur = conn.execute(
        """
        INSERT INTO shadow_roll_marks (
            ts, leg_id, date, underlying_close, leg_pnl, basis, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stamp,
            leg_id,
            _as_date(date).isoformat(),
            float(underlying_close),
            pnl,
            MARK_BASIS,
            notes,
            stamp,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShadowLeg:
    """One leg of the shadow roll, as read back from the journal."""

    id: int
    opened_on: str
    expiry: str
    entry_close: float
    qty: float
    supersedes: int | None
    last_close: float | None
    last_mark_date: str | None
    pnl: float
    return_on_notional: float | None
    closed_by: int | None
    realized: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_id": self.id,
            "opened_on": self.opened_on,
            "expiry": self.expiry,
            "entry_close": self.entry_close,
            "qty_contract_units": self.qty,
            "notional_usd": self.qty * SHARES_PER_UNIT * self.entry_close,
            "last_mark_date": self.last_mark_date,
            "last_close": self.last_close,
            "pnl_usd": self.pnl,
            "return_on_notional": self.return_on_notional,
            "closed_by_leg_id": self.closed_by,
            "realized": self.realized,
        }


def _as_date(value: dt.date | str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.datetime.strptime(str(value), "%Y-%m-%d").date()


def _legs(conn: sqlite3.Connection) -> list[ShadowLeg]:
    rows = conn.execute(
        "SELECT * FROM shadow_roll_legs ORDER BY opened_on, id"
    ).fetchall()
    legs: list[ShadowLeg] = []
    for row in rows:
        mark = conn.execute(
            "SELECT date, underlying_close, leg_pnl FROM shadow_roll_marks "
            "WHERE leg_id = ? ORDER BY date DESC, id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        pnl = float(mark["leg_pnl"]) if mark is not None else 0.0
        notional = float(row["qty"]) * SHARES_PER_UNIT * float(row["underlying_close"])
        successor = conn.execute(
            "SELECT id FROM shadow_roll_legs WHERE supersedes_leg_id = ? ORDER BY id LIMIT 1",
            (row["id"],),
        ).fetchone()
        legs.append(
            ShadowLeg(
                id=int(row["id"]),
                opened_on=row["opened_on"],
                expiry=row["expiry"],
                entry_close=float(row["underlying_close"]),
                qty=float(row["qty"]),
                supersedes=row["supersedes_leg_id"],
                last_close=float(mark["underlying_close"]) if mark is not None else None,
                last_mark_date=mark["date"] if mark is not None else None,
                pnl=pnl,
                return_on_notional=(pnl / notional) if notional else None,
                closed_by=int(successor["id"]) if successor is not None else None,
                realized=successor is not None,
            )
        )
    return legs


def shadow_roll_status(store: Any) -> dict[str, Any]:
    """The shadow roll as of the latest mark, plus the per-leg lineage.

    ``sufficient`` is False when no leg has ever been opened — the state the scorecard
    has to read as "not yet scorable" rather than "zero" (prediction #3 needs a
    comparison arm to compare against; without one it simply has none).
    """
    conn = _conn(store)
    legs = _legs(conn)
    total_pnl = sum(leg.pnl for leg in legs)
    deployed = sum(leg.qty * SHARES_PER_UNIT * leg.entry_close for leg in legs)
    return {
        "basis": MARK_BASIS,
        "shares_per_unit": SHARES_PER_UNIT,
        "sufficient": bool(legs),
        "legs": [leg.to_dict() for leg in legs],
        "leg_count": len(legs),
        "realized_legs": sum(1 for leg in legs if leg.realized),
        "open_leg_id": next((leg.id for leg in legs if not leg.realized), None),
        "total_pnl_usd": total_pnl,
        "capital_deployed_usd": deployed,
        "return_on_capital": (total_pnl / deployed) if deployed else None,
        "last_mark_date": max(
            (leg.last_mark_date for leg in legs if leg.last_mark_date), default=None
        ),
        "note": (
            "Underlying-notional hypothetical: no order was placed, no premium paid. "
            "Compare against arm C on return on capital deployed, not on raw P&L — "
            "the two sides may carry very different notional."
        ),
    }


def compare_with_arm_c(store: Any) -> dict[str, Any]:
    """Prediction #3's comparison: TA-timed entries vs the fixed quarterly roll.

    The fair comparison is *return on capital deployed* on both sides. Arm C's
    denominator is its bankroll (``meta.arm_bankroll['C']``) because a deep-ITM call
    position is sized as a fraction of bankroll; the shadow leg's denominator is the
    notional it actually deployed. If the bankroll is missing from meta, arm C's
    return is ``None`` with a stated reason rather than an assumed $10k.
    """
    from journal.store import get_meta

    conn = _conn(store)
    status = shadow_roll_status(conn)
    bankroll = get_meta(conn, "arm_bankroll", None) or {}
    arm_c_bankroll = bankroll.get("C") if isinstance(bankroll, dict) else None

    result: dict[str, Any] = {
        "sufficient": bool(status["sufficient"]),
        "shadow": status,
        "arm_c": {
            "bankroll": arm_c_bankroll,
            "return_on_capital": None,
            "reason": None,
        },
        "comparison": "return_on_capital",
        "ta_entries_beat_fixed_roll": None,
        "delta_return_on_capital": None,
    }
    if not status["sufficient"]:
        result["arm_c"]["reason"] = (
            "no shadow leg has been opened yet — prediction #3 has no comparison arm"
        )
        return result
    if not arm_c_bankroll:
        result["arm_c"]["reason"] = (
            "meta.arm_bankroll has no 'C' entry, so arm C's return on capital cannot "
            "be computed honestly"
        )
    return result


def score_shadow_vs_arm_c(
    store: Any, arm_c_return_on_capital: float | None
) -> dict[str, Any]:
    """:func:`compare_with_arm_c` with arm C's number supplied by the scorecard.

    ``None`` (no arm C bankroll, or no arm C P&L yet) returns the shadow side and an
    explicit "not comparable yet" instead of a comparison that would be arithmetic on
    a missing input.
    """
    base = compare_with_arm_c(store)
    if base["arm_c"]["reason"] is not None:
        # The shadow side exists but arm C's denominator does not (no bankroll in
        # meta). A return-on-capital comparison needs both denominators, so there
        # isn't one — the missing input is named, not defaulted to $10k.
        base["ta_entries_beat_fixed_roll"] = None
        base["delta_return_on_capital"] = None
        return base
    if arm_c_return_on_capital is None:
        base["ta_entries_beat_fixed_roll"] = None
        base["delta_return_on_capital"] = None
        base["arm_c"]["reason"] = (
            "arm C has no closed P&L in the window, so the two returns cannot be compared"
        )
        return base
    base["arm_c"]["return_on_capital"] = arm_c_return_on_capital
    base["arm_c"]["reason"] = None
    shadow_return = base["shadow"]["return_on_capital"]
    if shadow_return is None:
        base["ta_entries_beat_fixed_roll"] = None
        base["delta_return_on_capital"] = None
        base["arm_c"]["reason"] = "shadow leg has zero notional deployed"
        return base
    base["delta_return_on_capital"] = arm_c_return_on_capital - shadow_return
    base["ta_entries_beat_fixed_roll"] = arm_c_return_on_capital > shadow_return
    return base
