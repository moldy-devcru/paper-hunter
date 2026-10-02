"""Weekly rollups and counterfactual scoring — the review half of the journal.

The brief, "Journaling & review":

    "Weekly rollup: P&L per arm vs A, trade/no-trade counts, checklist-failure
     histogram (which conditions veto most), counterfactual ledger delta."

and "Monthly review: operator + agents" — which is where the pre-registered
predictions in the brief get scored, and where the whole point of the project is paid
out: :func:`prediction_scorecard` is the function that decides, from the journal, which
of the four claims the data supports and which it kills.

Everything here is a **pure read** over a journal: no writes, no network, no I/O
beyond the connection it is handed. A weekly rollup that could edit the journal would
be an experiment that grades its own homework, so the module has no write path at all.

Honesty rules baked into the shapes
-----------------------------------
1. **Exception-path P&L is a separate key, never a row in the per-arm table that gets
   summed with the others.** The brief requires it ("tracked SEPARATELY in the journal
   so the window can score: does the operator's hunting eye beat the frozen rules?"),
   and it is the one place where a small number can flatter a large one.
2. **Proxy counterfactuals are never summed with real ones.** The NO-SHOT ledger holds
   two kinds of "what would have happened": a delta-weighted underlying proxy for arm C
   and, for arm B's 0DTE, only the underlying's session path (no option P&L is
   modelled at all). Adding those to each other would produce a single confident
   number built from an approximation and a non-computation.
   :func:`counterfactual_delta` therefore returns three buckets — ``real``,
   ``proxy_delta_weighted`` and ``unmodelled`` — and the headline delta is computed
   *within* the proxy bucket or not at all.
3. **T3 is counted once.** The checklist evaluates T3 as an OR group
   (``satisfied_if_any_of``); when neither arm passes, the veto is carried by T3a with
   T3b folded in non-blocking. The histogram keys on the *group* id and de-duplicates
   per source row, so a T3 veto is one count, not two.
4. **PENDING is not a veto.** An uncalibrated gate (T6's flow multiplier) blinded the
   system; it did not reject a setup. PENDING is counted in its own column.
5. **Insufficient data is a value, not an exception.** Every function returns a
   ``sufficient`` flag and an explicit reason instead of raising or inventing a zero.

# INTERPRETATION: the window.
``since``/``until`` are inclusive calendar dates. ``positions`` are filtered on the
**UTC date component of ``exit_ts``** because that is the journal's only clock
(``ts`` columns are UTC ISO-8601; there is no ET column on positions). ``noshots`` are
filtered on their ``date`` column, which the schema defines as the ET session date.
So a position exiting at 02:00Z on the 3rd (21:00 ET on the 2nd) lands in the 3rd's
window. This one-day seam is a real asymmetry between the two tables and it is named
here rather than papered over with a timezone conversion nobody can audit.

# INTERPRETATION: what counts as a "trade" in the counts.
A trade is a ``decisions`` row with kind ``TRADE`` — the decision ledger is the record
of truth, and ``positions`` is the convenience view (per its own schema comment). A
no-trade is a ``NO_TRADE`` decision; sightings walked away from are ``noshots`` rows.
All three are reported separately because collapsing them would hide the difference
between "the checklist said no" and "the checklist said maybe and the session went
nowhere".

Python 3.12+, stdlib + the journal.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

from analysis import shadow_roll

CONTROL_ARM = "A"
ACTIVE_ARMS: tuple[str, ...] = ("B", "C")
ARMS: tuple[str, ...] = ("A", "B", "C", "EXCEPTION")
EXCEPTION_ARM = "EXCEPTION"

#: OR-groups in the frozen checklist. Keyed by the condition ids the evaluator emits.
CONDITION_GROUPS: dict[str, str] = {"T3a": "T3", "T3b": "T3"}

#: The brief's pre-registered frequency expectation, prediction #4.
EXPECTED_TRADES_PER_MONTH = 2.0

#: Mean Gregorian month length, for turning a day count into "per month".
DAYS_PER_MONTH = 365.2425 / 12.0


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _conn(store: Any) -> sqlite3.Connection:
    if hasattr(store, "execute"):
        return store
    for attr in ("conn", "connection"):
        inner = getattr(store, attr, None)
        if hasattr(inner, "execute"):
            return inner
    raise TypeError(f"cannot get a sqlite connection from {store!r}")


def _as_date(value: dt.date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _window(since: dt.date | str | None, until: dt.date | str | None) -> dict[str, str | None]:
    return {"since": _as_date(since), "until": _as_date(until)}


def _where(
    column: str,
    since: str | None,
    until: str | None,
    base: tuple[str, ...] = (),
) -> tuple[str, list[Any]]:
    """A complete ``WHERE`` clause for a window on one text date/timestamp column.

    ``column`` is a text date or a text timestamp; both compare correctly on text
    because the journal stores ISO-8601 with a leading date component.
    """
    clauses, params = list(base), []
    if since is not None:
        clauses.append(f"substr({column}, 1, 10) >= ?")
        params.append(since)
    if until is not None:
        clauses.append(f"substr({column}, 1, 10) <= ?")
        params.append(until)
    return (f" WHERE {' AND '.join(clauses)}" if clauses else ""), params


def _loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _bankroll(conn: sqlite3.Connection) -> dict[str, float]:
    from journal.store import get_meta

    raw = get_meta(conn, "arm_bankroll", None)
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _no_data(reason: str) -> dict[str, Any]:
    return {"sufficient": False, "reason": reason}


# ---------------------------------------------------------------------------
# 1. P&L per arm
# ---------------------------------------------------------------------------


def arm_pnl(
    store: Any,
    since: dt.date | str | None = None,
    until: dt.date | str | None = None,
) -> dict[str, Any]:
    """Realized P&L per arm over the window, with arm A as the control.

    Realized means *closed positions whose exit falls in the window*. An open position
    is counted (so "two of arm C's three positions are still open" is visible) but
    contributes nothing to P&L — marking it to market here would need a price series the
    journal does not hold, and inventing a mark is how a paper experiment starts
    lying.

    A closed position with a NULL ``pnl`` is excluded from the sum and counted in
    ``missing_pnl`` rather than silently treated as zero.
    """
    conn = _conn(store)
    where, params = _where("exit_ts", _as_date(since), _as_date(until), ("status = 'CLOSED'",))
    rows = conn.execute(
        f"SELECT * FROM positions{where} ORDER BY exit_ts, id", params
    ).fetchall()

    open_where, open_params = _where(
        "entry_ts", _as_date(since), _as_date(until), ("status = 'OPEN'",)
    )
    open_rows = conn.execute(
        f"SELECT * FROM positions{open_where} ORDER BY entry_ts, id",
        open_params,
    ).fetchall()

    bankroll = _bankroll(conn)
    arms: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        arms[arm] = {
            "arm": arm,
            "realized_pnl": 0.0,
            "closed": 0,
            "missing_pnl": 0,
            "open": 0,
            "bankroll": bankroll.get(arm),
            "return_on_bankroll": None,
        }
    for row in rows:
        bucket = arms.get(row["arm"])
        if bucket is None:  # arm outside the vocabulary: counted, not crashed on
            arms.setdefault(
                row["arm"],
                {
                    "arm": row["arm"],
                    "realized_pnl": 0.0,
                    "closed": 0,
                    "missing_pnl": 0,
                    "open": 0,
                    "bankroll": bankroll.get(row["arm"]),
                    "return_on_bankroll": None,
                },
            )
            bucket = arms[row["arm"]]
        bucket["closed"] += 1
        if row["pnl"] is None:
            bucket["missing_pnl"] += 1
            continue
        bucket["realized_pnl"] += float(row["pnl"])
    for row in open_rows:
        if row["arm"] in arms:
            arms[row["arm"]]["open"] += 1

    for bucket in arms.values():
        if bucket["bankroll"]:
            bucket["return_on_bankroll"] = bucket["realized_pnl"] / bucket["bankroll"]

    control = arms[CONTROL_ARM]["realized_pnl"]
    for bucket in arms.values():
        bucket["vs_control"] = bucket["realized_pnl"] - control
        bucket["vs_control_pct_of_control"] = (
            (bucket["realized_pnl"] - control) / abs(control) if control else None
        )

    closed_total = sum(b["closed"] for b in arms.values())
    return {
        "window": _window(since, until),
        "control_arm": CONTROL_ARM,
        "sufficient": closed_total > 0,
        "reason": None
        if closed_total
        else "no closed position in the window — nothing to mark as realized P&L",
        "arms": arms,
        "exception_path": {
            **arms[EXCEPTION_ARM],
            "note": (
                "Catalyst-clause / exception-path P&L is deliberately NOT part of the "
                "A/B/C comparison and is never summed into it (brief: 'tracked "
                "SEPARATELY ... does the operator's hunting eye beat the frozen rules?')."
            ),
        },
        "open_positions": [
            {"id": r["id"], "arm": r["arm"], "entry_ts": r["entry_ts"],
             "entry_price": r["entry_price"], "qty": r["qty"]}
            for r in open_rows
        ],
    }


# ---------------------------------------------------------------------------
# 2. trade / no-trade counts
# ---------------------------------------------------------------------------


def trade_counts(
    store: Any,
    since: dt.date | str | None = None,
    until: dt.date | str | None = None,
) -> dict[str, Any]:
    """Trades, no-trades, rolls, stops and sightings per arm over the window."""
    conn = _conn(store)
    where, params = _where("ts", _as_date(since), _as_date(until))
    decisions = conn.execute(
        f"SELECT arm, kind, COUNT(*) AS n FROM decisions{where} GROUP BY arm, kind", params
    ).fetchall()

    n_where, n_params = _where("date", _as_date(since), _as_date(until))
    noshot_rows = conn.execute(
        f"SELECT instrument_hypothesis, failed_conditions FROM noshots{n_where} ORDER BY date, id",
        n_params,
    ).fetchall()

    by_arm: dict[str, dict[str, int]] = {}
    for row in decisions:
        bucket = by_arm.setdefault(row["arm"], {})
        bucket[row["kind"]] = bucket.get(row["kind"], 0) + int(row["n"])

    noshots_by_arm: dict[str, int] = {}
    for row in noshot_rows:
        arm = str((_loads(row["instrument_hypothesis"]) or {}).get("arm", "?"))
        noshots_by_arm[arm] = noshots_by_arm.get(arm, 0) + 1

    arms = sorted(set(by_arm) | set(noshots_by_arm) | set(ARMS))
    table: dict[str, dict[str, Any]] = {}
    for arm in arms:
        kinds = by_arm.get(arm, {})
        table[arm] = {
            "arm": arm,
            "trades": kinds.get("TRADE", 0),
            "no_trades": kinds.get("NO_TRADE", 0),
            "proposals": kinds.get("PROPOSAL", 0),
            "rolls": kinds.get("ROLL", 0),
            "stops": kinds.get("STOP", 0),
            "vetoes": kinds.get("VETO", 0),
            "noshots": noshots_by_arm.get(arm, 0),
        }

    combined = {
        key: sum(table.get(arm, {}).get(key, 0) for arm in ACTIVE_ARMS)
        for key in ("trades", "no_trades", "proposals", "rolls", "stops", "noshots")
    }
    days = _window_days(since, until)
    combined["days"] = days
    combined["months"] = days / DAYS_PER_MONTH if days is not None else None
    combined["trades_per_month"] = (
        combined["trades"] / (days / DAYS_PER_MONTH)
        if days and combined["trades"] is not None
        else None
    )
    combined["frequency_under_two_per_month"] = (
        None
        if combined["trades_per_month"] is None
        else combined["trades_per_month"] < EXPECTED_TRADES_PER_MONTH
    )
    return {
        "window": _window(since, until),
        "sufficient": any(v["trades"] or v["no_trades"] or v["noshots"] for v in table.values()),
        "reason": None,
        "by_arm": table,
        "combined_active_arms": combined,
    }


def _window_days(since: dt.date | str | None, until: dt.date | str | None) -> int | None:
    start, end = _as_date(since), _as_date(until)
    if start is None or end is None:
        return None
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1


# ---------------------------------------------------------------------------
# 3. checklist-failure histogram
# ---------------------------------------------------------------------------


def _veto_ids(conditions: Any, failed_list: Any) -> set[str]:
    """Blocking FAIL condition-group ids for one source row.

    Reads the rich ``conditions`` map when present (status + blocking per condition)
    and falls back to the ``failed_conditions`` list otherwise. T3a/T3b collapse to the
    ``T3`` group id and the result is a set, so a group veto can never be counted
    twice no matter which column said so.
    """
    vetoes: set[str] = set()
    if isinstance(conditions, dict) and conditions:
        for cid, body in conditions.items():
            body = body if isinstance(body, dict) else {}
            status = body.get("status")
            if status != "FAIL":
                continue
            if body.get("blocking") is False:
                continue
            vetoes.add(CONDITION_GROUPS.get(str(cid), str(cid)))
        return vetoes
    for cid in failed_list or []:
        vetoes.add(CONDITION_GROUPS.get(str(cid), str(cid)))
    return vetoes


def _pending_ids(conditions: Any, failed_list: Any) -> set[str]:
    pendings: set[str] = set()
    if isinstance(conditions, dict) and conditions:
        for cid, body in conditions.items():
            body = body if isinstance(body, dict) else {}
            if body.get("status") == "PENDING" and body.get("blocking") is not False:
                pendings.add(CONDITION_GROUPS.get(str(cid), str(cid)))
        return pendings
    for cid, body in (failed_list or {}).items():
        if isinstance(body, dict) and body.get("status") == "PENDING":
            if body.get("blocking") is False:
                continue
            pendings.add(CONDITION_GROUPS.get(str(cid), str(cid)))
    return pendings


def _noshot_conditions(failed: Any) -> dict[str, Any] | None:
    """Normalize a noshot row's ``failed_conditions`` into a condition map.

    The noshot writer stores ``{condition: {status, blocking, detail}}`` for every
    blocking non-PASS condition — which includes blocking **PENDING** ones, because
    a PENDING gate did stop the trade. Feeding that straight into the veto counter
    would file blindness as a veto, so it is normalized to the same shape the
    decisions' ``checklist_state.conditions`` uses and read by the same code.
    """
    if not isinstance(failed, dict) or not failed:
        return None
    out: dict[str, Any] = {}
    for cid, body in failed.items():
        if isinstance(body, dict):
            out[str(cid)] = {
                "status": body.get("status"),
                "blocking": body.get("blocking", True),
            }
        else:
            # A bare value (not the shape the writer produces): the id is a failed
            # condition, its status is unknown. Counted as a veto, never as a pass.
            out[str(cid)] = {"status": "FAIL", "blocking": True}
    return out


def _arm_of_noshot(row: sqlite3.Row) -> str:
    return str((_loads(row["instrument_hypothesis"]) or {}).get("arm", "?"))


def checklist_failure_histogram(
    store: Any,
    since: dt.date | str | None = None,
    until: dt.date | str | None = None,
) -> dict[str, Any]:
    """Which conditions veto most, over NO_TRADE decisions and NO-SHOT rows.

    Two sources, because the brief's vetoes arrive by two roads: a plan-time
    ``NO_TRADE`` decision (the checklist did not fire at 08:30) and a ``noshots`` row
    (the sights were on and the shot was still not taken). Counting only one would
    under-report the conditions that veto *after* the plan.
    """
    conn = _conn(store)
    where, params = _where("ts", _as_date(since), _as_date(until), ("kind = 'NO_TRADE'",))
    decisions = conn.execute(
        f"SELECT id, arm, checklist_state FROM decisions{where} ORDER BY ts, id",
        params,
    ).fetchall()
    n_where, n_params = _where("date", _as_date(since), _as_date(until))
    noshots = conn.execute(
        f"SELECT id, instrument_hypothesis, failed_conditions FROM noshots{n_where} "
        f"ORDER BY date, id",
        n_params,
    ).fetchall()

    totals: dict[str, int] = {}
    per_arm: dict[str, dict[str, int]] = {}
    pending_totals: dict[str, int] = {}

    def bump(table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1

    for row in decisions:
        state = _loads(row["checklist_state"]) or {}
        vetoes = _veto_ids(state.get("conditions"), state.get("failed_conditions"))
        for cid in vetoes:
            bump(totals, cid)
            bump(per_arm.setdefault(row["arm"], {}), cid)
        for cid in _pending_ids(state.get("conditions"), state.get("failed_conditions")):
            bump(pending_totals, cid)
    for row in noshots:
        arm = _arm_of_noshot(row)
        conditions = _noshot_conditions(_loads(row["failed_conditions"]))
        for cid in _veto_ids(conditions, None):
            bump(totals, cid)
            bump(per_arm.setdefault(arm, {}), cid)
        for cid in _pending_ids(conditions, None):
            bump(pending_totals, cid)

    sources = {"no_trade_decisions": len(decisions), "noshots": len(noshots)}
    return {
        "window": _window(since, until),
        "sufficient": bool(totals or pending_totals),
        "reason": None
        if (totals or pending_totals)
        else "no NO_TRADE decisions and no sightings in the window — the checklist never vetoed",
        "sources": sources,
        "by_condition": dict(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_arm": {
            arm: dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
            for arm, counts in sorted(per_arm.items())
        },
        "pending_by_condition": dict(sorted(pending_totals.items())),
        "grouping_note": (
            "T3 is an OR group (satisfied_if_any_of: squeeze_release, band_rejection); "
            "its T3a/T3b conditions are counted once per row under 'T3', so the "
            "histogram matches how many sessions the group actually blocked."
        ),
    }


# ---------------------------------------------------------------------------
# 4. counterfactual ledger delta
# ---------------------------------------------------------------------------


def counterfactual_delta(
    store: Any,
    since: dt.date | str | None = None,
    until: dt.date | str | None = None,
) -> dict[str, Any]:
    """"Is the discipline saving us or costing us?" — with the bases kept apart.

    The headline the brief wants is a single number. It is not available, and the
    function says so structurally instead of manufacturing one:

    * **real** — hypotheticals whose actual option P&L was computed and filled in.
      Nothing produces these yet (the intraday watch loop does), so this bucket is
      normally empty and honestly so.
    * **proxy_delta_weighted** — arm C's ``underlying_delta_proxy`` rows. Summable
      among themselves, and labelled a proxy in the returned dict, in the row basis and
      in the rendered text.
    * **unmodelled** — arm B's ``underlying_session`` rows: the underlying's path on
      the day the 0DTE would have been traded, with ``option_pnl_modelled: False``.
      Summable as *directional* tallies, not as dollars.

    ``delta_usd`` is the difference between the proxy bucket and the realized P&L of
    the *same arms*, and is ``None`` whenever either side is missing.
    """
    conn = _conn(store)
    where, params = _where("date", _as_date(since), _as_date(until))
    rows = conn.execute(
        f"SELECT id, date, instrument_hypothesis, counterfactual_outcome FROM noshots{where} "
        f"ORDER BY date, id",
        params,
    ).fetchall()

    real = {"count": 0, "total_usd": 0.0, "rows": []}
    proxy = {"count": 0, "total_usd": 0.0, "per_arm": {}, "rows": []}
    unmodelled = {
        "count": 0,
        "arms": [],
        "directional": {
            "sessions_closing_beyond_projection": 0,
            "sessions_closing_inside_projection": 0,
            "sessions_with_projection_unknown": 0,
            "sum_move_from_open_pct": 0.0,
            "sum_max_favourable_excursion_pct": 0.0,
            "sum_max_adverse_excursion_pct": 0.0,
        },
        "rows": [],
    }
    proxy_arms: set[str] = set()

    for row in rows:
        outcome = _loads(row["counterfactual_outcome"])
        arm = _arm_of_noshot(row)
        if outcome is None:
            continue
        basis = str(outcome.get("basis", "unknown"))
        if basis == "underlying_delta_proxy":
            proxy["count"] += 1
            total = outcome.get("net_total_usd")
            if isinstance(total, (int, float)):
                proxy["total_usd"] += float(total)
                proxy["per_arm"][arm] = proxy["per_arm"].get(arm, 0.0) + float(total)
                proxy_arms.add(arm)
            proxy["rows"].append(
                {"noshot_id": row["id"], "date": row["date"], "arm": arm,
                 "net_total_usd": total, "basis": basis}
            )
        elif basis == "underlying_session":
            unmodelled["count"] += 1
            unmodelled["arms"].append(arm)
            d = unmodelled["directional"]
            beyond = outcome.get("close_beyond_projection")
            if beyond is True:
                d["sessions_closing_beyond_projection"] += 1
            elif beyond is False:
                d["sessions_closing_inside_projection"] += 1
            else:
                d["sessions_with_projection_unknown"] += 1
            for key, source in (
                ("sum_move_from_open_pct", "move_from_open_pct"),
                ("sum_max_favourable_excursion_pct", "max_favourable_excursion_pct"),
                ("sum_max_adverse_excursion_pct", "max_adverse_excursion_pct"),
            ):
                value = outcome.get(source)
                if isinstance(value, (int, float)):
                    d[key] += float(value)
            unmodelled["rows"].append(
                {"noshot_id": row["id"], "date": row["date"], "arm": arm,
                 "move_from_open_pct": outcome.get("move_from_open_pct"),
                 "close_beyond_projection": beyond, "basis": basis,
                 "option_pnl_modelled": False}
            )
        else:
            real["count"] += 1
            value = outcome.get("net_total_usd")
            if isinstance(value, (int, float)):
                real["total_usd"] += float(value)
            real["rows"].append(
                {"noshot_id": row["id"], "date": row["date"], "arm": arm,
                 "net_total_usd": value, "basis": basis}
            )

    pnl = arm_pnl(conn, since, until)
    realized_same_arms = 0.0
    for arm in sorted(proxy_arms):
        realized_same_arms += float(pnl["arms"].get(arm, {}).get("realized_pnl", 0.0))
    delta = (
        proxy["total_usd"] - realized_same_arms
        if proxy["count"]
        and proxy_arms
        and any(pnl["arms"].get(a, {}).get("closed", 0) > 0 for a in proxy_arms)
        else None
    )
    if delta is None:
        verdict = "insufficient data"
    elif delta > 0:
        verdict = (
            "PROXY BASIS: the rejected trades would have made MORE than the trades we "
            "took — on this (approximate) measure the discipline is costing us"
        )
    else:
        verdict = (
            "PROXY BASIS: the rejected trades would have made LESS than the trades we "
            "took — on this (approximate) measure the discipline is saving us"
        )

    return {
        "window": _window(since, until),
        "sufficient": bool(proxy["count"] or unmodelled["count"] or real["count"]),
        "reason": None
        if (proxy["count"] or unmodelled["count"] or real["count"])
        else "no sighting in the window has a filled counterfactual_outcome yet",
        "realized_pnl_same_arms": realized_same_arms,
        "realized_closed_same_arms": sum(
            int(pnl["arms"].get(a, {}).get("closed", 0)) for a in sorted(proxy_arms)
        ),
        "real": real,
        "proxy_delta_weighted": {
            **proxy,
            "basis": "underlying_delta_proxy",
            "note": (
                "APPROXIMATION: delta x underlying move, less hypothetical premium. "
                "Direction and rough magnitude only — never an option mark."
            ),
        },
        "unmodelled": {
            **unmodelled,
            "note": (
                "Arm B is 0DTE and exits the same session; only the underlying's path "
                "is recorded. No option P&L exists for these rows, so they are tallied "
                "directionally and are never added to any dollar figure."
            ),
        },
        "delta_usd": delta,
        "delta_basis": "proxy_delta_weighted vs realized, same arms only",
        "verdict": verdict,
        "honesty_note": (
            "The three buckets are never summed together. A single headline number "
            "would be an approximation plus a non-computation wearing a decimal point."
        ),
    }


# ---------------------------------------------------------------------------
# 5. pre-registered predictions
# ---------------------------------------------------------------------------

PREDICTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "1",
        "claim": "Arm A (SPY buy-and-hold) finishes the window positive (market beta).",
        "measurement": "arm A realized P&L > 0 over the window",
    },
    {
        "id": "2",
        "claim": (
            "Arm B (0DTE OTM) underperforms arm A; selective timing may or may not "
            "overcome the structural decay — falsifiable either way."
        ),
        "measurement": "arm B realized P&L minus arm A realized P&L",
    },
    {
        "id": "3",
        "claim": (
            "Arm C tracks arm A with leverage-amplified variance; do TA entries pick "
            "better roll points than a fixed quarterly roll?"
        ),
        "measurement": (
            "arm C realized P&L minus arm A realized P&L, plus arm C's return on "
            "capital vs the shadow roll's"
        ),
    },
    {
        "id": "4",
        "claim": (
            "Hunting discipline means fewer than 2 trades/month across arms B and C "
            "combined. If we are trading weekly, the rules were not frozen tight enough."
        ),
        "measurement": "TRADE decisions on B+C divided by months elapsed",
    },
)

STATUS_SUPPORTED = "supported"
STATUS_FALSIFIED = "falsified"
STATUS_INSUFFICIENT = "insufficient_data"


def _empty_scorecard(reason: str, window: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "sufficient": False,
        "reason": reason,
        "window": window or {},
        "tally": {"supported": 0, "falsified": 0, "insufficient_data": len(PREDICTIONS)},
        "predictions": [
            {
                "id": p["id"],
                "claim": p["claim"],
                "measurement": p["measurement"],
                "status": STATUS_INSUFFICIENT,
                "evidence": reason,
                "numbers": {},
            }
            for p in PREDICTIONS
        ],
    }


def _journal_window(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """The experiment window: ``meta.window_start`` through the latest journal date."""
    from journal.store import get_meta

    start = get_meta(conn, "window_start", None)
    start = _as_date(start)
    row = conn.execute(
        "SELECT MAX(d) AS d FROM ("
        "  SELECT substr(ts, 1, 10) AS d FROM decisions"
        "  UNION ALL SELECT date AS d FROM noshots"
        "  UNION ALL SELECT substr(exit_ts, 1, 10) AS d FROM positions"
        "  UNION ALL SELECT opened_on AS d FROM shadow_roll_legs"
        "  UNION ALL SELECT date AS d FROM shadow_roll_marks"
        ")"
    ).fetchone()
    end = row["d"] if row else None
    return start, end


def prediction_scorecard(
    store: Any,
    since: dt.date | str | None = None,
    until: dt.date | str | None = None,
) -> dict[str, Any]:
    """Score the brief's four pre-registered predictions from the journal alone.

    With no ``since``/``until``, the window is the experiment's own
    (``meta.window_start`` → the latest date anywhere in the journal). Every prediction
    reports one of three statuses — ``supported``, ``falsified``,
    ``insufficient_data`` — and a prediction with no trades in it is
    ``insufficient_data``, never "supported" by default. A pre-registered test that
    defaults to pass is not a test.
    """
    conn = _conn(store)
    j_start, j_end = _journal_window(conn)
    start = _as_date(since) or j_start
    end = _as_date(until) or j_end

    pnl = arm_pnl(conn, start, end)
    counts = trade_counts(conn, start, end)
    days = _window_days(start, end)
    months = days / DAYS_PER_MONTH if days else None

    window = {
        "since": start,
        "until": end,
        "days": days,
        "months": months,
        "source": "arguments" if (since or until) else "meta.window_start .. latest journal date",
    }
    if not start and not end:
        return _empty_scorecard(
            "the journal is empty: no window_start in meta and no rows to date the window from",
            window,
        )

    arms = pnl["arms"]

    def arm_return(arm: str) -> float | None:
        bankroll = arms.get(arm, {}).get("bankroll")
        if not bankroll:
            return None
        return arms[arm]["realized_pnl"] / bankroll

    predictions: list[dict[str, Any]] = []

    # --- 1: arm A positive ------------------------------------------------
    a = arms[CONTROL_ARM]
    if a["closed"] == 0:
        p1 = {
            "status": STATUS_INSUFFICIENT,
            "evidence": (
                "arm A has no closed position in the window — buy-and-hold has not "
                "been marked, so the claim cannot be scored"
            ),
            "numbers": {"A_closed": 0, "A_realized_pnl": a["realized_pnl"]},
        }
    else:
        positive = a["realized_pnl"] > 0
        p1 = {
            "status": STATUS_SUPPORTED if positive else STATUS_FALSIFIED,
            "evidence": (
                f"arm A realized ${a['realized_pnl']:,.2f} over {a['closed']} closed "
                f"position(s) — {'positive' if positive else 'not positive'}"
            ),
            "numbers": {
                "A_closed": a["closed"],
                "A_realized_pnl": a["realized_pnl"],
                "A_return_on_bankroll": a["return_on_bankroll"],
            },
        }
    predictions.append({**_meta(PREDICTIONS[0]), **p1})

    # --- 2: arm B vs arm A ----------------------------------------------
    b = arms["B"]
    b_trades = counts["by_arm"].get("B", {}).get("trades", 0)
    if a["closed"] == 0 or b_trades == 0:
        missing = []
        if a["closed"] == 0:
            missing.append("arm A has no closed position")
        if b_trades == 0:
            missing.append("arm B has no TRADE decision")
        p2 = {
            "status": STATUS_INSUFFICIENT,
            "evidence": (
                f"{' and '.join(missing)} — with no arm B trade in the window the "
                "structural-decay claim has no sample"
            ),
            "numbers": {"B_trades": b_trades, "B_closed": b["closed"],
                        "B_realized_pnl": b["realized_pnl"], "A_realized_pnl": a["realized_pnl"]},
        }
    else:
        vs_a = b["vs_control"]
        p2 = {
            "status": STATUS_SUPPORTED if vs_a <= 0 else STATUS_FALSIFIED,
            "evidence": (
                f"arm B is ${vs_a:,.2f} against arm A over {b['closed']} closed "
                f"position(s) — "
                + (
                    "B underperformed (as predicted)"
                    if vs_a <= 0
                    else "B BEAT A, which is the genuine finding"
                )
            ),
            "numbers": {
                "B_trades": b_trades,
                "B_closed": b["closed"],
                "B_realized_pnl": b["realized_pnl"],
                "A_realized_pnl": a["realized_pnl"],
                "B_minus_A": vs_a,
            },
        }
    predictions.append({**_meta(PREDICTIONS[1]), **p2})

    # --- 3: arm C vs arm A + the shadow roll ----------------------------
    c = arms["C"]
    c_trades = counts["by_arm"].get("C", {}).get("trades", 0)
    shadow = shadow_roll.score_shadow_vs_arm_c(conn, arm_return("C") if c_trades else None)
    if a["closed"] == 0 or c_trades == 0:
        missing = []
        if a["closed"] == 0:
            missing.append("arm A has no closed position")
        if c_trades == 0:
            missing.append("arm C has no TRADE decision")
        p3 = {
            "status": STATUS_INSUFFICIENT,
            "evidence": (
                f"{' and '.join(missing)} — 'tracks A' needs both sides marked, and the "
                "roll-point question needs arm C entries to compare against the "
                "fixed quarterly roll"
            ),
            "numbers": {
                "C_trades": c_trades,
                "C_closed": c["closed"],
                "C_realized_pnl": c["realized_pnl"],
                "A_realized_pnl": a["realized_pnl"],
                "shadow_roll_sufficient": shadow["sufficient"],
            },
            "shadow_roll": shadow,
        }
    else:
        vs_a = c["vs_control"]
        tracks = vs_a >= 0
        p3 = {
            "status": STATUS_SUPPORTED if tracks else STATUS_FALSIFIED,
            "evidence": (
                f"arm C is ${vs_a:,.2f} against arm A over {c['closed']} closed "
                f"position(s) — {'C is at or above A' if tracks else 'C is below A'}"
                + (
                    "; fixed-roll comparison: "
                    + (
                        f"TA entries "
                        f"{'BEAT' if shadow['ta_entries_beat_fixed_roll'] else 'trail'} "
                        f"the fixed quarterly roll by "
                        f"{shadow['delta_return_on_capital']:+.2%} on return on capital"
                        if shadow.get("delta_return_on_capital") is not None
                        else "not yet comparable ("
                        + (
                            shadow["arm_c"].get("reason") or "no shadow leg marked"
                        )
                        + ")"
                    )
                )
            ),
            "numbers": {
                "C_trades": c_trades,
                "C_closed": c["closed"],
                "C_realized_pnl": c["realized_pnl"],
                "A_realized_pnl": a["realized_pnl"],
                "C_minus_A": vs_a,
                "C_return_on_capital": arm_return("C"),
                "shadow_roll_return_on_capital": shadow["shadow"]["return_on_capital"],
                "delta_return_on_capital": shadow.get("delta_return_on_capital"),
                "ta_entries_beat_fixed_roll": shadow["ta_entries_beat_fixed_roll"],
            },
            "shadow_roll": shadow,
        }
    predictions.append({**_meta(PREDICTIONS[2]), **p3})

    # --- 4: trade frequency --------------------------------------------
    combined = counts["combined_active_arms"]
    if months is None or months <= 0:
        p4 = {
            "status": STATUS_INSUFFICIENT,
            "evidence": (
                "the window has no measurable length, so 'per month' is undefined"
            ),
            "numbers": {"days": days, "months": months, "B_plus_C_trades": combined["trades"]},
        }
    else:
        rate = combined["trades"] / months
        p4 = {
            "status": STATUS_SUPPORTED if rate < EXPECTED_TRADES_PER_MONTH else STATUS_FALSIFIED,
            "evidence": (
                f"{combined['trades']} trade(s) across B+C in {months:.2f} month(s) = "
                f"{rate:.2f}/month vs the pre-registered expectation of "
                f"< {EXPECTED_TRADES_PER_MONTH:.0f}/month"
            ),
            "numbers": {
                "B_plus_C_trades": combined["trades"],
                "months": months,
                "days": days,
                "trades_per_month": rate,
                "threshold": EXPECTED_TRADES_PER_MONTH,
            },
        }
    predictions.append({**_meta(PREDICTIONS[3]), **p4})

    statuses = {p["status"] for p in predictions}
    return {
        "sufficient": STATUS_INSUFFICIENT not in statuses,
        "reason": (
            None
            if STATUS_INSUFFICIENT not in statuses
            else "at least one pre-registered prediction has no data to score it yet"
        ),
        "window": window,
        "predictions": predictions,
        "tally": {
            "supported": sum(1 for p in predictions if p["status"] == STATUS_SUPPORTED),
            "falsified": sum(1 for p in predictions if p["status"] == STATUS_FALSIFIED),
            "insufficient_data": sum(
                1 for p in predictions if p["status"] == STATUS_INSUFFICIENT
            ),
        },
    }


def _meta(prediction: dict[str, str]) -> dict[str, str]:
    return {"id": prediction["id"], "claim": prediction["claim"],
            "measurement": prediction["measurement"]}


# ---------------------------------------------------------------------------
# 6. the weekly rollup text
# ---------------------------------------------------------------------------

WEEK_DAYS = 7


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2%}"


def weekly_rollup_text(store: Any, week: dt.date | str) -> str:
    """The operator's weekly rollup, as markdown.

    ``week`` is any date inside the reporting week; the window is the **seven
    calendar days ending on that date, inclusive** (so a Friday rollup covers the
    previous Friday through this Friday). The pre-registered scorecard is always
    reported over the *whole* experiment window, never the week — a weekly scorecard
    would invite exactly the narrative-fitting the pre-registration exists to stop.

    An empty journal produces a short, explicit "not enough data" document. It never
    raises, and it never prints a zero as if it were a result.
    """
    end_date = dt.date.fromisoformat(_as_date(week) or str(week))
    start_date = end_date - dt.timedelta(days=WEEK_DAYS - 1)

    pnl = arm_pnl(store, start_date, end_date)
    counts = trade_counts(store, start_date, end_date)
    hist = checklist_failure_histogram(store, start_date, end_date)
    cf = counterfactual_delta(store, start_date, end_date)
    card = prediction_scorecard(store)

    lines: list[str] = [
        f"# Weekly rollup — {start_date.isoformat()} → {end_date.isoformat()}",
        "",
        "## P&L per arm (realized, closed positions in window)",
        "",
    ]
    if not pnl["sufficient"]:
        lines.append(f"_No closed positions in this window — {pnl['reason']}._")
        lines.append("")
    else:
        lines.append("| arm | realized | vs A | closed | open | return on bankroll |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for arm in ARMS:
            bucket = pnl["arms"][arm]
            lines.append(
                f"| {arm} | {_money(bucket['realized_pnl'])} | "
                f"{_money(bucket['vs_control'])} | {bucket['closed']} | {bucket['open']} | "
                f"{_pct(bucket['return_on_bankroll'])} |"
            )
        exc = pnl["exception_path"]
        lines += [
            "",
            f"Exception path (tracked separately, never summed in): "
            f"{_money(exc['realized_pnl'])} across {exc['closed']} closed position(s).",
        ]
    if pnl["open_positions"]:
        lines += [
            "",
            "Open at week end: "
            + ", ".join(
                f"{p['arm']}#{p['id']}" for p in pnl["open_positions"]
            ),
        ]

    combined = counts["combined_active_arms"]
    lines += [
        "",
        "## Trade / no-trade counts",
        "",
        "| arm | trades | no-trades | proposals | rolls | stops | sightings |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm, bucket in counts["by_arm"].items():
        lines.append(
            f"| {arm} | {bucket['trades']} | {bucket['no_trades']} | "
            f"{bucket['proposals']} | {bucket['rolls']} | {bucket['stops']} | "
            f"{bucket['noshots']} |"
        )
    rate = combined["trades_per_month"]
    lines += [
        "",
        f"B+C combined: {combined['trades']} trade(s), {combined['noshots']} sighting(s) "
        f"walked away from. Frequency: "
        + (
            f"{rate:.2f} trades/month"
            if rate is not None
            else "n/a (window too short to annualise)"
        )
        + f" (pre-registered expectation: < {EXPECTED_TRADES_PER_MONTH:.0f}/month).",
    ]

    lines += ["", "## Checklist-failure histogram (which conditions veto most)", ""]
    if not hist["sufficient"]:
        lines.append(f"_{hist['reason']}._")
    else:
        lines.append(
            f"Sources: {hist['sources']['no_trade_decisions']} NO_TRADE decision(s), "
            f"{hist['sources']['noshots']} sighting row(s)."
        )
        lines += ["", "| condition | vetoes | pending |", "| --- | --- | --- |"]
        for cid, count in hist["by_condition"].items():
            lines.append(f"| {cid} | {count} | {hist['pending_by_condition'].get(cid, 0)} |")
        for cid, count in hist["pending_by_condition"].items():
            if cid not in hist["by_condition"]:
                lines.append(f"| {cid} | 0 | {count} |")
        lines += ["", f"_{hist['grouping_note']}_"]

    lines += ["", "## Counterfactual ledger (discipline saving us, or costing us)", ""]
    if not cf["sufficient"]:
        lines.append(f"_{cf['reason']}._")
    else:
        proxy = cf["proxy_delta_weighted"]
        unmod = cf["unmodelled"]
        lines += [
            f"- Realized P&L on the same arms: {_money(cf['realized_pnl_same_arms'])}",
            f"- Proxy counterfactuals (arm C, delta-weighted): "
            f"{_money(proxy['total_usd'])} over {proxy['count']} row(s)",
            f"- Unmodelled (arm B 0DTE, underlying session only): {unmod['count']} row(s) "
            f"— {unmod['directional']['sessions_closing_beyond_projection']} closed beyond "
            f"the projected strike, "
            f"{unmod['directional']['sessions_closing_inside_projection']} closed inside",
            f"- Delta (proxy basis only): {_money(cf['delta_usd'])}",
            "",
            f"**{cf['verdict']}**",
            "",
            f"_{cf['honesty_note']}_",
        ]

    lines += [
        "",
        "## Pre-registered predictions (whole experiment window, not just this week)",
        "",
    ]
    if not card["window"].get("since") and not card["window"].get("until"):
        lines.append(f"_{card['reason']}._")
    else:
        win = card["window"]
        lines.append(
            f"Window: {win.get('since')} → {win.get('until')} "
            f"({win.get('days')} day(s), {win.get('months'):.2f} month(s))"
            if win.get("months")
            else f"Window: {win.get('since')} → {win.get('until')}"
        )
        lines += ["", "| # | prediction | status | evidence |", "| --- | --- | --- | --- |"]
        for pred in card["predictions"]:
            lines.append(
                f"| {pred['id']} | {pred['claim']} | **{pred['status']}** | {pred['evidence']} |"
            )
        tally = card["tally"]
        lines += [
            "",
            f"Tally: {tally['supported']} supported, {tally['falsified']} falsified, "
            f"{tally['insufficient_data']} not yet scorable.",
        ]

    shadow_status = shadow_roll.shadow_roll_status(store)
    lines += ["", "## Shadow roll (fixed quarterly roll, prediction #3's comparison arm)", ""]
    if not shadow_status["sufficient"]:
        lines.append("_No shadow leg has been opened yet — the comparison arm does not exist._")
    else:
        lines += [
            f"- Legs: {shadow_status['leg_count']} "
            f"({shadow_status['realized_legs']} rolled, leg "
            f"{shadow_status['open_leg_id']} open)",
            f"- Capital deployed: {_money(shadow_status['capital_deployed_usd'])}",
            f"- P&L to date: {_money(shadow_status['total_pnl_usd'])} "
            f"({_pct(shadow_status['return_on_capital'])} on capital deployed)",
            f"- Last mark: {shadow_status['last_mark_date']}",
            "",
            f"_{shadow_status['note']}_",
        ]

    lines += [
        "",
        "---",
        "Generated from the journal only. Exception-path P&L is reported separately by "
        "design; proxy and real counterfactuals are never summed together.",
        "",
    ]
    return "\n".join(lines)


def monthly_review_bullets(store: Any) -> list[str]:
    """The monthly review's opening bullets (brief: 'Monthly review: operator + agents').

    Same numbers as the weekly rollup, but whole-window and as short sentences — the
    rule-change discussion that follows needs the deltas, not a second rendering of
    the tables.
    """
    card = prediction_scorecard(store)
    pnl = arm_pnl(store, card["window"].get("since"), card["window"].get("until"))
    cf = counterfactual_delta(store, card["window"].get("since"), card["window"].get("until"))
    exc = pnl["exception_path"]
    bullets = [
        f"Window {card['window'].get('since')} → {card['window'].get('until')} "
        f"({card['window'].get('days')} days).",
        f"A {_money(pnl['arms']['A']['realized_pnl'])} · "
        f"B {_money(pnl['arms']['B']['realized_pnl'])} · "
        f"C {_money(pnl['arms']['C']['realized_pnl'])} · "
        f"exception path {_money(exc['realized_pnl'])} (separate).",
        f"Predictions: {card['tally']['supported']} supported, "
        f"{card['tally']['falsified']} falsified, "
        f"{card['tally']['insufficient_data']} not yet scorable.",
        f"Counterfactual verdict: {cf['verdict']}",
    ]
    for pred in card["predictions"]:
        if pred["status"] != STATUS_SUPPORTED:
            bullets.append(f"Prediction {pred['id']} — {pred['status']}: {pred['evidence']}")
    return bullets
