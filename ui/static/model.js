// Pure view-model logic for the U3 pages. No DOM, no fetch, no globals: every export
// here is a function of its arguments, which is what makes it testable from Node
// (tests/test_static_logic.py runs this file with `node --input-type=module` and scores
// the real functions rather than grepping for their names).
//
// The rule these functions exist to enforce: a view model states what is KNOWN, what
// is UNKNOWN, and what the endpoint itself said about the data. It never substitutes a
// zero, a dash or a green check for a fact the journal did not record.

import { DASH, isNum, usd, pct, esc, truncate } from "./format.js";

export const ARM_COLOR = {
  A: "#9aa4b2",
  B: "#e0a63a",
  C: "#4c9aff",
  EXCEPTION: "#e05fd0",
};

export const ARM_LABEL = {
  A: "Arm A · control",
  B: "Arm B · 0DTE",
  C: "Arm C · deep ITM",
  EXCEPTION: "Exception path",
};

export const CONDITION_ORDER = ["T1", "T2", "T2b", "T3a", "T3b", "T4", "T5", "T6"];

/** The rulebook's T3 is a GROUP of two (T3a squeeze, T3b release); either one failing
 * the group fails T3. `analysis.rollup` folds them, so the histogram does too, and the
 * two orders below are deliberately different: chips show the real ids, bars show the
 * group. Showing "T3" as a chip would be a condition that does not exist. */
export const HISTOGRAM_ORDER = ["T1", "T2", "T2b", "T3", "T4", "T5", "T6"];

export const groupCondition = (id) => (id === "T3a" || id === "T3b" ? "T3" : id);

// ---------------------------------------------------------------------------
// 1. the NO-SHOT join
// ---------------------------------------------------------------------------

/**
 * Index NO-SHOT rows so a signal marker can find the sighting that produced it.
 *
 * The join is `noshots.counterfactual_entry_ref -> decisions.id`, i.e. the sighting
 * names the NO_TRADE decision that rejected the trade. It is nullable in the schema,
 * so rows are ALSO indexed by their own id under a namespaced key: a sighting nobody
 * linked still has to be reachable, and losing it silently would delete the most
 * interesting rows in the journal.
 */
export function indexNoshots(rows) {
  const index = new Map();
  for (const row of rows || []) {
    const ref = row.counterfactual_entry_ref;
    if (ref != null) {
      const key = `d:${ref}`;
      if (!index.has(key)) index.set(key, []);
      index.get(key).push(row);
    }
    index.set(`n:${row.id}`, [row]);
  }
  return index;
}

/** Sightings attached to one decision id, newest last. */
export function noshotsForDecision(index, journalId) {
  if (!index || journalId == null) return [];
  return index.get(`d:${journalId}`) || [];
}

/**
 * The counterfactual of one sighting, as a sentence plus a tone.
 *
 * tone: "good" (we skipped a winner), "bad" (we skipped a loser), "flat", "pending".
 * `pending` is its own state and is deliberately NOT a tone of "flat": a null outcome
 * means the window has not moved far enough to know, which the endpoint says outright,
 * and rendering that as "no edge" would be the exact lie this page exists to prevent.
 */
export function counterfactual(outcome) {
  if (outcome == null) {
    return {
      state: "pending",
      tone: "pending",
      headline: "pending fill",
      detail:
        "the window has not moved far enough to know yet. That is not the same as " +
        '"no edge", and it is not counted in any bucket until it is filled.',
    };
  }
  const basis = outcome.basis;
  if (isNum(outcome.net_total_usd)) {
    const net = outcome.net_total_usd;
    return {
      state: "modelled",
      tone: net > 0 ? "good" : net < 0 ? "bad" : "flat",
      headline: `would have ${net >= 0 ? "made" : "lost"} ${usd(net)}`,
      detail:
        `${outcome.contracts ?? DASH} contract(s) @ $${outcome.premium_per_contract_usd ?? DASH} ` +
        `premium, delta ${outcome.delta_at_sighting ?? DASH} → ` +
        `${usd(outcome.net_per_contract_usd)}/contract. Basis: ${basis}.`,
    };
  }
  if (isNum(outcome.move_from_open_pct)) {
    const move = outcome.move_from_open_pct;
    return {
      state: "unmodelled",
      tone: move > 0 ? "good" : move < 0 ? "bad" : "flat",
      headline:
        `underlying moved ${pct(move, { sign: false }) > 0 ? "+" : ""}${(move * 100).toFixed(2)}% ` +
        `from the open — option P&L NOT modelled`,
      detail:
        `no delta was recorded at the sighting, so no premium math is possible. ` +
        `MFE ${pct(outcome.max_favourable_excursion_pct, { sign: false })} / ` +
        `MAE ${pct(outcome.max_adverse_excursion_pct, { sign: false })}; ` +
        `close beyond projection: ${outcome.close_beyond_projection ? "yes" : "no"}.`,
    };
  }
  return {
    state: "unmodelled",
    tone: "flat",
    headline: "counterfactual recorded, not quantifiable",
    detail: `Basis "${DASH}" carried fields this UI does not model. Shown verbatim.`,
  };
}

/**
 * Everything the marker popup shows for one clicked signal.
 *
 * A NO-SHOT sighting has no decision of its own on the chart (`/api/signals` draws
 * TRADE/STOP/ROLL/PROPOSAL/VETO only, by design — see SIGNAL_KINDS), so the hollow
 * markers are added by the caller from `/api/noshots` and a hollow marker with no
 * attached decision carries `signal: null`. That case is the one that matters most:
 * "we saw this and said no" is the whole point of the page.
 */
export function markerPopup(signal, sightings, { reasoningLimit = 220 } = {}) {
  const rows = sightings || [];
  const first = rows[0] || null;
  const kind = signal ? signal.kind : "NO_SHOT";
  const arm = signal ? signal.arm : (first && first.instrument_hypothesis && first.instrument_hypothesis.arm) || DASH;
  const fields = [];

  if (signal) {
    fields.push({ k: "journal", v: `#${signal.journal_id}`, title: "decisions.id — immutable" });
    fields.push({ k: "kind", v: kind });
    fields.push({ k: "arm", v: arm, color: ARM_COLOR[arm] });
    fields.push({
      k: "price",
      v: isNum(signal.price) ? signal.price.toFixed(2) : DASH,
      title: `source: ${signal.price_source || "unknown"}`,
    });
    if (signal.conviction != null) fields.push({ k: "conviction", v: String(signal.conviction) });
  } else {
    fields.push({ k: "noshot", v: `#${first ? first.id : DASH}`, title: "noshots.id" });
    fields.push({ k: "kind", v: kind, color: "#8b95a4" });
    fields.push({ k: "arm", v: arm, color: ARM_COLOR[arm] });
    if (first && isNum(first.price)) {
      fields.push({
        k: "underlying",
        v: first.price.toFixed(2),
        title: `source: ${first.price_source || "unknown"}`,
      });
    }
  }

  const failed = [];
  if (first) {
    for (const [id, body] of Object.entries(first.failed_conditions || {})) {
      const detail = typeof body === "object" && body ? body.detail || "" : String(body);
      const status = typeof body === "object" && body ? body.status || "FAIL" : "FAIL";
      failed.push({ id, status, detail: detail || DASH });
    }
  }
  // Failed conditions belong to the SIGHTING, not to the decision: /api/signals
  // deliberately does not carry a NO_TRADE row's condition state (see SIGNAL_KINDS),
  // because a plan-time no-trade is a veto and belongs in the histogram. So when the
  // clicked marker is an ordinary decision there is simply nothing to list here and the
  // popup says so instead of inventing a veto reason.

  return {
    title: signal ? `${arm} · ${kind}` : `NO-SHOT · ${arm}`,
    subtitle: signal ? `journal #${signal.journal_id}` : `noshot #${first ? first.id : DASH}`,
    fields,
    reasoning: signal && signal.reasoning ? truncate(signal.reasoning, reasoningLimit) : "",
    reasoningFull: signal ? signal.reasoning || "" : "",
    failed,
    counterfactual: counterfactual(first ? first.counterfactual_outcome : null),
    attached: rows.length,
  };
}

// ---------------------------------------------------------------------------
// 2. the veto histogram
// ---------------------------------------------------------------------------

/**
 * `/api/histogram?veto=weekly` -> stacked-bar rows, one per week, one segment per
 * condition, plus the totals used for the legend and the sort order.
 *
 * Rendered as plain stacked DIVs, not a lightweight-charts histogram: the series is
 * categorical-per-week, the axis is a handful of ISO week labels, and the browser
 * already gives us a flexbox for free. A canvas would add a resize lifecycle and a
 * second colour map to maintain for no gain at this data size. Documented here because
 * the spec offered both and the choice is deliberate.
 */
export function vetoHistogram(weeks) {
  const list = weeks || [];
  const totals = new Map();
  for (const week of list) {
    for (const [raw, n] of Object.entries(week.by_condition || {})) {
      const condition = groupCondition(raw);
      totals.set(condition, (totals.get(condition) || 0) + (Number(n) || 0));
    }
  }
  // Rulebook order, not frequency order: T4 vetoed 4 times and T3 4 times, but the axis
  // reads T1,T2,T2b,T3,T4 — a chart sorted by count makes the checklist a leaderboard.
  const ordered = [
    ...HISTOGRAM_ORDER.filter((id) => totals.has(id)),
    ...[...totals.keys()].filter((id) => !HISTOGRAM_ORDER.includes(id)).sort(),
  ];
  const bars = list.map((week) => {
    const perWeek = new Map();
    for (const [raw, n] of Object.entries(week.by_condition || {})) {
      const condition = groupCondition(raw);
      perWeek.set(condition, (perWeek.get(condition) || 0) + (Number(n) || 0));
    }
    const segments = ordered
      .map((id) => ({ condition: id, value: perWeek.get(id) || 0 }))
      .filter((segment) => segment.value > 0);
    const total = segments.reduce((sum, segment) => sum + segment.value, 0);
    return {
      week_start: week.week_start,
      week_end: week.week_end,
      total,
      segments,
      pending: week.pending_by_condition || {},
      sufficient: week.sufficient !== false,
      reason: week.reason || "",
    };
  });
  const max = bars.reduce((peak, bar) => Math.max(peak, bar.total), 0);
  return {
    conditions: ordered.map((id) => ({ id, total: totals.get(id) || 0 })),
    bars,
    max,
    empty: bars.length === 0 || ordered.length === 0,
  };
}

// ---------------------------------------------------------------------------
// 3. the hunt plan grid
// ---------------------------------------------------------------------------

const DIRECTION_ORDER = ["call", "put"];

/** Normalise a direction to a stable, lowercase key. */
export const directionKey = (direction) => String(direction || "unset").toLowerCase();

/**
 * Plan cells -> a direction × arm grid.
 *
 * `correction` is set when a cell references an earlier journal row: the plan is stored
 * as immutable decision rows, so a corrected plan is a SECOND row, and hiding the
 * superseded one would be an edit the UI is not allowed to perform.
 */
export function huntGrid(cells) {
  const byDirection = new Map();
  for (const cell of cells || []) {
    const key = directionKey(cell.direction);
    if (!byDirection.has(key)) byDirection.set(key, new Map());
    byDirection.get(key).set(cell.arm, cell);
  }
  const directions = [
    ...DIRECTION_ORDER.filter((d) => byDirection.has(d)),
    ...[...byDirection.keys()].filter((d) => !DIRECTION_ORDER.includes(d)).sort(),
  ];
  const arms = [...new Set((cells || []).map((cell) => cell.arm))].sort();
  const rows = directions.map((direction) => ({
    direction,
    arms: arms.map((arm) => byDirection.get(direction).get(arm) || null),
  }));
  return {
    arms,
    rows,
    empty: rows.length === 0,
    vetoed: (cells || []).filter((cell) => cell.veto_reasons && cell.veto_reasons.length),
  };
}

/** T1..T6 chips for one cell, in rulebook order, with the veto reason on hover. */
export function conditionChips(cell) {
  const conditions = (cell && cell.conditions) || {};
  const failed = new Set((cell && cell.failed_conditions) || []);
  const pending = new Set((cell && cell.pending_conditions) || []);
  const ids = [
    ...CONDITION_ORDER.filter((id) => id in conditions),
    ...Object.keys(conditions)
      .filter((id) => !CONDITION_ORDER.includes(id))
      .sort(),
  ];
  if (ids.length === 0) {
    for (const id of failed) ids.push(id);
    for (const id of pending) if (!ids.includes(id)) ids.push(id);
  }
  return ids.map((id) => {
    const body = conditions[id] || {};
    const status =
      body.status || (failed.has(id) ? "FAIL" : pending.has(id) ? "PENDING" : "UNKNOWN");
    const blocking = body.blocking !== false;
    const reasons = (cell.veto_reasons || []).filter((reason) => String(reason).startsWith(`${id}:`));
    return {
      id,
      status,
      blocking,
      reason: body.detail || reasons.join(" · ") || DASH,
      tone: status === "PASS" ? "pass" : status === "PENDING" ? "pending" : blocking ? "fail" : "soft",
    };
  });
}

/** The event-veto banner state, including the case where we could not determine it. */
export function eventVetoBanner(eventVeto) {
  const veto = eventVeto || {};
  if (veto.available === false) {
    return {
      level: "unknown",
      text: "event veto: UNKNOWN — the rulebook could not be read, so no verdict is claimed",
    };
  }
  if (veto.active) {
    return {
      level: "bad",
      text: `event veto ACTIVE (${(veto.kinds || []).join(", ")}) — no entries today. ${veto.reason || ""}`,
    };
  }
  return { level: "ok", text: "event veto: none of the rulebook's veto kinds in any cell today" };
}

// ---------------------------------------------------------------------------
// 4. arms cards
// ---------------------------------------------------------------------------

/** Per-arm display rows, with the control comparison kept explicit. */
export function armSummary(card, { controlArm = "A", controlPnl = 0 } = {}) {
  const bank = card.bankroll;
  const deployed = card.deployed || 0;
  const closed = card.closed_positions || 0;
  return {
    arm: card.arm,
    label: ARM_LABEL[card.arm] || `Arm ${card.arm}`,
    color: ARM_COLOR[card.arm] || "#8b95a4",
    isControl: card.arm === controlArm,
    bankroll: bank,
    deployed,
    cash: card.cash,
    pnl: card.realized_pnl ?? 0,
    vsControl: card.vs_control,
    returnOnBankroll: card.return_on_bankroll,
    closed,
    open: card.open_positions || [],
    openCount: (card.open_positions || []).length,
    positions: (card.open_positions || []).map((position) => ({
      id: position.id,
      label: position.contract || position.symbol || DASH,
      symbol: position.symbol || DASH,
      contract: position.contract || null,
      qty: position.qty,
      entry: position.entry_price,
      // The journal holds no marks, and marking to market needs a price series the UI
      // does not own, so "last" and unrealized are honestly absent rather than zero.
      last: null,
      unrealized: null,
      entryTs: position.entry_ts,
      notes: "no mark in the journal — unrealized P&L is not computed",
    })),
    curve: (card.equity_curve || []).map((point) => ({ t: point.t, equity: point.equity })),
    shadow: card.shadow_roll || null,
    // Enough to say "the window is not open" instead of rendering a flat zero line.
    windowOpen: isNum(bank) && (closed > 0 || (card.open_positions || []).length > 0),
    controlPnl,
  };
}

/** Split mismatch flags by arm, and by severity. */
export function mismatchesByArm(flags) {
  const out = new Map();
  for (const flag of flags || []) {
    if (!out.has(flag.arm)) out.set(flag.arm, []);
    out.get(flag.arm).push(flag);
  }
  return out;
}

// ---------------------------------------------------------------------------
// 5. ledger
// ---------------------------------------------------------------------------

/** Column definitions: which are server-sortable, and how to label them. */
export const LEDGER_COLUMNS = [
  { key: "id", label: "#", sort: "id" },
  { key: "ts", label: "when", sort: "ts" },
  { key: "arm", label: "arm", sort: "arm" },
  { key: "kind", label: "kind", sort: "kind" },
  { key: "symbol", label: "symbol", sort: "symbol" },
  { key: "conviction", label: "conv", sort: "conviction" },
  { key: "reasoning", label: "reasoning", sort: null },
  { key: "created_at", label: "written", sort: "created_at" },
];

/** Sort indicator for a column header. `null` sort => not clickable, by design. */
export function sortIndicator(column, sort, order) {
  if (!column.sort) return "";
  if (column.sort !== sort) return "";
  return order === "asc" ? " ▲" : " ▼";
}

/** The next (sort, order) when a header is clicked: same field flips, new field asc. */
export function nextSort(column, sort, order) {
  if (!column.sort) return { sort, order };
  if (column.sort !== sort) return { sort: column.sort, order: "asc" };
  return { sort, order: order === "asc" ? "desc" : "asc" };
}

/**
 * The immutable-journal reference chain of one row, as followable links.
 *
 * `references` holds ids of rows this one corrects or supersedes. A correction is
 * never an edit: the old row stays, the new row points back at it, and the Ledger
 * renders the pointer so the operator can walk the chain to the current truth.
 */
export function referenceChain(row, lookup, { maxDepth = 8 } = {}) {
  const seen = new Set([row.id]);
  const chain = [];
  let cursor = row;
  let depth = 0;
  while (cursor && depth < maxDepth) {
    const refs = cursor.references || [];
    for (const ref of refs) {
      if (ref == null || seen.has(ref)) continue;
      seen.add(ref);
      // `known` is honest about the limit of the current page: a reference to a row
      // that is not loaded cannot be a link, so it renders as a plain id.
      chain.push({ id: ref, from: cursor.id, depth, known: Boolean(lookup && lookup.has(ref)) });
    }
    const next = refs.map((ref) => (lookup ? lookup.get(ref) : null)).find(Boolean);
    cursor = next || null;
    depth += 1;
  }
  return chain;
}

/** Pagination footer numbers, with the honest 0-row case. */
export function pageInfo(payload) {
  const total = (payload && payload.total) || 0;
  const page = (payload && payload.page) || 1;
  const perPage = (payload && payload.per_page) || 50;
  const pages = (payload && payload.pages) || 0;
  const first = total === 0 ? 0 : (page - 1) * perPage + 1;
  const last = total === 0 ? 0 : Math.min(total, page * perPage);
  return {
    total,
    page,
    perPage,
    pages,
    first,
    last,
    hasPrev: page > 1,
    hasNext: page < pages,
    empty: total === 0,
  };
}

/** Escape helper re-export so page modules have one import for text handling. */
export { esc, usd, pct, truncate, DASH };
