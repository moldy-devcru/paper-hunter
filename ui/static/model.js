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

/**
 * The event-veto banner state, including the case where we could not determine it.
 *
 * UX PASS 2 fix #3: the "clear" case now carries the SPECIFIC kinds the rulebook
 * declares, because "none of the rulebook's veto kinds" is a claim a reader cannot
 * check — it does not say which kinds were looked for, so it reads as a reassurance
 * with no content. Naming them ("none of fomc, cpi recorded") makes the confirmation
 * falsifiable at a glance, which is the whole point of a safety confirmation: it should
 * survive being demoted from a banner to a chip.
 */
export function eventVetoBanner(eventVeto, { kinds = [] } = {}) {
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
  const named = kinds.length ? `none of ${kinds.join(", ")} recorded` : null;
  return {
    level: "ok",
    text: named
      ? `event veto: ${named} in any cell today`
      : "event veto: none of the rulebook's veto kinds in any cell today",
    kinds,
  };
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
/**
 * The ledger's columns, and which of them are NUMBERS.
 *
 * PASS 2: `num: true` marks a column whose cells are figures, which is what makes the
 * alignment decision explicit and table-wide rather than per-cell. The header takes the
 * same alignment as its column, because a right-aligned number under a left-aligned
 * header is a misalignment the reader has to reconcile on every row.
 */
export const LEDGER_COLUMNS = [
  { key: "id", label: "#", sort: "id" },
  { key: "ts", label: "when", sort: "ts" },
  { key: "arm", label: "arm", sort: "arm" },
  { key: "kind", label: "kind", sort: "kind" },
  { key: "symbol", label: "symbol", sort: "symbol" },
  { key: "conviction", label: "conv", sort: "conviction", num: true },
  { key: "reasoning", label: "reasoning", sort: null },
  { key: "created_at", label: "written", sort: "created_at", num: true },
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

// ---------------------------------------------------------------------------
// 6. calibration (U4) — the T6 story in pictures
// ---------------------------------------------------------------------------

/** The soak's working deep-OTM threshold, in percentage points from spot.
 *
 * # INTERPRETATION: this is a MIRROR of `executor.soak.DEFAULT_DEEP_OTM_PCT`, and
 * deliberately not a second source of truth: it exists only to draw the threshold rule
 * on the distance histogram when `flow_baseline` holds no rows yet (nothing to read it
 * from). As soon as the journal has a session, the drawn value comes from that row's
 * own `deep_otm_threshold_pct` — the number the soak actually applied. This constant is
 * the working input to calibration, never a frozen threshold; the ratification freezes
 * it at the first monthly review with the distribution beside it. */
export const WORKING_DEEP_OTM_PCT = 3;

/** T6's frozen N when the store has not recorded what the rulebook asked for. Mirrors
 * the soak's `baseline_lookback_days` default of 20. */
export const BASELINE_SESSIONS = 20;

/** `executor.iv_rank.MIN_OBSERVATIONS` — below this T5 is PENDING and blocks. Mirrored
 * for the same reason as the threshold above: the page says "warmup not reached", not
 * "the number looks fine". */
export const MIN_IV_OBSERVATIONS = 60;

/**
 * Linear-interpolation percentile, mirroring `ui.api._percentile`.
 *
 * The same convention as the server on purpose: a panel whose P90 line disagreed with
 * the P90 in the API response by a visible amount would be a second definition of the
 * number T6's threshold is frozen from. (The OTHER percentile convention in this repo —
 * `executor.indicators.bandwidth_percentile`'s strict-rank rule — is the right one for
 * scoring a value against a band, which is not what this is.)
 *
 * Missing values are EXCLUDED, not zero-filled: a session with no recorded volume is
 * not a session with zero volume, and substituting 0 would drag the forming P90 down.
 */
export function percentile(values, q) {
  const list = (values || []).filter(isNum);
  if (list.length === 0) return null;
  const ordered = [...list].sort((a, b) => a - b);
  if (ordered.length === 1) return ordered[0];
  const position = (ordered.length - 1) * q;
  const low = Math.floor(position);
  const high = Math.min(low + 1, ordered.length - 1);
  return ordered[low] * (1 - (position - low)) + ordered[high] * (position - low);
}

/**
 * The forming percentile line: index `i` is the percentile of everything measured up
 * to and including `i`. This is what "the P90 is forming" means — a number that moves
 * every session until the baseline is complete — so it has to be drawn as a series, not
 * as today's value extended backwards over history it did not see.
 */
export function percentileSeries(values, q = 0.9) {
  const out = [];
  const seen = [];
  for (const value of values || []) {
    if (isNum(value)) seen.push(value);
    out.push(seen.length ? percentile(seen, q) : null);
  }
  return out;
}

/**
 * Trailing window mean, index `i` over the last `window` values up to `i`.
 *
 * The "20-session baseline mean" line. It is drawn over every session including the
 * early ones, where the window is not full yet — so the model also returns `fullFrom`,
 * the index at which the window first holds `window` samples, and the page says which
 * part of the line is a short-window mean rather than a 20-session one.
 */
export function trailingMean(values, window = BASELINE_SESSIONS) {
  const out = [];
  for (let i = 0; i < (values || []).length; i += 1) {
    const slice = (values || [])
      .slice(Math.max(0, i - window + 1), i + 1)
      .filter(isNum);
    out.push(slice.length ? slice.reduce((a, b) => a + b, 0) / slice.length : null);
  }
  return out;
}

/**
 * `/api/calibration`'s `flow` rows -> the per-session volume chart model.
 *
 * Calls and puts are separate series so the operator can see which side the deep-OTM
 * print was on; the baseline mean and the forming P90 ride on top as lines. `p90Status`
 * is the server's own verdict (`no_data` / `forming` / `defined`) rather than something
 * re-derived here, and `needs` is the honest "needs N more sessions" count — with one
 * or two rows on screen the page must say the P90 is not a threshold yet.
 *
 * KEY NAMES ARE THE WIRE NAMES, not the journal's column names. The endpoint renames
 * `deep_otm_call_volume` / `deep_otm_put_volume` / `deep_otm_total_volume` to
 * `call_volume` / `put_volume` / `total_volume` on the way out (see ui/api.py), and this
 * model read the journal's spelling instead: every volume came back null against a real
 * store, so the panel drew a table of dashes and no chart while the fixtures in
 * tests/test_static_logic.py — written from the schema rather than from a response —
 * passed happily. `test_ui_static.py::test_the_calibration_model_reads_the_wire_key_names`
 * now pins the two sides together, and the fixtures use the wire shape.
 */
export function flowVolume(flow, p90Meta, { window = BASELINE_SESSIONS } = {}) {
  const rows = [...(flow || [])].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  const meta = p90Meta || {};
  const sessionsNeeded = isNum(meta.sessions_needed) ? meta.sessions_needed : window;
  const totals = rows.map((row) => row.total_volume);
  const mean = trailingMean(totals, sessionsNeeded);
  const forming = percentileSeries(totals, 0.9);
  const points = rows.map((row, i) => ({
    date: row.date,
    call: isNum(row.call_volume) ? row.call_volume : null,
    put: isNum(row.put_volume) ? row.put_volume : null,
    total: isNum(row.total_volume) ? row.total_volume : null,
    baselineMean: mean[i],
    formingP90: forming[i],
    thresholdPct: isNum(row.deep_otm_threshold_pct) ? row.deep_otm_threshold_pct : null,
    sessionSpot: isNum(row.session_spot) ? row.session_spot : null,
    baselineDays: isNum(row.baseline_days) ? row.baseline_days : null,
    isDelayed: row.is_delayed === true || row.is_delayed === 1,
    feed: row.feed || null,
  }));
  const measured = totals.filter(isNum).length;
  return {
    points,
    sessions: rows.length,
    sessionsNeeded,
    needs: Math.max(0, sessionsNeeded - rows.length),
    complete: rows.length >= sessionsNeeded,
    measured,
    // A session with no recorded volume is a hole, and the model counts it rather than
    // quietly shortening the series the P90 was computed from.
    holes: rows.length - measured,
    fullFrom: Math.max(0, window - 1),
    p90Status: meta.status || (rows.length ? "forming" : "no_data"),
    p90Value: isNum(meta.value) ? meta.value : forming[forming.length - 1] ?? null,
    p90Method: meta.method || "linear-interpolation percentile of per-session deep-OTM total volume",
    baselineMeanLatest: mean[mean.length - 1] ?? null,
    // The threshold the soak actually applied, from the store. Null before the first row.
    appliedThresholdPct: (() => {
      const seen = points.map((p) => p.thresholdPct).filter(isNum);
      return seen.length ? seen[seen.length - 1] : null;
    })(),
    empty: rows.length === 0,
  };
}

/**
 * The proposed T6 multiplier (`N`) from `/api/calibration`'s `n` block.
 *
 * The volume P90 above is a chart line in contract counts. T6 does not compare a
 * contract count to anything: it evaluates `today_volume >= N * baseline_mean`, so N
 * has to be a *ratio*, and it is the P90 of the per-session ratios
 * (docs/ratification.md section (c)). The two numbers are different quantities and the
 * panel says which is which rather than showing a single ambiguous "P90".
 */
export function proposedN(nMeta) {
  const meta = nMeta || {};
  const excluded = (meta.excluded || []).filter((e) => e && e.reason);
  const byReason = {};
  for (const e of excluded) byReason[e.reason] = (byReason[e.reason] || 0) + 1;
  return {
    status: meta.status || "no_data",
    value: isNum(meta.value) ? meta.value : null,
    callValue: isNum(meta.n_call) ? meta.n_call : null,
    putValue: isNum(meta.n_put) ? meta.n_put : null,
    samples: isNum(meta.samples) ? meta.samples : 0,
    sessionsConsidered: isNum(meta.sessions_considered) ? meta.sessions_considered : 0,
    sessionsQualified: isNum(meta.sessions_qualified) ? meta.sessions_qualified : 0,
    sessionsNeeded: isNum(meta.sessions_needed) ? meta.sessions_needed : 20,
    thresholdPct: isNum(meta.threshold_pct) ? meta.threshold_pct : null,
    formula: meta.method || "",
    excludedCount: excluded.length,
    excludedByReason: byReason,
    notes: (meta.notes || []).filter((n) => typeof n === "string"),
    // Nothing is ever frozen by reading this page. It says "proposed" and means it.
    frozen: false,
    enough: meta.status === "sufficient",
  };
}

/**
 * The 1pp distance buckets the soak stores -> paired call/put bars with the working
 * threshold marked.
 *
 * `deepVolume` is the soak's own definition of "deep": the sum of every bucket at or
 * beyond the threshold (`executor.soak.threshold_from_buckets`), restated here so the
 * page can show the split rather than one total. `buckets` is EMPTY when the journal
 * has no flow rows, and the renderer then says so — it does not draw 0-height bars that
 * look like a flat measurement.
 */
export function distanceHistogram(byDistance, thresholdPct = WORKING_DEEP_OTM_PCT) {
  const merged = new Map();
  for (const side of ["call", "put"]) {
    for (const [key, value] of Object.entries((byDistance && byDistance[side]) || {})) {
      const pct = Number(key);
      if (!Number.isFinite(pct)) continue;
      if (!merged.has(pct)) merged.set(pct, { pct, call: 0, put: 0 });
      merged.get(pct)[side] = merged.get(pct)[side] + (Number(value) || 0);
    }
  }
  const buckets = [...merged.values()].sort((a, b) => a.pct - b.pct);
  const threshold = isNum(thresholdPct) ? thresholdPct : WORKING_DEEP_OTM_PCT;
  const sum = (rows, pick) => rows.reduce((acc, row) => acc + row[pick], 0);
  const deep = buckets.filter((row) => row.pct >= threshold);
  const near = buckets.filter((row) => row.pct < threshold);
  return {
    buckets,
    threshold,
    max: buckets.reduce((peak, row) => Math.max(peak, row.call, row.put), 0),
    callTotal: sum(buckets, "call"),
    putTotal: sum(buckets, "put"),
    deepVolume: sum(deep, "call") + sum(deep, "put"),
    nearVolume: sum(near, "call") + sum(near, "put"),
    empty: buckets.length === 0,
    note:
      "summed across every stored session, not a per-session average — a session with a " +
      "large print counts once per session it printed in",
  };
}

/**
 * IV observations -> per-tenor series, split into proxy and real SEGMENTS.
 *
 * Provenance honesty is the whole point of this panel, so the split is structural: a
 * `VIX` proxy seed row and a real SPY chain row are never one continuous line. Two
 * independent guards exist upstream (`is_proxy` and the `VIX` underlying key, per the
 * ratification's VIX-proxy reconciliation item), and `guardConflicts` reports any row
 * where those two guards DISAGREE — that is a real finding, so it is surfaced rather
 * than silently resolved in favour of either flag.
 *
 * Tenors are keyed `underlying|tenor_key` because the proxy seed has no expiry, right
 * or strike and therefore no meaningful tenor identity of its own; collapsing it into a
 * SPY tenor would be exactly the papering-over the ratification forbids.
 */
export function ivHistory(rows, { minObservations = MIN_IV_OBSERVATIONS } = {}) {
  const list = rows || [];
  const byKey = new Map();
  const guardConflicts = [];
  for (const row of list) {
    if (!row || !row.as_of || !isNum(row.iv)) continue;
    const proxyFlag = Number(row.is_proxy) === 1;
    const proxyKey = String(row.underlying || "").toUpperCase() === "VIX";
    if (proxyFlag !== proxyKey) {
      guardConflicts.push({
        underlying: row.underlying,
        as_of: row.as_of,
        is_proxy: proxyFlag,
        note: proxyFlag
          ? "is_proxy=1 on a non-VIX underlying"
          : "VIX underlying without is_proxy=1 — could reach a real SPY rank",
      });
    }
    const key = `${row.underlying}|${row.tenor_key}`;
    if (!byKey.has(key)) {
      byKey.set(key, { key, underlying: row.underlying, tenorKey: row.tenor_key, points: [] });
    }
    byKey.get(key).points.push({
      date: row.as_of,
      iv: row.iv,
      provenance: proxyFlag ? "proxy" : "real",
      source: row.source || null,
    });
  }
  const series = [...byKey.values()]
    .map((entry) => {
      const points = entry.points.sort((a, b) => String(a.date).localeCompare(String(b.date)));
      const segments = [];
      for (const point of points) {
        const tail = segments[segments.length - 1];
        if (tail && tail.provenance === point.provenance) tail.points.push(point);
        else segments.push({ provenance: point.provenance, points: [point] });
      }
      return {
        ...entry,
        points,
        segments,
        realPoints: points.filter((p) => p.provenance === "real").length,
        proxyPoints: points.filter((p) => p.provenance === "proxy").length,
        latest: points.length ? points[points.length - 1] : null,
        first: points.length ? points[0].date : null,
        last: points.length ? points[points.length - 1].date : null,
        isProxy: points.every((p) => p.provenance === "proxy"),
      };
    })
    .sort((a, b) => a.key.localeCompare(b.key));
  const real = series.flatMap((entry) => entry.points).filter((p) => p.provenance === "real");
  const proxy = series.flatMap((entry) => entry.points).filter((p) => p.provenance === "proxy");
  const proxyLast = proxy.length ? proxy[proxy.length - 1].date : null;
  const realFirst = real.length ? real[0].date : null;
  return {
    series,
    guardConflicts,
    realCount: real.length,
    proxyCount: proxy.length,
    warm: real.length >= minObservations,
    minObservations,
    seam: {
      proxyLast,
      realFirst,
      // "The seam is visible in the store" needs both halves AND a real series that
      // actually begins after the proxy seed ends. Overlapping or reversed is a finding.
      state:
        !proxy.length ? "no_proxy_seed"
          : !real.length
            ? "no_real_observations"
            : realFirst > proxyLast
              ? "ordered"
              : "overlapped_or_reversed",
    },
    empty: series.length === 0,
  };
}

/**
 * The window-start checklist, rendered from what `/api/calibration` can actually see.
 *
 * `state` is one of: `done` | `blocked` | `progress` | `todo` | `unknown`.
 *  - `done`    — the store proves it.
 *  - `blocked` — the store proves it is NOT true yet.
 *  - `progress`— partway, with the count that remains stated.
 *  - `todo`    — a fact this server cannot see (a human or a code fact); never ticked.
 *  - `unknown` — not enough data to say anything at all.
 *
 * # INTERPRETATION: no item is derived from a place the checklist does not exist. The
 * T6 threshold freeze is `progress` even when every stored row agrees on one value,
 * because the working threshold is the INPUT to the calibration and a store of
 * identical numbers is not evidence of a freeze; the reconciliation items that need a
 * human read are `todo`, and a panel that could not be derived is `unknown` rather than
 * a guess. A checklist that renders eight green ticks from six numbers is worse than no
 * checklist, because it would be read as clearance.
 */
export function windowChecklist(payload) {
  const data = payload || {};
  const flow = flowVolume(data.flow, data.p90);
  const iv = ivHistory((data.ivrank || {}).series || []);
  const pending = data.pending_calibrations || [];
  const strategyVersion = data.strategy_version || null;
  const items = [];

  items.push({
    id: "pending_calibrations",
    label: "`pending_calibrations` is empty",
    state: pending.length ? "blocked" : "done",
    reason: pending.length
      ? `${pending.length} still pending: ${pending.join(" · ")}`
      : "the rulebook reports no calibration-pending values",
  });

  items.push({
    id: "t6_threshold_frozen",
    label: "T6 deep-OTM thresholds frozen",
    state: flow.appliedThresholdPct == null ? "unknown" : "progress",
    reason:
      flow.appliedThresholdPct == null
        ? "no flow_baseline session recorded yet, so the threshold the soak applied is " +
          "not in the store at all"
        : `the soak is running at ${flow.appliedThresholdPct}pp — the WORKING value that is ` +
          "the input to the calibration, not a frozen one. A store of identical numbers " +
          "is not evidence that the monthly-review freeze happened.",
  });

  items.push({
    id: "t6_n_frozen",
    label: "T6 N frozen from the ≥20-session baseline",
    state: flow.empty ? "unknown" : "progress",
    reason: flow.empty
      ? "no flow_baseline rows — the soak writes one per session and none has run"
      : flow.complete
        ? `${flow.sessions} session(s) collected against N=${flow.sessionsNeeded}; the ` +
          "baseline is long enough to calibrate from, and the frozen value is still a " +
          "review decision"
        : `${flow.sessions}/${flow.sessionsNeeded} sessions — needs ${flow.needs} more`,
  });

  items.push({
    id: "baseline_accumulating",
    label: "Baseline actually accumulating",
    state: flow.empty ? "blocked" : "progress",
    reason: flow.empty
      ? "no session rows: either the timer has not fired or the soak is failing"
      : `${flow.sessions} session(s) from ${flow.points[0].date} to ` +
        `${flow.points[flow.points.length - 1].date}` +
        (flow.holes ? ` · ${flow.holes} session(s) missing a volume` : "") +
        (flow.fullFrom > 0 && flow.sessions <= flow.fullFrom
          ? ` · the baseline mean is still a short-window mean until session ${flow.fullFrom + 1}`
          : ""),
  });

  items.push({
    id: "proxy_seam",
    label: "VIX-proxy reconciliation — seam visible in the store",
    state:
      iv.seam.state === "ordered"
        ? "progress"
        : iv.seam.state === "no_proxy_seed"
          ? "todo"
          : "blocked",
    reason:
      iv.seam.state === "ordered"
        ? `proxy seed ends ${iv.seam.proxyLast}, real series begins ${iv.seam.realFirst} — ` +
          "the seam is visible. Still needs a human to confirm no proxy row can reach a " +
          "real SPY rank."
        : iv.seam.state === "no_proxy_seed"
          ? "no is_proxy rows in the store: nothing to reconcile, and the VIX warmup " +
            "history has not been seeded either"
          : iv.seam.state === "no_real_observations"
            ? `proxy seed ends ${iv.seam.proxyLast} but no real chain observation exists yet, ` +
              "so there is no seam to see"
            : `real series begins ${iv.seam.realFirst}, on or before the proxy seed ends ` +
              `(${iv.seam.proxyLast}) — overlapping or reversed provenance`,
  });

  items.push({
    id: "iv_warmup",
    label: `IV warmup reached (${iv.minObservations} observations)`,
    state: iv.realCount === 0 ? "unknown" : iv.warm ? "progress" : "blocked",
    reason: iv.realCount === 0
      ? "no real chain observations in the store — warmup cannot be assessed"
      : iv.warm
        ? `${iv.realCount} real observation(s), at or past MIN_OBSERVATIONS=${iv.minObservations}`
        : `${iv.realCount}/${iv.minObservations} real observation(s) — T5 reports PENDING and ` +
          `blocks until this is reached` +
          (iv.proxyCount ? ` (${iv.proxyCount} proxy seed row(s) do not count)` : ""),
  });

  items.push({
    id: "iv_tenor_choice",
    label: "Which tenor T5 reads is settled",
    state: "todo",
    reason:
      "both tenor-key families are stored and neither is marked as the one T5 reads; that " +
      "is a code fact (executor/iv_rank.py), not something this store can answer",
  });

  items.push({
    id: "strategy_version_frozen",
    label: "Strategy version bumped and frozen",
    state: !strategyVersion ? "unknown" : /draft/i.test(strategyVersion) ? "blocked" : "progress",
    reason: !strategyVersion
      ? "the rulebook did not load, so there is no version to check"
      : /draft/i.test(strategyVersion)
        ? `strategy_version is "${strategyVersion}" — a draft rulebook running a ` +
          "pre-registered experiment is a contradiction"
        : `strategy_version is "${strategyVersion}" — frozen at this content hash unless the ` +
          "rulebook changes, which re-stamps every row after it",
  });

  const counts = items.reduce((acc, item) => {
    acc[item.state] = (acc[item.state] || 0) + 1;
    return acc;
  }, {});
  return {
    items,
    counts,
    ready: counts.done === items.length,
    // Deliberately NOT derived from counts.done: several items are `todo`/`unknown` by
    // construction, so this is a statement about the panel, not a verdict on the window.
    summary:
      `${counts.done || 0}/${items.length} provable from the store · ` +
      `${counts.blocked || 0} contradicted · ${counts.progress || 0} in progress · ` +
      `${(counts.todo || 0) + (counts.unknown || 0)} not assessable here`,
  };
}

// ---------------------------------------------------------------------------
// 7. shared page furniture (UX pass 1)
// ---------------------------------------------------------------------------
//
// Operator feedback on the first build: "very dense, compact text, hard to immediately
// discern." The four non-chart pages were four walls of 11px monospace, where the one
// number worth knowing (how much money, how many vetoes, is calibration ready) had the
// same visual weight as a column header. These four helpers are the fix, and they live
// here rather than in each page module because the rules they encode are the SAME rule
// four times, and a rule copied four times is a rule that will be right on three tabs.
//
// # INTERPRETATION — the hierarchy rule, stated once:
//   1. The number you came for is a CARD, at the top, in the largest type on the page.
//   2. Methodology, caveats and "everything is fine" reporting is COLLAPSED. A page
//      whose loudest element says "no problems" is a page that hides the problems.
//   3. An empty state is ONE LINE plus a "?" that holds the explanation. Prose where a
//      fact belongs reads as an error, and the machine is one day old: empty is the
//      current, correct state of four of these tabs, not a failure to render.

/**
 * One big-number summary card.
 *
 * `label` is the noun ("bankroll"), `value` the number, `sub` the muted secondary line
 * that says which slice of the world the number covers. `tone` is the usual up/down/flat
 * class so a card reads at a glance; absent a tone the value is plain foreground.
 *
 * PASS 2: `word: true` is for a value that is a WORD rather than a figure — "plan
 * unavailable", "not open". The 22px mono value slot was built for money and counts, and
 * a 14-character status word in it either ellipsises or, if the tile were widened to fit
 * it, shrinks the numbers to match. So a word value takes the sans stack at a size that
 * fits the slot, and keeps the colour its tone asks for. `date: true` is the middle
 * case: a date is a figure, but a short one, so it stays mono and steps down a size
 * instead of wrapping or clipping. The two kinds of value stay distinguishable, which is
 * the point: a reader can tell a figure from a state without reading it.
 */
export function statCard({
  label,
  value,
  sub = "",
  tone = "",
  title = "",
  word = false,
  date = false,
}) {
  const kind = word ? " word" : date ? " date" : "";
  return `<div class="stat"${title ? ` title="${esc(title)}"` : ""}>
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value ${esc(tone)}${kind}">${value == null ? DASH : esc(value)}</div>
    ${sub ? `<div class="stat-sub">${esc(sub)}</div>` : ""}
  </div>`;
}

/** A row of summary cards. `cards` is a list of statCard() argument objects. */
export function statRow(cards, { cls = "" } = {}) {
  const list = (cards || []).filter(Boolean);
  if (!list.length) return "";
  return `<div class="stat-row ${esc(cls)}">${list.map(statCard).join("")}</div>`;
}

/**
 * An empty state: ONE short line, plus a "?" disclosure carrying the long explanation.
 *
 * `line` is the whole visible state — a fact, not an apology. `why` is the reasoning
 * that used to be the whole paragraph, and it stays collapsed because an operator who
 * needs it can open it and an operator scanning four tabs cannot. The "?" is a real
 * `<details>` element rather than a `title` attribute: a native tooltip cannot hold two
 * sentences, cannot be read on a touch screen, and disappears before it is finished.
 */
export function emptyState(line, why = "", { cls = "" } = {}) {
  const disclosure = why
    ? `<details class="why"><summary title="why">?</summary><div class="why-body">${esc(
        why,
      )}</div></details>`
    : "";
  return `<div class="empty-line ${esc(cls)}"><span>${esc(line)}</span>${disclosure}</div>`;
}

/**
 * A collapsed methodology/integrity block.
 *
 * `count` marks the disclosure itself when there is something to look at, so a real
 * finding is findable without being the loudest thing on the page: a badge on a closed
 * disclosure says "read me", a banner says "stop and read me", and for a research tool
 * where flags are usually zero, only the first is true most of the time.
 */
export function disclosure({ label, body, count = 0, tone = "", open = false, hint = "" }) {
  const badge =
    count > 0 ? `<span class="disc-count ${esc(tone)}">${esc(count)}</span>` : "";
  return `<details class="disc"${open ? " open" : ""}>
    <summary><span class="disc-label">${esc(label)}</span>${badge}${
      hint ? `<span class="disc-hint">${esc(hint)}</span>` : ""
    }</summary>
    <div class="disc-body">${body}</div>
  </details>`;
}

/** Escape helper re-export so page modules have one import for text handling. */
export { esc, usd, pct, truncate, DASH };
