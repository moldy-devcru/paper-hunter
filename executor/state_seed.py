"""Session-start state seeding: broker truth for positions, journal for the counter.

The watch loop's ``ArmState`` is per-session. Before this module, every morning
started with amnesia: ``WatchState.initial`` builds empty states, the live CLI
never wrote position rows (``SqliteJournalSink`` records decisions only), and
nothing read open positions back. Under v1's ~0.05-trades-per-window frequency
that never surfaced. Under v2 (V5, ~10 arm-C trades per window, fires clustered
by regime) it is a live defect: arm C could stack duplicate multi-day positions
on consecutive green days, the concurrent-position and bankroll governors would
see an empty state, and arm B's ``max_entries_per_week`` anti-tilt governor would
silently reset every morning.

The fix reads the two sources of truth that already exist:

* **The broker** (``GET /v2/positions``) for what is actually held. Arms are
  structurally disjoint at session open, so classification is deterministic, not
  a guess: SPY shares are arm A (the only equity arm); a 0-1 DTE option is arm B
  (0DTE, hard-closed 15:30, never overnight by design); a >= 30 DTE option is
  arm C (entered 90-180 DTE, rolled before DTE < 45). Anything else — an
  unexpected instrument, a short position, a fractional contract, a DTE in the
  2-29 gap, two positions in one arm — raises :class:`StateSeedError` and the
  session refuses to start. Fail-closed: a state the executor cannot represent
  is a state it must not trade around.

* **The journal** (``decisions`` table, append-only, written on every action)
  for arm B's weekly entry count: ``TRADE`` decisions since Monday 00:00 ET.
  Rolls are ``ROLL`` decisions and do not count as entries, matching
  ``ArmState.with_entry`` semantics.

``entry_ts`` is seeded as ``now``: the broker position payload carries no entry
timestamp. This is harmless for the governors that read it — arm B never
survives the session it entered (0DTE + hard close), and arm C has no
time-based exit (roll triggers read live Greeks; the t1_break streak is
recomputed from daily bars by the EOD pass, not from state).

# INTERPRETATION: seeding is live-mode only. A dry run places no orders and
# reads no broker state, so it keeps the fresh per-session states the rehearsal
# harness threads explicitly; the offline surface stays offline.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

from executor.alpaca_client import parse_occ_symbol
from executor.position_manager import (
    ArmState,
    ManagedPosition,
    et_week_key,
    iso_ts,
    to_et,
)

#: DTE at or below this is arm B (0DTE; 1 tolerates an expiry-date edge).
_B_DTE_MAX = 1
#: DTE at or above this is arm C (entered 90-180, rolled before 45).
_C_DTE_MIN = 30


class StateSeedError(RuntimeError):
    """Broker state the executor cannot represent. The session must not start."""


def _managed_from_broker(p: Any, arm: str, now: dt.datetime) -> ManagedPosition:
    if p.side != "long":
        raise StateSeedError(
            f"{arm}: broker reports a {p.side!r} position in {p.symbol}; "
            "the executor is long-only — refusing to seed"
        )
    if p.avg_entry_price is None:
        raise StateSeedError(
            f"{arm}: broker position {p.symbol} has no avg_entry_price; "
            "the exit ladder cannot mark it — refusing to seed"
        )
    if p.qty != int(p.qty) or p.qty <= 0:
        raise StateSeedError(
            f"{arm}: broker position {p.symbol} has non-positive or fractional "
            f"qty {p.qty}; whole units only — refusing to seed"
        )
    return ManagedPosition(
        arm=arm,  # type: ignore[arg-type]
        symbol=p.symbol,
        qty=int(p.qty),
        entry_price=float(p.avg_entry_price),
        entry_ts=now,
        kind="equity" if arm == "A" else "option",
    )


def seed_arm_states(
    *,
    broker_positions: list[Any],
    rules: Any,
    now: dt.datetime,
    conn: Any = None,
) -> dict[str, ArmState]:
    """Build per-arm states from broker positions + the journal's weekly count.

    Raises :class:`StateSeedError` on anything unclassifiable. ``conn=None``
    skips the journal counter (dry-run / no ledger attached).
    """
    arms: dict[str, ArmState] = {
        a: ArmState(bankroll_usd=getattr(rules.arms, a).bankroll_usd)
        for a in ("A", "B", "C")
    }
    symbol = rules.strategy.symbol
    for p in broker_positions:
        if p.asset_class == "us_equity":
            if p.symbol != symbol:
                raise StateSeedError(
                    f"unexpected equity {p.symbol!r}; the only equity arm holds "
                    f"{symbol} — refusing to seed"
                )
            arm = "A"
            position = _managed_from_broker(p, arm, now)
        elif p.asset_class == "us_option":
            underlying, expiry, strike, right = parse_occ_symbol(p.symbol)
            if underlying != symbol:
                raise StateSeedError(
                    f"option on {underlying!r}, not {symbol} — refusing to seed"
                )
            expiry_date = dt.datetime.strptime(expiry, "%Y%m%d").date()
            dte = (expiry_date - to_et(now).date()).days
            if dte <= _B_DTE_MAX:
                arm = "B"
            elif dte >= _C_DTE_MIN:
                arm = "C"
            else:
                raise StateSeedError(
                    f"{p.symbol}: DTE {dte} is in the 2-29 gap between arm B "
                    "(0DTE) and arm C (>=30 held) — no arm owns it, refusing to seed"
                )
            position = replace(
                _managed_from_broker(p, arm, now),
                right=right,
                expiry=expiry,
                strike=strike,
            )
        else:
            raise StateSeedError(
                f"{p.symbol}: unknown asset_class {p.asset_class!r} — refusing to seed"
            )
        if arms[arm].position is not None:
            raise StateSeedError(
                f"arm {arm}: broker reports more than one open position "
                f"({arms[arm].position.symbol} and {p.symbol}); the governors "
                "model one position per arm — refusing to seed"
            )
        arms[arm] = replace(arms[arm], position=position)

    if conn is not None:
        from journal.store import list_decisions

        week_key = et_week_key(now)
        monday_et = dt.datetime.combine(
            week_key, dt.time.min, tzinfo=to_et(now).tzinfo
        )
        trades = list_decisions(
            conn, arm="B", kind="TRADE", since=iso_ts(monday_et)
        )
        arms["B"] = replace(
            arms["B"], entries_this_week=len(trades), week_key=week_key
        )
    return arms


def seeding_summary(arms: dict[str, ArmState]) -> str:
    """One human line for the session banner."""
    held = [
        f"{a}:{arms[a].position.symbol}x{arms[a].position.qty}"
        for a in ("A", "B", "C")
        if arms[a].position is not None
    ]
    return (
        f"seeded from broker: {', '.join(held) if held else 'flat'}; "
        f"B entries this week: {arms['B'].entries_this_week}"
    )
