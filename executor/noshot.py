"""NO-SHOT log — "the hunting part" (brief: "The NO-SHOT log").

Every session where the sights were on something and no shot was taken gets a row:
which conditions failed, the indicator values at the time, the contract that *would*
have been bought, and — filled in later, once the hypothetical's window has closed —
what that rejected trade would have done.

Sights on: the precise definition
---------------------------------
The brief is prose ("every day the sights were on something and we didn't shoot"), so
this module pins it down, and pins it down **narrowly**, because a NO-SHOT log that
catches every quiet day measures nothing. A sighting is recorded when:

1. **no position was taken** for that arm/direction today (the caller's knowledge —
   Phase 4a places no orders, so by default nothing is taken), **and**
2. the checklist either
   * **fired** and no position was taken anyway (the trigger never came, or the
     intraday re-verification killed it), **or**
   * did not fire with **exactly one** blocking FAIL (default ``marginal_limit=1``) —
     "at least one condition was the only blocker" — and no event-day hard veto.

Two exclusions, both deliberate:

* **Event days are not sightings.** T5's event veto is a calendar fact that removes the
  whole session; recording a NO-SHOT for it would fill the counterfactual ledger with
  rows whose "conditions failed" is a single veto that applied to everything and whose
  rejected trade was never seriously on the table. The veto is recorded in the hunt
  plan's journal row instead, which is where a reader looks for it.
* **PENDING does not make a sighting.** A PENDING gate means the system was *blind*,
  not that it declined. T6 is PENDING for arm B in every run until its multiplier is
  calibrated before first trade, so counting PENDING as a sighting would make the log
  all-PENDING and all-hollow. PENDING conditions are still recorded on the row (they
  are honest evidence that the entry could not have happened anyway).

``would_fire_if_failed_cleared`` states the thing a reader actually wants: even with
the failed condition(s) magically satisfied, would the checklist have fired? No, if a
PENDING gate remains. That distinction is the difference between "we nearly shot" and
"we could not have shot".

Counterfactuals: filled later, by an approximation that says so
--------------------------------------------------------------
``counterfactual_outcome`` stays NULL on the day of the sighting — writing it up front
would be inventing the one number that matters. :func:`fill_counterfactuals` computes
it later from outcome bars, when enough of the hypothetical's window has closed:

* **Arm B** is 0DTE and exits same day (hard close 15:30), so its window closes with
  the session. The outcome recorded is what the **underlying** did in that session
  (high/low/close and the move toward/away from the projection), explicitly labelled
  ``basis="underlying_session"``. The honest option-level P&L is not computable here:
  it needs the hypothetical fill time and premium path, which belong to the intraday
  watch loop (Phase 4b), so this module records the underlying move and says the
  option outcome is not modelled rather than multiplying a delta by a guess.
* **Arm C** holds 90-180 DTE with a mechanical roll, so the "pass-through window"
  used here is *from the sighting close to the evaluation date*. The basis is
  ``"underlying_delta_proxy"``: delta-weighted underlying move less the premium paid
  at the hypothetical entry. That is an approximation of an option's payoff, not a
  mark — delta alone ignores the second-order term that makes deep-ITM long calls
  behave like the underlying plus time value. It is labelled as a proxy everywhere it
  appears so no reviewer can mistake it for a fill.

Python 3.12+, stdlib + the hunt plan / journal layers.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from config.loader import Rulebook
from executor.alpaca_client import Bar, BarSeries, OptionChain
from executor.hunt_plan import ArmPlan, HuntPlan, RangeProjection, project_setup_day_range

#: How many blocking FAILs still count as "the sights were on".
DEFAULT_MARGINAL_LIMIT = 1


class NoshotError(ValueError):
    """Raised when a NO-SHOT row cannot be built honestly from what was supplied."""


# ---------------------------------------------------------------------------
# sighting assessment
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SightsAssessment:
    """Why (or why not) this (arm, direction) counts as a sighting."""

    arm: str
    direction: str
    on: bool
    reason: str
    failed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    checklist_fired: bool = False
    position_taken: bool = False
    would_fire_if_failed_cleared: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "direction": self.direction,
            "sights_on": self.on,
            "reason": self.reason,
            "failed_conditions": list(self.failed),
            "pending_conditions": list(self.pending),
            "checklist_fired": self.checklist_fired,
            "position_taken": self.position_taken,
            "would_fire_if_failed_cleared": self.would_fire_if_failed_cleared,
        }


def assess_sights(
    cell: ArmPlan,
    *,
    position_taken: bool = False,
    event_day_veto: bool = False,
    marginal_limit: int = DEFAULT_MARGINAL_LIMIT,
) -> SightsAssessment:
    """The NO-SHOT eligibility rule, in one readable function."""
    if marginal_limit < 1:
        raise NoshotError(f"marginal_limit must be >= 1, got {marginal_limit}")

    failed = cell.failed()
    pending = cell.pending()
    would_fire = cell.fire or not pending

    if position_taken:
        return SightsAssessment(
            cell.arm, cell.direction, False,
            "a position was taken — that is a trade, not a no-shot",
            failed, pending, cell.fire, True, would_fire,
        )

    if cell.fire:
        return SightsAssessment(
            cell.arm, cell.direction, True,
            "checklist fired but no position was taken (trigger never fired, or the "
            "intraday re-verification rejected it): sights on, shot not taken",
            failed, pending, True, False, would_fire,
        )

    if event_day_veto:
        return SightsAssessment(
            cell.arm, cell.direction, False,
            "event-day hard veto removed the whole session — no sighting was taken, so "
            "there is nothing to record as a near-miss",
            failed, pending, False, False, would_fire,
        )

    if not failed:
        return SightsAssessment(
            cell.arm, cell.direction, False,
            "no condition FAILED — the checklist was blocked only by PENDING "
            f"conditions ({', '.join(pending) or 'none'}): the system was blind, not "
            "declining, so this is not a sighting",
            failed, pending, False, False, would_fire,
        )

    if len(failed) > marginal_limit:
        return SightsAssessment(
            cell.arm, cell.direction, False,
            f"{len(failed)} blocking conditions failed (>{marginal_limit}): "
            f"{', '.join(failed)} — not a near miss",
            failed, pending, False, False, would_fire,
        )

    if len(failed) == 1:
        reason = (
            f"one condition away: {failed[0]} was the only blocker"
            + (
                f" (plus uncalibrated/undata'd PENDING: {', '.join(pending)})"
                if pending
                else ""
            )
        )
    else:
        reason = (
            f"{len(failed)} blocking conditions failed ({', '.join(failed)}), within "
            f"the marginal limit of {marginal_limit}"
        )
    return SightsAssessment(
        cell.arm, cell.direction, True, reason, failed, pending, False, False, would_fire
    )


# ---------------------------------------------------------------------------
# instrument hypothesis
# ---------------------------------------------------------------------------


def _premium(contract: Any) -> tuple[float | None, str]:
    """Premium and where it came from. Ask first (what we would pay), then mid, then last."""
    quote = contract.latest_quote
    if quote is not None and quote.ask is not None:
        return quote.ask, "latest_quote.ask"
    mid = quote.mid() if quote is not None else None
    if mid is not None:
        return mid, "latest_quote.mid"
    trade = contract.latest_trade
    if trade is not None and trade.p is not None:
        return trade.p, "latest_trade.p"
    return None, "unavailable (no ask, mid or last trade on the contract)"


def arm_b_hypothesis(
    *,
    day: dt.date,
    direction: str,
    setup_bar: Bar,
    chain: OptionChain | None,
    rules: Rulebook,
    projection_factor: float = 1.0,
) -> dict[str, Any]:
    """The 0DTE contract arm B's rules would have bought, plus the premium-cap check.

    Strike rule (brief): "first OTM strike beyond the setup-day range projection", with
    the projection computed by :func:`executor.hunt_plan.project_setup_day_range` —
    one setup-day range extended beyond the setup day's extreme (see that function's
    INTERPRETATION for why the factor is a parameter).
    """
    arm = rules.arms.B
    entry = arm.entry
    exits = arm.exits
    cap = min(
        entry.premium_pct_of_bankroll_max * arm.bankroll_usd,
        entry.premium_usd_hard_cap,
    )
    right = direction
    expiry = day.strftime("%Y%m%d")
    projection: RangeProjection = project_setup_day_range(
        setup_bar, direction, factor=projection_factor
    )

    payload: dict[str, Any] = {
        "arm": "B",
        "direction": direction,
        "instrument": "option",
        "right": right,
        "expiry": expiry,
        "dte": 0,
        "strike_rule": entry.strike_rule,
        "strike_projection": projection.to_dict(),
        "premium_cap_usd": cap,
        "premium_cap_source": (
            f"min(premium_pct_of_bankroll_max {entry.premium_pct_of_bankroll_max} x "
            f"${arm.bankroll_usd:,.0f}, premium_usd_hard_cap ${entry.premium_usd_hard_cap})"
        ),
        "exits": {
            "time_stop_minutes": exits.time_stop.after_entry_minutes,
            "time_stop_gain_pct": exits.time_stop.if_under_gain_pct,
            "time_stop_action": exits.time_stop.action,
            "profit_take_close_pct_at_100pct_gain": exits.profit_take.close_pct_at_100pct_gain,
            "trail_remainder": exits.profit_take.trail_remainder,
            "hard_close_et": exits.hard_close_et,
            "hold_to_expiry": exits.hold_to_expiry,
        },
    }

    if chain is None:
        payload.update(
            strike=None,
            premium=None,
            premium_within_cap=False,
            max_contracts=0,
            unavailable_reason=(
                "no options chain supplied — the strike rule cannot be applied without a "
                "0DTE chain, so the contract is left unnamed rather than invented"
            ),
        )
        return payload

    same_day = [c for c in chain.contracts if c.expiry == expiry and c.right == right]
    if not same_day:
        payload.update(
            strike=None,
            premium=None,
            premium_within_cap=False,
            max_contracts=0,
            unavailable_reason=(
                f"chain has no {right} contracts expiring {expiry} (0DTE) — SPY 0DTE only "
                f"exists on the session's own expiry, and this chain does not carry it"
            ),
        )
        return payload

    strikes = sorted({c.strike for c in same_day})
    if direction == "call":
        beyond = [k for k in strikes if k > projection.level]
    else:
        beyond = [k for k in strikes if k < projection.level]
    if not beyond:
        side = "above" if direction == "call" else "below"
        payload.update(
            strike=None,
            premium=None,
            premium_within_cap=False,
            max_contracts=0,
            unavailable_reason=(
                f"no {right} strike sits {side} the projected level {projection.level:.4f} "
                f"(chain strikes {strikes[0]:g}-{strikes[-1]:g}) — the hypothetical trade "
                f"would not have existed at this projection"
            ),
        )
        return payload

    strike = beyond[0] if direction == "call" else beyond[-1]
    contract = next(c for c in same_day if c.strike == strike)
    premium, source = _premium(contract)
    cost = None if premium is None else premium * 100.0
    within = False if cost is None else cost <= cap
    qty = 0 if cost in (None, 0) else int(math.floor(cap / cost))

    payload.update(
        strike=strike,
        contract_symbol=contract.symbol,
        premium=premium,
        premium_source=source,
        premium_per_contract_usd=cost,
        premium_within_cap=within,
        premium_cap_check=(
            "within cap" if within
            else (
                f"premium ${cost:,.2f} per contract exceeds the ${cap:,.2f} cap — the arm "
                f"would NOT have bought this contract even with a perfect setup"
                if cost is not None
                else "no premium available on the selected contract"
            )
        ),
        max_contracts=qty if within else 0,
        unavailable_reason=None,
    )
    return payload


def arm_c_hypothesis(
    *,
    day: dt.date,
    chain: OptionChain | None,
    rules: Rulebook,
) -> dict[str, Any]:
    """The deep-ITM call arm C's rules would have bought.

    Selection (brief): "90-180 DTE, delta >= 0.80 at entry". Among the contracts
    meeting that, the **cheapest** is taken — the strike rule is a constraint, not a
    preference, and buying the cheapest contract that satisfies it is the only choice
    the brief does not already make.

    # INTERPRETATION: DTE is computed against the **session date**, never against
    ``datetime.now()``. ``OptionContract.dte`` uses wall-clock now, which would make a
    backfilled or replayed row drift by however long the replay happened to be run —
    and a counterfactual ledger that quietly re-ages is worse than no ledger.
    """
    arm = rules.arms.C
    entry = arm.entry
    cap = entry.premium_pct_of_bankroll_max * arm.bankroll_usd

    payload: dict[str, Any] = {
        "arm": "C",
        "direction": "call",
        "instrument": "option",
        "right": entry.right,
        "dte_min": entry.dte.min,
        "dte_max": entry.dte.max,
        "delta_min": entry.delta_min,
        "premium_pct_of_bankroll_max": entry.premium_pct_of_bankroll_max,
        "premium_cap_usd": cap,
        "roll": {
            "delta_below": arm.exits.roll.trigger.delta_below,
            "dte_below": arm.exits.roll.trigger.dte_below,
            "rule": arm.exits.roll.trigger.rule,
            "to": arm.exits.roll.to,
            "discretionary": arm.exits.roll.discretionary,
        },
        "exit_all": {
            "trigger": arm.exits.exit_all.trigger,
            "condition": arm.exits.exit_all.condition,
            "action": arm.exits.exit_all.action,
            "then": arm.exits.exit_all.then,
        },
    }

    if chain is None:
        payload.update(
            contract=None, premium=None, delta=None, dte=None,
            premium_within_cap=False, max_contracts=0,
            unavailable_reason=(
                "no options chain supplied — a 90-180 DTE deep-ITM contract cannot be "
                "named without one, so it is left unnamed rather than invented"
            ),
        )
        return payload

    considered = 0
    rejected_dte = 0
    rejected_delta = 0
    qualifying: list[tuple[float, Any]] = []
    for contract in chain.contracts:
        if contract.right != entry.right:
            continue
        considered += 1
        dte = _dte(contract.expiry, day)
        if dte is None or not (entry.dte.min <= dte <= entry.dte.max):
            rejected_dte += 1
            continue
        delta = contract.greeks.delta
        if delta is None or delta < entry.delta_min:
            rejected_delta += 1
            continue
        premium, _source = _premium(contract)
        qualifying.append((premium if premium is not None else math.inf, contract))

    payload["selection_counts"] = {
        "candidates_of_right": considered,
        "rejected_outside_dte_window": rejected_dte,
        "rejected_below_delta": rejected_delta,
        "qualifying": len(qualifying),
    }

    if not qualifying:
        payload.update(
            contract=None, premium=None, delta=None, dte=None,
            premium_within_cap=False, max_contracts=0,
            unavailable_reason=(
                f"no {entry.right} contract in the {entry.dte.min}-{entry.dte.max} DTE "
                f"window reaches delta {entry.delta_min} in this chain "
                f"(candidates {considered}, outside DTE window {rejected_dte}, below "
                f"delta {rejected_delta}) — the hypothetical arm-C trade would not have "
                f"existed today"
            ),
        )
        return payload

    qualifying.sort(key=lambda pair: (pair[0], pair[1].strike))
    _premium_price, contract = qualifying[0]
    premium, source = _premium(contract)
    delta = contract.greeks.delta
    dte = _dte(contract.expiry, day)
    cost = None if premium is None else premium * 100.0
    within = False if cost is None else cost <= cap
    qty = 0 if cost in (None, 0) else int(math.floor(cap / cost))

    payload.update(
        contract=contract.symbol,
        expiry=contract.expiry,
        dte=dte,
        strike=contract.strike,
        delta=delta,
        premium=premium,
        premium_source=source,
        premium_per_contract_usd=cost,
        premium_within_cap=within,
        premium_cap_check=(
            "within cap" if within
            else (
                f"premium ${cost:,.2f} per contract exceeds the ${cap:,.2f} cap "
                f"({entry.premium_pct_of_bankroll_max} of ${arm.bankroll_usd:,.0f})"
                if cost is not None
                else "no premium available on the selected contract"
            )
        ),
        max_contracts=qty if within else 0,
        selection_rule=(
            f"cheapest {entry.right} with {entry.dte.min}-{entry.dte.max} DTE and "
            f"delta >= {entry.delta_min}"
        ),
        unavailable_reason=None,
    )
    return payload


# ---------------------------------------------------------------------------
# drafts + writing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoshotDraft:
    """A sighting, ready to be appended. ``counterfactual_outcome`` is absent by design."""

    arm: str
    direction: str
    date: dt.date
    session: dict[str, Any]
    assessment: SightsAssessment
    instrument_hypothesis: dict[str, Any]
    failed_conditions: dict[str, Any]
    indicator_values: dict[str, Any]
    reasoning: str
    strategy_version: str
    symbol: str
    ts: str
    decision_ref: int | None = None

    def to_entry(self) -> Any:
        from journal.store import NoshotEntry

        return NoshotEntry(
            ts=self.ts,
            date=self.date.strftime("%Y-%m-%d"),
            instrument_hypothesis=self.instrument_hypothesis,
            failed_conditions=self.failed_conditions,
            indicator_values=self.indicator_values,
            counterfactual_entry_ref=self.decision_ref,
            counterfactual_outcome=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "direction": self.direction,
            "date": self.date.strftime("%Y-%m-%d"),
            "ts": self.ts,
            "session": self.session,
            "assessment": self.assessment.to_dict(),
            "instrument_hypothesis": self.instrument_hypothesis,
            "failed_conditions": self.failed_conditions,
            "reasoning": self.reasoning,
            "decision_ref": self.decision_ref,
        }


def _session_summary(setup_bar: Bar | None, day: dt.date) -> dict[str, Any]:
    if setup_bar is None:
        return {"date": day.isoformat(), "available": False}
    return {
        "date": day.isoformat(),
        "available": True,
        "bar_time": setup_bar.t.isoformat().replace("+00:00", "Z"),
        "open": setup_bar.o,
        "high": setup_bar.h,
        "low": setup_bar.l,
        "close": setup_bar.c,
        "volume": setup_bar.v,
        "range": setup_bar.h - setup_bar.l,
    }


def _failed_conditions(cell: ArmPlan) -> dict[str, Any]:
    """condition -> why it failed (or is pending, honestly separated)."""
    out: dict[str, Any] = {}
    for cid, cond in cell.checklist.conditions.items():
        if not cond.blocking or cond.status == "PASS":
            continue
        out[cid] = {"status": cond.status, "blocking": True, "detail": cond.detail}
    return out


def build_noshots(
    plan: HuntPlan,
    *,
    setup_bar: Bar | None,
    chain: OptionChain | None,
    rules: Rulebook,
    taken: Sequence[tuple[str, str]] = (),
    marginal_limit: int = DEFAULT_MARGINAL_LIMIT,
    projection_factor: float = 1.0,
    ts: str | None = None,
    decision_ids: dict[tuple[str, str], int] | None = None,
) -> list[NoshotDraft]:
    """EOD pass: drafts for every sighting in ``plan``.

    ``setup_bar`` is the session's own daily bar (the bar that becomes available at the
    close). Arm B's strike projection needs the real setup-day range, which does not
    exist at plan time — so the plan's projection is marked provisional and this pass
    recomputes it from the actual day.

    ``taken`` is the set of ``(arm, direction)`` keys that ended up with a position.
    """
    session = _session_summary(setup_bar, plan.day)
    taken_set = set(taken)
    stamp = ts or dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    drafts: list[NoshotDraft] = []

    for cell in plan.arms:
        assessment = assess_sights(
            cell,
            position_taken=cell.key in taken_set,
            event_day_veto=plan.event_day_veto,
            marginal_limit=marginal_limit,
        )
        if not assessment.on:
            continue

        if setup_bar is None and cell.arm == "B":
            hypothesis: dict[str, Any] = {
                "arm": "B",
                "direction": cell.direction,
                "instrument": "option",
                "unavailable_reason": (
                    "no session bar supplied — arm B's strike rule is defined on the "
                    "setup-day range, and that range is unknown without the day"
                ),
            }
        elif cell.arm == "B":
            hypothesis = arm_b_hypothesis(
                day=plan.day,
                direction=cell.direction,
                setup_bar=setup_bar,  # type: ignore[arg-type]
                chain=chain,
                rules=rules,
                projection_factor=projection_factor,
            )
        else:
            hypothesis = arm_c_hypothesis(day=plan.day, chain=chain, rules=rules)

        failed = _failed_conditions(cell)
        reasoning = _noshot_reasoning(cell, assessment, hypothesis, plan.symbol)

        drafts.append(
            NoshotDraft(
                arm=cell.arm,
                direction=cell.direction,
                date=plan.day,
                session=session,
                assessment=assessment,
                instrument_hypothesis=hypothesis,
                failed_conditions=failed,
                indicator_values={
                    "plan_snapshot": cell.snapshot_dict,
                    "plan_checklist_state": cell.to_state(),
                    "session_bar": session,
                    "triggers": [t.to_dict() for t in cell.triggers],
                },
                reasoning=reasoning,
                strategy_version=plan.strategy_version,
                symbol=plan.symbol,
                ts=stamp,
                decision_ref=(decision_ids or {}).get(cell.key),
            )
        )
    return drafts


def _noshot_reasoning(
    cell: ArmPlan,
    assessment: SightsAssessment,
    hypothesis: dict[str, Any],
    symbol: str,
) -> str:
    parts = [
        f"{cell.arm}/{cell.direction} on {symbol}: NO SHOT. {assessment.reason}."
    ]
    if assessment.failed:
        parts.append(
            "Failed: "
            + "; ".join(
                f"{cid} — {cell.checklist.conditions[cid].detail}"
                for cid in assessment.failed
            )
            + "."
        )
    if assessment.pending:
        parts.append(
            "Pending (would have blocked anyway): "
            + ", ".join(assessment.pending)
            + "."
        )
    named = (
        hypothesis.get("contract_symbol")
        or hypothesis.get("contract")
        or hypothesis.get("strike")
    )
    if named:
        parts.append(
            f"Hypothetical instrument: {named}"
            + (
                f" @ ${hypothesis['premium']:.2f} premium"
                if hypothesis.get("premium") is not None
                else ""
            )
            + (
                " (premium over cap — would not have been bought)."
                if hypothesis.get("premium_within_cap") is False
                else "."
            )
        )
    else:
        parts.append(
            "Hypothetical instrument: "
            f"{hypothesis.get('unavailable_reason') or 'not nameable from the inputs'}."
        )
    parts.append("Counterfactual outcome left NULL at write time; filled later.")
    return " ".join(parts)


def write_noshots(
    conn: Any, drafts: Sequence[NoshotDraft]
) -> list[int]:
    """Append the drafts. Inserts only — the one mutable column is never touched here."""
    from journal.store import append_noshot

    return [append_noshot(conn, draft.to_entry()) for draft in drafts]


# ---------------------------------------------------------------------------
# counterfactual fill (a later pass)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Counterfactual:
    """The computed outcome for one hypothetical, or a reason it is not computable yet."""

    noshot_id: int
    arm: str
    date: str
    outcome: dict[str, Any] | None
    reason: str

    @property
    def computed(self) -> bool:
        return self.outcome is not None


def _bar_on_or_before(bars: BarSeries, day: dt.date) -> Bar | None:
    for bar in reversed(bars.bars):
        if bar.t.date() <= day:
            return bar
    return None


def _bars_after(bars: BarSeries, day: dt.date) -> list[Bar]:
    return [b for b in bars.bars if b.t.date() > day]


def _dte(contract_expiry: str | None, day: dt.date) -> int | None:
    """Calendar days from ``day`` to the contract's expiry.

    Deliberately not ``OptionContract.dte``, which measures from wall-clock now: a
    replayed NO-SHOT row would silently re-age every time it was recomputed.
    """
    if not contract_expiry:
        return None
    try:
        return (dt.datetime.strptime(contract_expiry, "%Y%m%d").date() - day).days
    except ValueError:
        return None


def fill_counterfactuals(
    conn: Any,
    dates: Sequence[dt.date | str] | None = None,
    *,
    bars: BarSeries | None = None,
    write: bool = True,
) -> list[Counterfactual]:
    """Fill ``counterfactual_outcome`` for sightings whose window has closed.

    ``dates`` are the session dates being evaluated; the **latest** of them is the
    evaluation date for any still-open arm-C pass-through window. Rows whose window has
    not closed (no bars past the sighting for arm C, no outcome bar at all) are left
    NULL and reported with a reason — the column is fillable exactly once, so a guess
    written early can never be corrected.

    ``bars`` is the daily outcome series (injected, same shape the snapshot builder
    takes). Without it nothing is computed: outcome bars are the only honest input.
    """
    from journal.store import set_counterfactual_outcome

    eval_day: dt.date | None = None
    if dates:
        parsed = [_as_date(d) for d in dates]
        eval_day = max(parsed)

    results: list[Counterfactual] = []
    rows = conn.execute(
        "SELECT id, date, instrument_hypothesis, indicator_values "
        "FROM noshots WHERE counterfactual_outcome IS NULL ORDER BY date, id"
    ).fetchall()

    for row in rows:
        noshot_id = int(row["id"])
        day = dt.datetime.strptime(row["date"], "%Y-%m-%d").date()
        hypothesis = json.loads(row["instrument_hypothesis"])
        arm = str(hypothesis.get("arm", "?"))

        if bars is None:
            results.append(
                Counterfactual(noshot_id, arm, row["date"], None,
                               "no outcome bars supplied — nothing computable yet")
            )
            continue

        day_bar = _bar_on_or_before(bars, day)
        future = _bars_after(bars, day)

        if day_bar is None:
            results.append(
                Counterfactual(noshot_id, arm, row["date"], None,
                               f"no bar on or before {day.isoformat()} in the outcome series")
            )
            continue

        if arm == "B":
            outcome = _arm_b_outcome(hypothesis, day_bar, day)
        elif arm == "C":
            if not future:
                results.append(
                    Counterfactual(
                        noshot_id, arm, row["date"], None,
                        "pass-through window still open: no session after "
                        f"{day.isoformat()} in the outcome series (arm C holds "
                        "90-180 DTE, so the window is days, not minutes)",
                    )
                )
                continue
            as_of = eval_day or future[-1].t.date()
            eval_bar = _bar_on_or_before(bars, as_of)
            if eval_bar is None or eval_bar.t.date() <= day:
                results.append(
                    Counterfactual(
                        noshot_id, arm, row["date"], None,
                        f"no outcome bar on or before {as_of.isoformat()}",
                    )
                )
                continue
            outcome = _arm_c_outcome(hypothesis, day_bar, eval_bar, day, eval_bar.t.date())
        else:
            results.append(
                Counterfactual(noshot_id, arm, row["date"], None,
                               f"no counterfactual basis implemented for arm {arm}")
            )
            continue

        if outcome is None:
            results.append(
                Counterfactual(noshot_id, arm, row["date"], None,
                               "hypothesis lacks the fields the counterfactual needs")
            )
            continue

        if write:
            set_counterfactual_outcome(conn, noshot_id, outcome)
        results.append(Counterfactual(noshot_id, arm, row["date"], outcome, "computed"))

    return results


def _arm_b_outcome(
    hypothesis: dict[str, Any], bar: Bar, day: dt.date
) -> dict[str, Any] | None:
    """What the underlying did in the session the 0DTE would have been traded in."""
    direction = hypothesis.get("direction", "call")
    strike = hypothesis.get("strike")
    entry_ref = bar.o
    move = bar.c - entry_ref
    favourable = (bar.h - entry_ref) if direction == "call" else (entry_ref - bar.l)
    adverse = (entry_ref - bar.l) if direction == "call" else (bar.h - entry_ref)
    projection = hypothesis.get("strike_projection") or {}
    level = projection.get("level")
    above_level = None if level is None else bar.c > float(level)
    return {
        "basis": "underlying_session",
        "basis_note": (
            "Arm B is 0DTE and exits the same session, so the window closes at the "
            "close. What is recorded is the UNDERLYING's session path, not an option "
            "mark: the hypothetical fill time and premium path belong to the intraday "
            "watch loop, and inventing them here would fabricate the number this whole "
            "ledger exists to measure."
        ),
        "evaluated_on": day.isoformat(),
        "bar_time": bar.t.isoformat().replace("+00:00", "Z"),
        "session": {"open": bar.o, "high": bar.h, "low": bar.l, "close": bar.c,
                    "volume": bar.v},
        "direction": direction,
        "hypothetical_strike": strike,
        "move_from_open_pct": (move / entry_ref) if entry_ref else None,
        "max_favourable_excursion_pct": (favourable / entry_ref) if entry_ref else None,
        "max_adverse_excursion_pct": (adverse / entry_ref) if entry_ref else None,
        "close_beyond_projection": above_level,
        "option_pnl_modelled": False,
    }


def _arm_c_outcome(
    hypothesis: dict[str, Any],
    entry_bar: Bar,
    eval_bar: Bar,
    day: dt.date,
    eval_date: dt.date,
) -> dict[str, Any] | None:
    """Delta-weighted underlying pass-through — a proxy, labelled as one."""
    delta = hypothesis.get("delta")
    premium = hypothesis.get("premium")
    qty = hypothesis.get("max_contracts")
    if delta is None:
        return None
    s0 = entry_bar.c
    s1 = eval_bar.c
    move = s1 - s0
    gross_per_contract = float(delta) * 100.0 * move
    cost_per_contract = (float(premium) * 100.0) if premium is not None else None
    net_per_contract = (
        None if cost_per_contract is None else gross_per_contract - cost_per_contract
    )
    n = int(qty) if isinstance(qty, int) and qty > 0 else 0
    return {
        "basis": "underlying_delta_proxy",
        "basis_note": (
            "APPROXIMATION, not a mark. The gross figure is delta x 100 x underlying "
            "move from the sighting close to the evaluation close, less the premium the "
            "hypothetical entry would have paid. Delta is a point-in-time sensitivity; a "
            "deep-ITM long call also carries time value and a changing delta, so this "
            "understates nothing and overstates nothing in particular. Read it as "
            "'direction and rough magnitude', never as P&L."
        ),
        "entry_date": day.isoformat(),
        "evaluated_on": eval_date.isoformat(),
        "sessions_held": (eval_date - day).days,
        "underlying_close_at_sighting": s0,
        "underlying_close_at_evaluation": s1,
        "underlying_move": move,
        "underlying_move_pct": (move / s0) if s0 else None,
        "contract": hypothesis.get("contract"),
        "dte_at_sighting": hypothesis.get("dte"),
        "delta_at_sighting": delta,
        "premium_per_contract_usd": premium,
        "contracts": n,
        "gross_per_contract_usd": gross_per_contract,
        "net_per_contract_usd": net_per_contract,
        "gross_total_usd": gross_per_contract * n,
        "net_total_usd": None if net_per_contract is None else net_per_contract * n,
        "within_premium_cap": hypothesis.get("premium_within_cap"),
    }


def _as_date(value: dt.date | str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.datetime.strptime(str(value), "%Y-%m-%d").date()


def summarise(drafts: Sequence[NoshotDraft]) -> str:
    if not drafts:
        return "no sightings today — nothing in the NO-SHOT log"
    lines = [f"{len(drafts)} sighting(s) walked away from:"]
    for draft in drafts:
        hyp = draft.instrument_hypothesis
        named = hyp.get("contract_symbol") or hyp.get("contract") or hyp.get("strike") or "?"
        failed = ",".join(draft.assessment.failed) or "(none)"
        lines.append(
            f"  {draft.arm}/{draft.direction}: {failed} — hypothetical {named}"
        )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MARGINAL_LIMIT",
    "Counterfactual",
    "NoshotDraft",
    "NoshotError",
    "SightsAssessment",
    "arm_b_hypothesis",
    "arm_c_hypothesis",
    "assess_sights",
    "build_noshots",
    "fill_counterfactuals",
    "summarise",
    "write_noshots",
]