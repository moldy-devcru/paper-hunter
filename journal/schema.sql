-- paper-hunter decision journal — schema
--
-- APPEND-ONLY BY CONSTRUCTION. This is the integrity core of the experiment.
-- The brief (docs/brief.md, "Journaling & review") requires: every decision is an
-- immutable entry written AT decision time; post-hoc edits are forbidden; corrections
-- are NEW entries that reference the entry they correct.
--
-- Enforcement is not a convention here, it is a trigger: any UPDATE or DELETE against
-- `decisions` raises. That makes the "pre-registered, honest" claim mechanically
-- checkable rather than a promise in a README. Dropping or disabling a trigger is
-- itself a falsification event and must be reported as one.
--
-- The single documented exception is `noshots.counterfactual_outcome` — see that
-- table's comment block. Everything else is write-once.
--
-- Timestamps: `ts` columns are UTC ISO-8601 with a trailing 'Z'. SQLite has no
-- native datetime type; text is the honest, sortable, timezone-explicit choice.
-- JSON payloads are stored as TEXT holding canonical JSON (validated by pydantic
-- at the store boundary, journal/store.py).

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- decisions: the immutable decision ledger.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,           -- UTC ISO-8601, decision time
    arm                 TEXT    NOT NULL CHECK (arm IN ('A', 'B', 'C', 'EXCEPTION')),
    kind                TEXT    NOT NULL CHECK (
                              kind IN ('TRADE', 'NO_TRADE', 'ROLL', 'STOP',
                                       'PROPOSAL', 'VETO')),
    symbol              TEXT,                      -- NULL for whole-market decisions
    -- FULL indicator snapshot: every checklist value (T1..T6, T2b, T3a...), not just
    -- the deciding ones. Rationale: you cannot tell later whether a skipped condition
    -- was near-miss or hopeless, and "which conditions veto most" (weekly rollup)
    -- requires the near-misses.
    checklist_snapshot  TEXT    NOT NULL,           -- JSON object
    -- Per-condition outcome + veto reasons, e.g. {"T4": {"pass": false, "reason": "rvol 1.2 < 1.5"}}.
    checklist_state     TEXT    NOT NULL,           -- JSON object
    reasoning           TEXT    NOT NULL,           -- stated AT decision time
    conviction          INTEGER CHECK (conviction IS NULL OR (conviction BETWEEN 1 AND 10)),
    strategy_version    TEXT    NOT NULL,           -- which frozen rulebook was in force
    -- Correction graph: a correcting entry lists the id(s) it supersedes/annotates.
    -- Append-only means "fixing" a decision is writing a new row, never editing.
    -- Column name is quoted everywhere: REFERENCES is a SQLite keyword.
    "references"       TEXT    NOT NULL DEFAULT '[]',  -- JSON array of decision ids
    created_at          TEXT    NOT NULL            -- UTC ISO-8601, row insertion time
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts  ON decisions (ts);
CREATE INDEX IF NOT EXISTS idx_decisions_arm ON decisions (arm, kind);

-- RAISE(ABORT, ...) makes the statement fail loudly and leaves the table untouched.
CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: decisions may not be updated');
END;

CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: decisions may not be deleted');
END;

-- ---------------------------------------------------------------------------
-- noshots: the counterfactual ledger ("the hunting part").
--
-- Every day the sights were on something and we did not shoot.
--
-- WHY THIS TABLE MAY BE UPDATED (the one documented exception):
-- `counterfactual_outcome` is inherently unknowable at decision time — you cannot
-- know what the rejected trade WOULD have done until the window has moved. The brief
-- requires that outcome tracked hypothetically, and fabricating it up front would be
-- dishonest. So: the row's identity and its evidence columns (failed_conditions,
-- indicator_values, instrument_hypothesis) are write-once, and only
-- counterfactual_outcome may be filled in later. That single mutable column is
-- enforced by trigger, not by convention — see noshots_immutable_but_outcome_open.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS noshots (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                          TEXT    NOT NULL,   -- UTC ISO-8601, observation time
    date                        TEXT    NOT NULL,   -- YYYY-MM-DD (ET session date)
    -- What trade WOULD have been taken: arm, kind, strike/expiry/side if relevant.
    instrument_hypothesis       TEXT    NOT NULL,   -- JSON object
    failed_conditions           TEXT    NOT NULL,   -- JSON: condition -> why it failed
    indicator_values            TEXT    NOT NULL,   -- JSON: full indicator snapshot
    -- Links a rejected trade to the decision entry that rejected it (the NO_TRADE row).
    counterfactual_entry_ref    INTEGER REFERENCES decisions (id),
    counterfactual_outcome      TEXT,              -- JSON, nullable, filled later
    created_at                  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_noshots_date ON noshots (date);

CREATE TRIGGER IF NOT EXISTS noshots_immutable_but_outcome_open
BEFORE UPDATE ON noshots
WHEN NOT (
    OLD.counterfactual_outcome IS NULL
    AND NEW.counterfactual_outcome IS NOT NULL
    AND NEW.id                  = OLD.id
    AND NEW.ts                  = OLD.ts
    AND NEW.date                = OLD.date
    AND NEW.instrument_hypothesis IS OLD.instrument_hypothesis
    AND NEW.failed_conditions  IS OLD.failed_conditions
    AND NEW.indicator_values   IS OLD.indicator_values
    AND NEW.counterfactual_entry_ref IS OLD.counterfactual_entry_ref
    AND NEW.created_at         IS OLD.created_at
)
BEGIN
    SELECT RAISE(ABORT,
        'noshots rows are immutable except for filling counterfactual_outcome once');
END;

CREATE TRIGGER IF NOT EXISTS noshots_no_delete
BEFORE DELETE ON noshots
BEGIN
    SELECT RAISE(ABORT, 'noshots may not be deleted');
END;

-- ---------------------------------------------------------------------------
-- positions: simulated fills. Mutable by nature (OPEN -> CLOSED) — every state
-- change is itself mirrored by a decisions row (kind = TRADE / STOP / ROLL), so this
-- table is a convenience view, never the record of truth.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    arm          TEXT    NOT NULL CHECK (arm IN ('A', 'B', 'C', 'EXCEPTION')),
    symbol       TEXT    NOT NULL,
    contract     TEXT,             -- e.g. "SPY260116C00685000"; NULL for Arm A (shares)
    entry_ts     TEXT    NOT NULL,
    entry_price  REAL    NOT NULL,
    qty          REAL    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    exit_ts      TEXT,
    exit_price   REAL,
    pnl          REAL,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
CREATE INDEX IF NOT EXISTS idx_positions_arm    ON positions (arm, status);

-- ---------------------------------------------------------------------------
-- shadow_roll_legs + shadow_roll_marks: the brief's "4th shadow-sim, costless".
--
-- Prediction #3 asks "do TA entries pick better roll points than a fixed quarterly
-- roll?" The comparison arm is a hypothetical, not a position: nothing is bought, no
-- premium is paid, no order exists. It lives in the journal anyway, under the same
-- append-only discipline as everything else, because a comparison that is not
-- recorded at decision time is a comparison that will be reconstructed from memory
-- once the results look interesting.
--
-- DESIGN NOTE (vocabulary honesty): the `decisions` table deliberately does NOT
-- carry these. Its `kind` vocabulary is TRADE/NO_TRADE/ROLL/STOP/PROPOSAL/VETO and
-- its CHECK constraint enforces that; a shadow roll is none of those (nothing was
-- traded, nothing rolled), and borrowing 'ROLL' for it would put a row in the
-- decision ledger that no executor could ever have written — the exact kind of
-- ambiguity this schema exists to prevent. Dedicated tables, same immutability.
--
-- A "roll" is expressed by lineage, not by mutating a leg: a new leg carries
-- `supersedes_leg_id`, and a leg is closed exactly when some later leg supersedes it.
-- So the whole shadow sim is insert-only — there is no UPDATE or DELETE path at all.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_roll_legs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,   -- UTC ISO-8601, write time
    -- ET session date the leg was opened on (the open of the quarterly expiry).
    opened_on           TEXT    NOT NULL,   -- YYYY-MM-DD
    -- The quarterly expiry this leg rolls INTO (3rd Friday of Mar/Jun/Sep/Dec).
    expiry              TEXT    NOT NULL,   -- YYYY-MM-DD
    underlying_close    REAL    NOT NULL,   -- SPY close on opened_on
    -- Size in CONTRACT UNITS, where 1 unit = 100 shares of notional. Expressing the
    -- shadow leg in contract units (not shares) is what makes the comparison with
    -- arm C honest: both sides are then measured as return on capital deployed.
    qty                 REAL    NOT NULL,   -- contract-equivalents, >= 0
    strategy_version    TEXT    NOT NULL,
    supersedes_leg_id   INTEGER REFERENCES shadow_roll_legs (id),
    notes               TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shadow_legs_opened ON shadow_roll_legs (opened_on);

CREATE TRIGGER IF NOT EXISTS shadow_roll_legs_no_update
BEFORE UPDATE ON shadow_roll_legs
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: shadow_roll_legs may not be updated');
END;

CREATE TRIGGER IF NOT EXISTS shadow_roll_legs_no_delete
BEFORE DELETE ON shadow_roll_legs
BEGIN
    SELECT RAISE(ABORT, 'shadow_roll_legs may not be deleted');
END;

CREATE TABLE IF NOT EXISTS shadow_roll_marks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,   -- UTC ISO-8601, mark time
    leg_id              INTEGER NOT NULL REFERENCES shadow_roll_legs (id),
    date                TEXT    NOT NULL,   -- YYYY-MM-DD (ET session date of the mark)
    underlying_close    REAL    NOT NULL,
    -- (close - entry_close) * 100 * qty, computed by the writer from the leg it
    -- belongs to and stored, so a reader never has to re-derive the notional basis.
    leg_pnl             REAL    NOT NULL,
    basis               TEXT    NOT NULL,   -- 'underlying_notional' — see module docstring
    notes               TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shadow_marks_leg ON shadow_roll_marks (leg_id, date);

CREATE TRIGGER IF NOT EXISTS shadow_roll_marks_no_update
BEFORE UPDATE ON shadow_roll_marks
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: shadow_roll_marks may not be updated');
END;

CREATE TRIGGER IF NOT EXISTS shadow_roll_marks_no_delete
BEFORE DELETE ON shadow_roll_marks
BEGIN
    SELECT RAISE(ABORT, 'shadow_roll_marks may not be deleted');
END;

-- ---------------------------------------------------------------------------
-- meta: key/value with a semantic guard on the three keys the brief cares about.
--   strategy_version  — append a row per frozen rulebook version
--   window_start      — the experiment's day-1 anchor
--   arm_bankroll      — JSON {"A": 10000.0, "B": 10000.0, "C": 10000.0}
-- Ambiguity note (brief is silent on shapes): meta is a generic k/v table; bankrolls
-- are stored as one JSON object under arm_bankroll rather than three rows, so the
-- $30k total is a single readable fact instead of a convention to reconstruct.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,   -- JSON-encoded scalar/object
    updated_at TEXT NOT NULL
);
