// Hunt page (U3 item 4): the day's plan as a direction × arm checklist grid, the
// NO-SHOT ledger with counterfactual outcomes, and the veto histogram.
//
// Three panels, three endpoints, loaded independently (net.getAll uses allSettled): a
// dead histogram must not take the plan down with it, and each panel says what it could
// not load instead of the page going blank.
//
// # INTERPRETATION — the checklist grid. T1..T6 are rendered as chips with their own
// status, and the veto reason lives in the chip's `title` rather than in a column of
// prose: six conditions × three arms × two directions is 36 cells per day, and a grid of
// sentences is unreadable at that density. The event veto is the exception and gets a
// banner, because it is a day-level fact rather than a per-cell one.

import { getJSON, getAll, query } from "./net.js";
import { esc, usd, pct, px, DASH, shortDay, clockET, toneClass } from "./format.js";
import {
  huntGrid,
  conditionChips,
  eventVetoBanner,
  vetoHistogram,
  counterfactual,
  statRow,
  emptyState,
} from "./model.js";

const POLL_MS = 60_000;
const state = { date: "", inFlight: false, timer: null };
const el = (id) => document.getElementById(id);

const today = () =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());

// ---------------------------------------------------------------------------
// plan grid
// ---------------------------------------------------------------------------

function chips(cell) {
  if (!cell) return `<span class="muted">no cell</span>`;
  return conditionChips(cell)
    .map(
      (chip) =>
        `<span class="chip ${chip.tone}" title="${esc(chip.id)}: ${esc(chip.reason)}` +
        `${chip.blocking ? " (blocking)" : " (advisory)"}">${esc(chip.id)}</span>`,
    )
    .join("");
}

function cellHtml(cell) {
  if (!cell) return `<td class="cell missing">—</td>`;
  const triggers = (cell.trigger_prices || [])
    .map(
      (trigger) =>
        `<span class="trig">${esc(trigger.kind || "trigger")} @ ${esc(
          px(trigger.price ?? trigger.strike ?? null),
        )}</span>`,
    )
    .join("");
  const watch = Object.entries(cell.watch_levels || {})
    .map(([key, value]) => `<span class="watch">${esc(key)} ${esc(px(value))}</span>`)
    .join("");
  const fire = cell.fire === true;
  return `<td class="cell ${fire ? "fires" : "holds"}">
    <div class="cell-top"><span class="kind kind-${esc(
      String(cell.kind).toLowerCase(),
    )}">${esc(cell.kind)}</span>${
      fire ? `<span class="chip pass">FIRE</span>` : `<span class="chip fail">HOLD</span>`
    }</div>
    <div class="chips">${chips(cell)}</div>
    ${cell.veto_reasons && cell.veto_reasons.length
      ? `<div class="veto-reasons">${cell.veto_reasons
          .map((reason) => `<div>${esc(reason)}</div>`)
          .join("")}</div>`
      : ""}
    ${triggers ? `<div class="triggers">${triggers}</div>` : ""}
    ${watch ? `<div class="watch-levels">${watch}</div>` : ""}
    <div class="cell-foot">#${esc(cell.journal_id)} · ${esc(String(cell.direction || DASH))} ·
      conv ${esc(cell.conviction == null ? DASH : cell.conviction)}</div>
  </td>`;
}

function renderPlan(payload) {
  const grid = huntGrid(payload.cells);
  // UX PASS 2 fix #3: `eventVetoBanner` is handed the RULEBOOK's veto kinds, so the
  // all-clear names what it checked ("none of fomc, cpi recorded") instead of asserting
  // that nothing was found. The chip stays quiet; the confirmation lives in the
  // disclosure behind the "?", where a reader who wants to satisfy themselves can read
  // the whole sentence, and in the chip's own tooltip for a glance.
  const ruleKinds = payload.event_veto_kinds || [];
  const banner = eventVetoBanner(payload.event_veto, { kinds: ruleKinds });
  // The event veto is a day-level fact, so it earns a banner ONLY when it says
  // something — active, or unknown (a rulebook we could not read is not an all
  // clear). The "none of the veto kinds" case used to render a full-width green banner
  // at the top of the tab, which made the absence of a problem the most prominent thing
  // on the page. It is a chip now, inline with the plan meta, where a glance catches it
  // and a scan does not trip over it.
  const bannerHtml = banner.level === "ok"
    ? ""
    : `<div class="banner ${banner.level === "bad" ? "bad" : "warn"}"><b>${esc(
        banner.text,
      )}</b></div>`;
  const eventChip = banner.level === "ok"
    ? `<span class="chip soft veto-clear" title="${esc(banner.text)}">event veto: none${
        ruleKinds.length ? ` (${esc(ruleKinds.join(", "))})` : ""
      }</span>` +
      `<details class="why"><summary title="what was checked">?</summary><div class="why-body">${esc(
        banner.text,
      )}. Checked against the rulebook's own <code>event_calendar.veto_kinds</code>${
        ruleKinds.length
          ? ` (${esc(ruleKinds.join(", "))})`
          : " — none could be read"
      }, matched against each cell's T5 condition text. This is a quiet chip rather than
         a banner because the absence of a veto is the expected state, not a finding;
         the reasoning is here so the claim can be checked rather than trusted.</div></details>`
    : "";
  if (!grid.empty) {
    const head = `<tr><th>direction</th>${grid.arms
      .map((arm) => `<th class="arm-head">arm ${esc(arm)}</th>`)
      .join("")}</tr>`;
    const body = grid.rows
      .map(
        (row) =>
          `<tr><td class="dir">${esc(row.direction)}</td>${row.arms.map(cellHtml).join("")}</tr>`,
      )
      .join("");
    el("hunt-plan").innerHTML =
      bannerHtml +
      `<div class="plan-meta">date ${esc(payload.date)} · ${grid.rows.length} direction(s) × ${
        grid.arms.length
      } arm(s) · source: ${esc(payload.source || "journal")} ${eventChip}</div>` +
      `<table class="plan">${head}<tbody>${body}</tbody></table>`;
  } else {
    // UX PASS 1: one line, plus the explanation behind a "?". The old sentence ran to
    // three clauses across two lines and read like a failure; the machine is one day
    // old and an unrun session is the expected state, not an error worth shouting about.
    el("hunt-plan").innerHTML =
      bannerHtml +
      (eventChip ? `<div class="plan-meta">${eventChip}</div>` : "") +
      emptyState(
        `No plan cells yet for ${payload.date} — the hunt writes its plan each session, pre-market`,
        "The plan is not a table: executor.hunt_plan writes one immutable decision row " +
          "per (arm, direction) cell, and /api/huntplan reads those rows back. An empty " +
          "day therefore means the hunt has not run for this session — not that a table " +
          "is missing. Pick another date above, or wait for the next session.",
      );
  }
}

// ---------------------------------------------------------------------------
// NO-SHOT ledger
// ---------------------------------------------------------------------------

function renderNoshots(payload) {
  const rows = payload.rows || [];
  const delta = payload.counterfactual;
  const head = `<div class="plan-meta">${rows.length} sighting(s)${
    delta
      ? ` · delta-weighted counterfactual ${usd(delta.proxy_usd)} vs realized ${
          delta.realized_usd == null ? DASH : usd(delta.realized_usd)
        } (${usd(delta.delta_usd)})`
      : ""
  }</div>`;
  if (!rows.length) {
    el("hunt-noshots").innerHTML = `${head}${emptyState(
      "No sightings for this date",
      "A NO-SHOT row is written when the indicator panel was on and the shot was still " +
        "not taken. No rows means the session either did not run or nothing came close " +
        "enough to log. Each row carries its own counterfactual outcome, which fills in " +
        "after the fact once the window has moved.",
    )}`;
    return;
  }
  const body = rows
    .map((row) => {
      const hypothesis = row.instrument_hypothesis || {};
      const failed = Object.entries(row.failed_conditions || {})
        .map(
          ([id, detail]) =>
            `<span class="chip fail" title="${esc(
              typeof detail === "object" && detail ? detail.detail || "" : String(detail),
            )}">${esc(id)}</span>`,
        )
        .join("");
      const cf = counterfactual(row.counterfactual_outcome);
      return `<tr>
        <td class="num">${esc(row.date)}</td>
        <td class="num">${esc(clockET(row.ts))}</td>
        <td>${esc(hypothesis.arm || DASH)}</td>
        <td>${esc(hypothesis.direction || DASH)}</td>
        <td>${failed || `<span class="dash">${DASH}</span>`}</td>
        <td class="r num ${row.price == null ? "dash" : ""}">${esc(
          row.price == null ? DASH : row.price.toFixed(2),
        )}</td>
        <td class="cf ${esc(cf.tone)}" title="${esc(cf.detail)}">${esc(cf.headline)}</td>
        <td class="num ${
          row.counterfactual_entry_ref == null
            ? "dash"
            : "r"
        }">${
          row.counterfactual_entry_ref == null
            ? `<span title="no decision row references this sighting">unlinked</span>`
            : `#${esc(row.counterfactual_entry_ref)}`
        }</td>
      </tr>`;
    })
    .join("");
  el("hunt-noshots").innerHTML = `${head}<table class="grid">
    <thead><tr><th>date</th><th>time</th><th>arm</th><th>dir</th><th>failed</th>
    <th class="r">underlying</th><th>counterfactual</th><th class="r">decision</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}

// ---------------------------------------------------------------------------
// veto histogram — plain stacked divs, see model.vetoHistogram for the reasoning
// ---------------------------------------------------------------------------

const VETO_COLORS = ["#4c9aff", "#e0a63a", "#e05fd0", "#3fb9c8", "#9aa4b2", "#f85149", "#8f7bff", "#26a69a"];

function renderHistogram(payload) {
  const model = vetoHistogram(payload.weeks);
  if (model.empty) {
    el("hunt-histogram").innerHTML = emptyState(
      "No vetoes recorded yet",
      "The histogram counts which checklist condition said no, across NO_TRADE decisions " +
        "and NO-SHOT rows, bucketed by week. Empty means the checklist has not blocked " +
        "anything yet — with the journal one day old that is the expected state, not a " +
        "clean bill of health.",
    );
    return;
  }
  const legend = model.conditions
    .map(
      (condition, i) =>
        `<span class="lg"><i style="background:${VETO_COLORS[i % VETO_COLORS.length]}"></i>${esc(
          condition.id,
        )} (${condition.total})</span>`,
    )
    .join("");
  const bars = model.bars
    .map((bar) => {
      const segments = bar.segments
        .map((segment) => {
          const index = model.conditions.findIndex((c) => c.id === segment.condition);
          return `<i style="flex:${segment.value};background:${
            VETO_COLORS[index % VETO_COLORS.length]
          }" title="${esc(segment.condition)}: ${segment.value}"></i>`;
        })
        .join("");
      return `<div class="hbar" title="week of ${esc(bar.week_start)}: ${bar.total} veto(es)${
        bar.reason ? ` — ${esc(bar.reason)}` : ""
      }">
        <div class="hstack" style="height:${Math.max(4, (bar.total / (model.max || 1)) * 100)}%">${
          bar.total ? segments : `<i class="zero"></i>`
        }</div>
        <span class="hlabel">${esc(shortDay(bar.week_start))}</span>
        <span class="hcount">${bar.total}</span>
      </div>`;
    })
    .join("");
  const pending = model.bars.reduce((acc, bar) => {
    for (const [id, n] of Object.entries(bar.pending || {})) {
      acc[id] = (acc[id] || 0) + (Number(n) || 0);
    }
    return acc;
  }, {});
  const pendingLine = Object.keys(pending).length
    ? `<div class="plan-meta">pending (not counted as a veto): ${Object.entries(pending)
        .map(([id, n]) => `${esc(id)}×${n}`)
        .join(", ")}</div>`
    : "";
  el("hunt-histogram").innerHTML =
    `<div class="chart-legend">${legend}</div>` +
    `<div class="hist">${bars}</div>${pendingLine}` +
    `<div class="plan-meta">${
      payload.window ? `${esc(payload.window.from)} → ${esc(payload.window.to)}` : ""
    }</div>`;
}

function panelError(name, error) {
  return `<div class="banner bad">${esc(name)} failed: ${esc(error)}</div>`;
}

/**
 * UX PASS 1: the four numbers the tab exists to answer, before any panel.
 *
 * Every one of them renders from an EMPTY payload without pretending: a dash is a
 * measurement we do not have, and a card that reads "0 plan cells" on a machine that
 * has never run is a true statement about the journal, which is not the same as a
 * verdict on the hunt. That is why the "run status" card is a word and not a number.
 */
function renderSummary({ plan, noshots, histogram, day, errors }) {
  const grid = plan ? huntGrid(plan.cells) : { rows: [], arms: [], empty: true };
  const cells = (plan && plan.cells) || [];
  const fired = cells.filter((cell) => cell.fire === true).length;
  const held = cells.filter((cell) => cell.fire === false).length;
  const banner = plan
    ? eventVetoBanner(plan.event_veto, { kinds: plan.event_veto_kinds || [] })
    : { level: "unknown", text: "" };

  let status = "not run";
  let statusTone = "";
  if (errors.plan) {
    status = "plan unavailable";
  } else if (grid.empty) {
    status = "no plan yet";
  } else if (banner.level === "bad") {
    status = "event veto";
    statusTone = "down";
  } else if (fired) {
    status = `${fired} cell${fired === 1 ? "" : "s"} fire`;
    statusTone = "up";
  } else {
    status = `${held} cell${held === 1 ? "" : "s"} hold`;
  }

  const rows = (noshots && noshots.rows) || [];
  const delta = noshots && noshots.counterfactual;
  const vetoTotal = histogram && histogram.totals
    ? Object.values(histogram.totals.by_condition || {}).reduce((a, b) => a + b, 0)
    : null;

  el("hunt-summary").innerHTML = statRow([
    {
      label: "run status",
      value: status,
      tone: statusTone,
      // PASS 2: every branch of `status` is a phrase ("plan unavailable", "3 cells
      // fire"), not a figure — without this it renders in the 22px numeric slot and
      // ellipsises the one word that says what the day did.
      word: true,
      sub: day,
      title: "the plan for this session: how many cells fire, how many hold, or that it has not run",
    },
    {
      label: "plan cells",
      value: String(cells.length),
      sub: grid.empty ? "no plan written" : `${grid.rows.length} direction(s) × ${grid.arms.length} arm(s)`,
      title: "immutable decision rows the executor wrote for this session",
    },
    {
      label: "sightings",
      value: String(rows.length),
      sub: delta ? `delta ${usd(delta.delta_usd)}` : "NO-SHOT rows on this date",
      title: "indicator panel on, shot not taken — each carries a counterfactual outcome",
    },
    {
      label: "vetoes",
      value: vetoTotal == null ? DASH : String(vetoTotal),
      sub: vetoTotal ? "conditions that said no" : "none recorded",
      title: "which checklist conditions blocked, across the whole journal window",
    },
  ]);
}

export async function loadHunt() {
  if (state.inFlight) return;
  state.inFlight = true;
  const day = state.date || today();
  const { data, errors } = await getAll({
    plan: `/api/huntplan?date=${day}`,
    noshots: `/api/noshots?from=${day}&to=${day}&limit=200`,
    histogram: `/api/histogram?veto=weekly`,
  });
  if (errors.plan) el("hunt-plan").innerHTML = panelError("/api/huntplan", errors.plan);
  else renderPlan(data.plan);
  if (errors.noshots) el("hunt-noshots").innerHTML = panelError("/api/noshots", errors.noshots);
  else renderNoshots(data.noshots);
  if (errors.histogram) el("hunt-histogram").innerHTML = panelError("/api/histogram", errors.histogram);
  else renderHistogram(data.histogram);
  renderSummary({ plan: data.plan, noshots: data.noshots, histogram: data.histogram, day, errors });
  el("hunt-summary").hidden = false;
  el("hunt-stamp").textContent = `hunt · ${day} · updated ${new Date().toLocaleTimeString("en-GB", {
    hour12: false,
  })}`;
  state.inFlight = false;
}

export function wireHunt() {
  const picker = el("hunt-date");
  picker.value = today();
  state.date = picker.value;
  picker.addEventListener("change", () => {
    state.date = picker.value || today();
    loadHunt();
  });
  el("hunt-today").addEventListener("click", () => {
    picker.value = today();
    state.date = picker.value;
    loadHunt();
  });
}

export function startHunt() {
  stopHunt();
  loadHunt();
  state.timer = setInterval(() => {
    if (!document.hidden) loadHunt();
  }, POLL_MS);
}

export function stopHunt() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

/** Exported so the module's pure pieces stay reachable from the test harness. */
export {
  vetoHistogram,
  huntGrid,
  conditionChips,
  counterfactual,
  eventVetoBanner,
  statRow,
  emptyState,
  pct,
  toneClass,
  query,
};
