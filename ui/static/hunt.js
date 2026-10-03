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
import { huntGrid, conditionChips, eventVetoBanner, vetoHistogram, counterfactual } from "./model.js";

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
  const banner = eventVetoBanner(payload.event_veto);
  const bannerHtml = `<div class="banner ${banner.level === "bad" ? "bad" : banner.level === "ok" ? "ok" : "warn"}">
    <b>${esc(banner.text)}</b></div>`;
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
      } arm(s) · source: ${esc(payload.source || "journal")}</div>` +
      `<table class="plan">${head}<tbody>${body}</tbody></table>`;
  } else {
    el("hunt-plan").innerHTML =
      bannerHtml +
      `<div class="empty big">no plan cells for ${esc(
        payload.date,
      )}. The plan is written once per session by the executor; an empty day is a day the hunt has not run, not a missing table.</div>`;
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
    el("hunt-noshots").innerHTML = `${head}<div class="empty">no NO-SHOT sightings recorded</div>`;
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
        <td>${failed || `<span class="muted">${DASH}</span>`}</td>
        <td class="num">${esc(row.price == null ? DASH : row.price.toFixed(2))}</td>
        <td class="cf ${esc(cf.tone)}" title="${esc(cf.detail)}">${esc(cf.headline)}</td>
        <td class="num muted">${
          row.counterfactual_entry_ref == null
            ? `<span class="muted" title="no decision row references this sighting">unlinked</span>`
            : `#${esc(row.counterfactual_entry_ref)}`
        }</td>
      </tr>`;
    })
    .join("");
  el("hunt-noshots").innerHTML = `${head}<table class="grid">
    <thead><tr><th>date</th><th>time</th><th>arm</th><th>dir</th><th>failed</th>
    <th>underlying</th><th>counterfactual</th><th>decision</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}

// ---------------------------------------------------------------------------
// veto histogram — plain stacked divs, see model.vetoHistogram for the reasoning
// ---------------------------------------------------------------------------

const VETO_COLORS = ["#4c9aff", "#e0a63a", "#e05fd0", "#3fb9c8", "#9aa4b2", "#f85149", "#8f7bff", "#26a69a"];

function renderHistogram(payload) {
  const model = vetoHistogram(payload.weeks);
  if (model.empty) {
    el("hunt-histogram").innerHTML = `<div class="empty">no veto records in the journal yet — the histogram is the count of which condition said no, and nothing has said no yet</div>`;
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
export { vetoHistogram, huntGrid, conditionChips, counterfactual, eventVetoBanner, pct, toneClass, query };
