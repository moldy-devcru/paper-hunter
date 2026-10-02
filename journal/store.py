"""Append-only decision journal — thin SQLite wrapper around journal/schema.sql.

Scope note (Phase 1a): this module owns the ledger and nothing else. Indicator
computation, checklist evaluation and order placement belong to ``executor/``, which
does not exist yet.

Design rules, straight from docs/brief.md "Journaling & review":
  * every decision is an immutable entry written at decision time;
  * post-hoc edits are forbidden — corrections are NEW rows referencing the old;
  * the snapshot stores ALL checklist values, not just the deciding ones.

The last point is why ``checklist_snapshot`` is typed as ``dict[str, Any]`` rather
than a strict model: the checklist gains conditions (T6 was added 2026-09-29) as the
rulebook is versioned, and a frozen journal must not become unreadable because the
code stopped recognising a condition that existed at decision time.

Python 3.12+, stdlib sqlite3 + pydantic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

Arm = Literal["A", "B", "C", "EXCEPTION"]
DecisionKind = Literal["TRADE", "NO_TRADE", "ROLL", "STOP", "PROPOSAL", "VETO"]
PositionStatus = Literal["OPEN", "CLOSED"]

CONVICTION_MIN = 1
CONVICTION_MAX = 10


class AppendOnlyViolation(RuntimeError):
    """Raised when the append-only guarantee is violated (UPDATE/DELETE on the journal)."""


def utcnow() -> str:
    """UTC ISO-8601 with a trailing Z. The journal's only clock."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dumps(value: Any) -> str:
    """Canonical JSON. sort_keys keeps hashes/reports stable across runs."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class DecisionEntry(BaseModel):
    """One immutable decision. ``id``/``created_at`` are assigned by the store on append."""

    model_config = ConfigDict(frozen=True)

    ts: str
    arm: Arm
    kind: DecisionKind
    checklist_snapshot: dict[str, Any] = Field(
        description="ALL indicator values at decision time, not just deciding ones"
    )
    checklist_state: dict[str, Any] = Field(
        description="per-condition pass/fail plus veto reasons, e.g. "
        "{'T4': {'pass': False, 'reason': 'rvol 1.2 < 1.5'}}"
    )
    reasoning: str
    strategy_version: str
    symbol: str | None = None
    conviction: int | None = Field(default=None, ge=CONVICTION_MIN, le=CONVICTION_MAX)
    references: list[int] = Field(
        default_factory=list,
        description="ids of prior decisions this entry corrects or annotates",
    )
    id: int | None = None
    created_at: str | None = None

    @field_validator("ts")
    @classmethod
    def _ts_is_utc_iso(cls, v: str) -> str:
        # Naive or non-ISO timestamps silently sort wrong. Require an explicit offset.
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class NoshotEntry(BaseModel):
    """A sighting we walked away from, plus the counterfactual tracked later.

    ``counterfactual_outcome`` is the one field that may be filled in after the fact
    (schema trigger allows exactly that transition). It stays here rather than in a
    decisions row because it is not a decision: nothing was decided, it is a
    measurement of a trade that does not exist.
    """

    model_config = ConfigDict(frozen=True)

    ts: str
    date: str
    instrument_hypothesis: dict[str, Any] = Field(
        description="what trade WOULD have been taken (arm, side, strike, expiry, size)"
    )
    failed_conditions: dict[str, Any]
    indicator_values: dict[str, Any]
    counterfactual_entry_ref: int | None = None
    counterfactual_outcome: dict[str, Any] | None = None
    id: int | None = None
    created_at: str | None = None

    @field_validator("date")
    @classmethod
    def _date_shape(cls, v: str) -> str:
        datetime.strptime(v, "%Y-%m-%d")
        return v


class Position(BaseModel):
    """A simulated fill. Mirrors (never replaces) the TRADE/STOP/ROLL decisions."""

    model_config = ConfigDict(frozen=True)

    arm: Arm
    symbol: str
    entry_ts: str
    entry_price: float
    qty: float
    status: PositionStatus = "OPEN"
    contract: str | None = None
    exit_ts: str | None = None
    exit_price: float | None = None
    pnl: float | None = None
    notes: str | None = None
    id: int | None = None


class FlowBaselineRow(BaseModel):
    """One session's deep-OTM 0DTE flow aggregate (see ``flow_baseline`` in schema.sql).

    ``baseline_*`` and ``ratio_*`` are allowed to be ``None`` and are filled by the
    writer from PRIOR rows, never from the row being written: the brief's T6 is
    "today >= N x 20-day baseline", so a session that counts itself in its own
    baseline is a session that can never show a spike.
    """

    model_config = ConfigDict(frozen=True)

    date: str
    underlying: str
    feed: str
    is_delayed: bool
    session_spot: float
    deep_otm_threshold_pct: float
    deep_otm_call_volume: float
    deep_otm_put_volume: float
    call_volume_by_distance: dict[str, float]
    put_volume_by_distance: dict[str, float]
    strategy_version: str
    ts: str
    chain_contracts: int = 0
    zero_dte_expiry: str | None = None
    deep_otm_total_volume: float = 0.0
    baseline_lookback_days: int = 20
    baseline_days: int = 0
    baseline_call_mean: float | None = None
    baseline_put_mean: float | None = None
    ratio_call: float | None = None
    ratio_put: float | None = None
    id: int | None = None
    created_at: str | None = None

    @field_validator("date")
    @classmethod
    def _date_shape(cls, v: str) -> str:
        datetime.strptime(v, "%Y-%m-%d")
        return v


# ---------------------------------------------------------------------------
# connection + init
# ---------------------------------------------------------------------------


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on and row access by name."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path, schema_path: str | Path | None = None) -> sqlite3.Connection:
    """Create the schema if absent. Idempotent."""
    conn = connect(db_path)
    sql = Path(schema_path or SCHEMA_PATH).read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def append_decision(conn: sqlite3.Connection, entry: DecisionEntry) -> int:
    """Insert one decision row. Returns its id."""
    if entry.id is not None or entry.created_at is not None:
        raise ValueError("id/created_at are assigned by the store, not the caller")
    cur = conn.execute(
        """
        INSERT INTO decisions (
            ts, arm, kind, symbol, checklist_snapshot, checklist_state,
            reasoning, conviction, strategy_version, "references", created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.ts,
            entry.arm,
            entry.kind,
            entry.symbol,
            _dumps(entry.checklist_snapshot),
            _dumps(entry.checklist_state),
            entry.reasoning,
            entry.conviction,
            entry.strategy_version,
            _dumps(entry.references),
            entry.created_at or utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def append_noshot(conn: sqlite3.Connection, entry: NoshotEntry) -> int:
    """Insert one NO-SHOT row. Returns its id."""
    if entry.id is not None or entry.created_at is not None:
        raise ValueError("id/created_at are assigned by the store, not the caller")
    cur = conn.execute(
        """
        INSERT INTO noshots (
            ts, date, instrument_hypothesis, failed_conditions, indicator_values,
            counterfactual_entry_ref, counterfactual_outcome, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.ts,
            entry.date,
            _dumps(entry.instrument_hypothesis),
            _dumps(entry.failed_conditions),
            _dumps(entry.indicator_values),
            entry.counterfactual_entry_ref,
            _dumps(entry.counterfactual_outcome)
            if entry.counterfactual_outcome is not None
            else None,
            entry.created_at or utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_counterfactual_outcome(
    conn: sqlite3.Connection, noshot_id: int, outcome: dict[str, Any]
) -> None:
    """Fill the one mutable column. The schema trigger blocks every other mutation."""
    if outcome is None:
        raise ValueError("outcome must be a payload; to leave it unset, don't call this")
    cur = conn.execute(
        "UPDATE noshots SET counterfactual_outcome = ? WHERE id = ?",
        (_dumps(outcome), noshot_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"no noshot row {noshot_id}")
    conn.commit()


def open_position(conn: sqlite3.Connection, position: Position) -> int:
    """Record an open simulated position. Returns its id."""
    if position.status != "OPEN":
        raise ValueError("open_position requires status='OPEN'")
    cur = conn.execute(
        """
        INSERT INTO positions (
            arm, symbol, contract, entry_ts, entry_price, qty, status, notes
        ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """,
        (
            position.arm,
            position.symbol,
            position.contract,
            position.entry_ts,
            position.entry_price,
            position.qty,
            position.notes,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def close_position(
    conn: sqlite3.Connection,
    position_id: int,
    *,
    exit_ts: str,
    exit_price: float,
    pnl: float | None = None,
    notes: str | None = None,
) -> None:
    """Close a position. If pnl is omitted it is computed as (exit-entry) * qty.

    Ambiguity note: the brief never fixes the pnl convention. These are paper
    SPY/option fills with no fees or slippage modelled in Phase 1a, so the simple
    price-difference convention is used and the caller may override it explicitly
    once fee/slippage modelling lands.
    """
    row = conn.execute(
        "SELECT entry_price, qty, status FROM positions WHERE id = ?", (position_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no position {position_id}")
    if row["status"] != "OPEN":
        raise ValueError(f"position {position_id} is already {row['status']}")
    if pnl is None:
        pnl = (exit_price - row["entry_price"]) * row["qty"]
    cur = conn.execute(
        """
        UPDATE positions
           SET status = 'CLOSED', exit_ts = ?, exit_price = ?, pnl = ?,
               notes = COALESCE(?, notes)
         WHERE id = ?
        """,
        (exit_ts, exit_price, pnl, notes, position_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"no position {position_id}")
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Upsert a meta key. Known keys: strategy_version, window_start, arm_bankroll."""
    conn.execute(
        """
        INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, _dumps(value), utcnow()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# read-only query helpers
# ---------------------------------------------------------------------------


def _decision_from_row(row: sqlite3.Row) -> DecisionEntry:
    data = dict(row)
    for field in ("checklist_snapshot", "checklist_state", "references"):
        data[field] = _loads(data[field])
    return DecisionEntry(**data)


def _noshot_from_row(row: sqlite3.Row) -> NoshotEntry:
    data = dict(row)
    for field in (
        "instrument_hypothesis",
        "failed_conditions",
        "indicator_values",
        "counterfactual_outcome",
    ):
        data[field] = _loads(data[field])
    return NoshotEntry(**data)


def get_decision(conn: sqlite3.Connection, decision_id: int) -> DecisionEntry | None:
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return _decision_from_row(row) if row else None


def list_decisions(
    conn: sqlite3.Connection,
    *,
    arm: Arm | None = None,
    kind: DecisionKind | None = None,
    since: str | None = None,
) -> list[DecisionEntry]:
    clauses, params = [], []
    if arm is not None:
        clauses.append("arm = ?")
        params.append(arm)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM decisions{where} ORDER BY ts, id", params).fetchall()
    return [_decision_from_row(r) for r in rows]


def list_noshots(conn: sqlite3.Connection, *, date: str | None = None) -> list[NoshotEntry]:
    if date is None:
        rows = conn.execute("SELECT * FROM noshots ORDER BY date, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM noshots WHERE date = ? ORDER BY id", (date,)).fetchall()
    return [_noshot_from_row(r) for r in rows]


def get_position(conn: sqlite3.Connection, position_id: int) -> Position | None:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    return Position(**dict(row)) if row else None


def list_positions(
    conn: sqlite3.Connection, *, arm: Arm | None = None, status: PositionStatus | None = None
) -> list[Position]:
    clauses, params = [], []
    if arm is not None:
        clauses.append("arm = ?")
        params.append(arm)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM positions{where} ORDER BY entry_ts, id", params).fetchall()
    return [Position(**dict(r)) for r in rows]


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else _loads(row["value"])


# ---------------------------------------------------------------------------
# flow_baseline — the T6 numerator/baseline accumulator (executor/soak.py writer)
# ---------------------------------------------------------------------------


class DuplicateFlowBaseline(RuntimeError):
    """A row already exists for this session date.

    A distinct exception rather than a silent no-op: the caller (the soak job) turns
    it into a "already recorded" summary line and exit 0, while any *other* caller
    that hits it has a bug worth seeing. The table is append-only and ``date`` is
    UNIQUE, so there is no legitimate path that should be writing a second row.
    """


def append_flow_baseline(conn: sqlite3.Connection, entry: FlowBaselineRow) -> int:
    """Insert one session's flow row. Returns its id.

    Raises :class:`DuplicateFlowBaseline` when the session already has one. Never
    updates — see the table's trigger comment.
    """
    if entry.id is not None or entry.created_at is not None:
        raise ValueError("id/created_at are assigned by the store, not the caller")
    try:
        cur = conn.execute(
            """
            INSERT INTO flow_baseline (
                ts, date, underlying, feed, is_delayed, session_spot, zero_dte_expiry,
                chain_contracts, deep_otm_threshold_pct,
                deep_otm_call_volume, deep_otm_put_volume, deep_otm_total_volume,
                call_volume_by_distance, put_volume_by_distance,
                baseline_lookback_days, baseline_days, baseline_call_mean,
                baseline_put_mean, ratio_call, ratio_put, strategy_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.ts,
                entry.date,
                entry.underlying.upper(),
                entry.feed,
                1 if entry.is_delayed else 0,
                entry.session_spot,
                entry.zero_dte_expiry,
                int(entry.chain_contracts),
                float(entry.deep_otm_threshold_pct),
                float(entry.deep_otm_call_volume),
                float(entry.deep_otm_put_volume),
                float(entry.deep_otm_total_volume),
                _dumps(entry.call_volume_by_distance),
                _dumps(entry.put_volume_by_distance),
                int(entry.baseline_lookback_days),
                int(entry.baseline_days),
                entry.baseline_call_mean,
                entry.baseline_put_mean,
                entry.ratio_call,
                entry.ratio_put,
                entry.strategy_version,
                entry.created_at or utcnow(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateFlowBaseline(
            f"flow_baseline already has a row for {entry.date}: {exc}"
        ) from None
    conn.commit()
    return int(cur.lastrowid)


def _flow_from_row(row: sqlite3.Row) -> FlowBaselineRow:
    data = dict(row)
    for field in ("call_volume_by_distance", "put_volume_by_distance"):
        data[field] = _loads(data[field])
    data["is_delayed"] = bool(data["is_delayed"])
    return FlowBaselineRow(**data)


def get_flow_baseline(conn: sqlite3.Connection, date: str) -> FlowBaselineRow | None:
    row = conn.execute("SELECT * FROM flow_baseline WHERE date = ?", (date,)).fetchone()
    return _flow_from_row(row) if row else None


def list_flow_baseline(
    conn: sqlite3.Connection, *, before: str | None = None, limit: int | None = None
) -> list[FlowBaselineRow]:
    """Flow rows oldest-first; ``before`` is an exclusive ISO date bound.

    The soak uses ``before=<this session>`` so the 20-day baseline is built from prior
    sessions only — the day being written is never in its own baseline.
    """
    clauses, params = [], []
    if before is not None:
        clauses.append("date < ?")
        params.append(before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order = " ORDER BY date ASC" + (f" LIMIT {int(limit)}" if limit else "")
    rows = conn.execute(f"SELECT * FROM flow_baseline{where}{order}", params).fetchall()
    return [_flow_from_row(r) for r in rows]
