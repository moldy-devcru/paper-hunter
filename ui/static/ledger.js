// Ledger page (U3 item 3): every journal decision row, searchable, sortable,
// filterable, paginated — and read-only in the strongest sense the page can manage.
//
// The journal is append-only. There is no edit affordance anywhere in this file, and
// that is not an omission: a correction is a NEW row whose `references` point at the
// old one, so `referenceChain` turns those pointers into links the operator can follow
// to the current truth. `created_at` (when the row was written) is rendered next to
// `ts` (the market time it is about) because those two are different facts, and
// "immutable" is only visible when both are on screen.
//
// Vanilla DOM, no table library: 12 columns of text and a sort key do not need one, and
// a library would be the first thing in the stack that could rewrite a journal row's
// cell without asking.

import { getJSON, query } from "./net.js";
import { esc, usd, DASH, shortDay, clockET, etDate, truncate } from "./format.js";
import {
  LEDGER_COLUMNS,
  nextSort,
  sortIndicator,
  referenceChain,
  pageInfo,
  statRow,
  emptyState,
  disclosure,
} from "./model.js";

const POLL_MS = 60_000;
const KINDS = ["TRADE", "NO_TRADE", "ROLL", "STOP", "PROPOSAL", "VETO"];
const ARMS = ["A", "B", "C", "EXCEPTION"];

const state = {
  q: "",
  arm: "",
  kind: "",
  from: "",
  to: "",
  sort: "ts",
  order: "desc",
  page: 1,
  perPage: 50,
  rows: [],
  lookup: new Map(),
  inFlight: false,
  timer: null,
  /** Rows a filter would exclude, kept so a corrected row cannot vanish silently. */
  note: "",
};

const el = (id) => document.getElementById(id);

function requestPath() {
  return `/api/trades?${query({
    q: state.q.trim(),
    arm: state.arm,
    kind: state.kind,
    from: state.from,
    to: state.to,
    sort: state.sort,
    order: state.order,
    page: state.page,
    per_page: state.perPage,
  })}`;
}

function headerCells() {
  return LEDGER_COLUMNS.map((column) => {
    const arrow = sortIndicator(column, state.sort, state.order);
    const active = column.sort === state.sort;
    if (!column.sort) {
      // Not every column is sortable (reasoning is free text) and saying so beats a
      // header that looks clickable and does nothing.
      return `<th class="${active ? "sorted" : ""} nosort" title="not sortable">${esc(
        column.label,
      )}</th>`;
    }
    return `<th class="sortable ${active ? "sorted" : ""}" data-sort="${esc(
      column.sort,
    )}" aria-sort="${active ? (state.order === "asc" ? "ascending" : "descending") : "none"}">${esc(
      column.label,
    )}${esc(arrow)}</th>`;
  }).join("");
}

function rowHtml(row) {
  const chain = referenceChain(row, state.lookup);
  const refs = chain.length
    ? `<div class="ref">${chain
        .map((ref) =>
          ref.known
            ? `<a href="#/ledger" data-goto="${ref.id}" title="follow the references chain (row #${ref.id})">correction of #${ref.id}</a>`
            : `<span class="ref-unknown" title="referenced row #${ref.id} is not on this page — page back to it, or widen the filters">correction of #${ref.id} (not loaded)</span>`,
        )
        .join(" ")}</div>`
    : "";
  const reasoning = row.reasoning
    ? `<span title="${esc(row.reasoning)}">${esc(truncate(row.reasoning, 90))}</span>`
    : `<span class="muted">${DASH}</span>`;
  return `<tr data-id="${esc(row.id)}">
    <td class="num">#${esc(row.id)}</td>
    <td class="num" title="${esc(row.ts)}">${esc(shortDay(row.ts))} ${esc(clockET(row.ts))}</td>
    <td><span class="arm-tag ${esc(row.arm)}">${esc(row.arm)}</span></td>
    <td><span class="kind kind-${esc(String(row.kind).toLowerCase())}">${esc(row.kind)}</span></td>
    <td class="num">${esc(row.symbol || DASH)}</td>
    <td class="num">${esc(row.conviction == null ? DASH : row.conviction)}</td>
    <td class="reasoning">${reasoning}${refs}</td>
    <td class="num muted" title="journal created_at — when the row was written, not the market time it is about">${esc(
      row.created_at ? `${shortDay(row.created_at)} ${clockET(row.created_at)}` : DASH,
    )}</td>
  </tr>`;
}

function render(payload) {
  state.rows = payload.rows || [];
  state.lookup = new Map(state.rows.map((row) => [row.id, row]));
  const info = pageInfo(payload);
  state.note = payload.note || "";

  // UX PASS 1: the empty state distinguishes "the journal is empty" from "your filters
  // match nothing", because they are opposite facts and the old text said only the
  // second in every case — an operator with a brand-new journal and no filters at all
  // was told their filters excluded everything, which is a false statement about their
  // own query. At zero rows the table header, the pager and the row counter are all
  // hidden: they describe rows that do not exist.
  const filters = activeFilters();
  const emptyHtml = info.empty
    ? filters.length
      ? emptyState(
          "No rows match these filters",
          `The journal holds ${info.total} row(s) in total and none of them survive ${
            filters.map((filter) => filter.label).join(", ")
          }. Clear a filter chip above, or reset, to see the whole journal. An empty ` +
            `result here is a fact about the filters, not a missing table.`,
        )
      : emptyState(
          "The journal is empty",
          (state.note || "no rows yet") +
            " — the ledger holds every decision the executor has written. Rows appear once " +
            "the hunt runs, so on a young journal this is the expected state, not an error.",
        )
    : "";

  const tableHtml = info.empty
    ? ""
    : `<table class="grid">
      <thead><tr>${headerCells()}</tr></thead>
      <tbody>${state.rows.map(rowHtml).join("")}</tbody>
    </table>
    <div class="pager">
      <span class="pager-info">rows ${info.first}–${info.last} of ${info.total}${
        state.note ? ` · ${esc(state.note)}` : ""
      }</span>
      <button class="pg" data-page="prev" ${info.hasPrev ? "" : "disabled"}>‹ prev</button>
      <span class="pager-page">page ${info.page} of ${Math.max(info.pages, 1)}</span>
      <button class="pg" data-page="next" ${info.hasNext ? "" : "disabled"}>next ›</button>
    </div>`;

  el("ledger-body").innerHTML =
    renderSummary(info, filters) + ledgerPolicy() + emptyHtml + tableHtml;
  renderFilterChips();
  el("ledger-stamp").textContent = `ledger · ${info.total} row(s) matching · updated ${new Date()
    .toLocaleTimeString("en-GB", { hour12: false })}`;
}

/** True when any filter narrows the query. Drives the empty state's wording. */
function activeFilters() {
  const parts = [];
  if (state.q.trim()) parts.push({ key: "q", label: `search "${state.q.trim()}"` });
  if (state.arm) parts.push({ key: "arm", label: `arm ${state.arm}` });
  if (state.kind) parts.push({ key: "kind", label: state.kind });
  if (state.from) parts.push({ key: "from", label: `from ${state.from}` });
  if (state.to) parts.push({ key: "to", label: `to ${state.to}` });
  return parts;
}

/**
 * UX PASS 1: the active filters, as removable chips under the filter bar.
 *
 * A filter you cannot see the effect of is a filter you cannot undo. Before this the
 * only way to know a filter was on was to read the empty-state sentence and count
 * clauses in it, and the only way to undo one was the reset button — which throws away
 * every filter at once to change one. Each chip names one filter and clears exactly
 * that one; reset stays for the whole set.
 */
function renderFilterChips() {
  const filters = activeFilters();
  const node = el("ledger-chips");
  if (!node) return;
  node.hidden = filters.length === 0;
  node.innerHTML = filters.length
    ? `<span class="chip-label">filters</span>${filters
        .map(
          (filter) =>
            `<button class="chip fchip" data-clear="${esc(filter.key)}" title="clear this filter">${esc(
              filter.label,
            )} ✕</button>`,
        )
        .join("")}`
    : "";
}

/**
 * UX PASS 1: three numbers above the table — how much is in the journal, how much of it
 * this page is showing, and when the last row was written. On a journal that fills up
 * over months, "rows shown" of a growing total is the number an operator actually
 * navigates by, and it was previously buried in the pager's small print.
 */
function renderSummary(info, filters) {
  const rows = state.rows;
  const last = rows.length
    ? rows.reduce((acc, row) => (row.created_at > acc ? row.created_at : acc), "")
    : "";
  const filtered = filters.length > 0;
  return statRow([
    {
      label: filtered ? "matching rows" : "journal rows",
      value: String(info.total),
      sub: filtered ? `${filters.length} filter(s) on` : "unfiltered total",
    },
    {
      label: "rows shown",
      value: String(rows.length),
      sub: info.total ? `${info.first}–${info.last} of ${info.total}` : "nothing to page",
    },
    {
      label: "last written",
      value: last ? `${shortDay(last)} ${clockET(last)}` : DASH,
      sub: last ? "journal created_at" : "nothing written yet",
      title: "created_at — when the row was written, not the market time it is about",
    },
  ]);
}

/** UX PASS 1: the append-only policy moves off the page face and into a disclosure. */
function ledgerPolicy() {
  return disclosure({
    label: "append-only",
    hint: "a correction is a new row, never an edit",
    body:
      `<div class="mp-detail">Rows are appended, never edited. A correction is a NEW row ` +
      `that references the old one, and the references render as followable links, so the ` +
      `chain from a superseded row to the current truth is walkable on this page.</div>` +
      `<div class="mp-detail" style="margin-top:4px">There is no edit affordance anywhere ` +
      `on this page, by design. \`created_at\` is rendered next to \`ts\` because those are ` +
      `different facts: one is when the row was written, one is the market time it is about.</div>`,
  });
}

export async function loadLedger() {
  if (state.inFlight) return;
  state.inFlight = true;
  try {
    const payload = await getJSON(requestPath());
    if (payload.page && payload.page > 1 && (payload.rows || []).length === 0) {
      // A filter narrowed to nothing on a page past the end: go back to page 1 rather
      // than showing an empty table that looks like the ledger is empty.
      state.page = 1;
      state.inFlight = false;
      return loadLedger();
    }
    render(payload);
  } catch (error) {
    el("ledger-body").innerHTML = `<div class="banner bad">/api/trades failed: ${esc(
      String(error.message || error),
    )}</div>`;
    el("ledger-stamp").textContent = "ledger · error";
  } finally {
    state.inFlight = false;
  }
}

function resetPage() {
  state.page = 1;
}

/** Clear exactly one filter and mirror it back into its control. */
function clearFilter(key) {
  if (key === "q") {
    state.q = "";
    el("ledger-q").value = "";
  } else if (key === "arm") {
    state.arm = "";
    el("ledger-arm").value = "";
  } else if (key === "kind") {
    state.kind = "";
    el("ledger-kind").value = "";
  } else if (key === "from") {
    state.from = "";
    el("ledger-from").value = "";
  } else if (key === "to") {
    state.to = "";
    el("ledger-to").value = "";
  } else {
    return;
  }
  resetPage();
  loadLedger();
}

export function wireLedger() {
  const q = el("ledger-q");
  let debounce = null;
  q.addEventListener("input", () => {
    clearTimeout(debounce);
    // Debounced because every keystroke is a server query against a growing table,
    // and the journal never changes under us mid-keystroke.
    debounce = setTimeout(() => {
      state.q = q.value;
      resetPage();
      loadLedger();
    }, 250);
  });
  el("ledger-arm").addEventListener("change", (event) => {
    state.arm = event.target.value;
    resetPage();
    loadLedger();
  });
  el("ledger-kind").addEventListener("change", (event) => {
    state.kind = event.target.value;
    resetPage();
    loadLedger();
  });
  el("ledger-from").addEventListener("change", (event) => {
    state.from = event.target.value;
    resetPage();
    loadLedger();
  });
  el("ledger-to").addEventListener("change", (event) => {
    state.to = event.target.value;
    resetPage();
    loadLedger();
  });
  el("ledger-reset").addEventListener("click", () => {
    state.q = "";
    state.arm = "";
    state.kind = "";
    state.from = "";
    state.to = "";
    q.value = "";
    el("ledger-arm").value = "";
    el("ledger-kind").value = "";
    el("ledger-from").value = "";
    el("ledger-to").value = "";
    resetPage();
    loadLedger();
  });

  // UX PASS 1: chips live in the header, so their handler lives here rather than in the
  // body listener. One filter per chip; `reset` still clears the whole set.
  el("ledger-chips").addEventListener("click", (event) => {
    const chip = event.target.closest("button.fchip");
    if (chip) clearFilter(chip.dataset.clear);
  });

  el("ledger-body").addEventListener("click", (event) => {
    const header = event.target.closest("th.sortable");
    if (header) {
      const next = nextSort({ sort: header.dataset.sort }, state.sort, state.order);
      state.sort = next.sort;
      state.order = next.order;
      resetPage();
      loadLedger();
      return;
    }
    const pager = event.target.closest("button.pg");
    if (pager) {
      if (pager.disabled) return;
      state.page = pager.dataset.page === "next" ? state.page + 1 : state.page - 1;
      loadLedger();
      return;
    }
    const link = event.target.closest("a[data-goto]");
    if (link) {
      // Following the references chain: search for the referenced id, which is the
      // only way to reach a row that is not on the current page. The row is found by
      // q, not by a route parameter, because /api/trades is the ledger's own index.
      event.preventDefault();
      state.q = `#${link.dataset.goto}`;
      el("ledger-q").value = state.q;
      resetPage();
      loadLedger();
    }
  });
}

export function startLedger() {
  stopLedger();
  loadLedger();
  state.timer = setInterval(() => {
    if (!document.hidden && state.q === "") loadLedger();
  }, POLL_MS);
}

export function stopLedger() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

/** The filter <select> options, so index.html does not hardcode the vocabularies. */
export const LEDGER_FILTERS = { ARMS, KINDS };

/** ET date of "today" for the date picker's default bounds. */
export const todayET = () => etDate(new Date().toISOString());

/** Exported for the tests: the money formatter the counterfactual column shares. */
export { usd };
