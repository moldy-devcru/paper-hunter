// Arms page (U3 item 2): three cards — bankroll, deployed, cash, open positions, the
// per-arm equity curve, and P&L absolute + vs the control. Arm C's card carries the
// shadow-roll sim as a dashed overlay. The exception path gets its own block and is
// never summed into A/B/C — that separation is the reason the numbers mean anything.
//
// Read-only, offline, and honest about the window not being open: an arm with no
// positions and no closes says "window not open — no positions yet" in words, not as a
// flat zero line that looks like a result.

import { getJSON } from "./net.js";
import { esc, usd, pct, dollars, toneClass, DASH, isNum, clockET } from "./format.js";
import { armSummary, mismatchesByArm, ARM_COLOR, ARM_LABEL } from "./model.js";
import { equityChart, shadowOverlayChart } from "./charts.js";

const POLL_MS = 45_000;
let timer = null;
let charts = [];
let inFlight = false;

const el = (id) => document.getElementById(id);

function teardownCharts() {
  for (const chart of charts) {
    try {
      chart.remove();
    } catch (_) {
      /* a chart already torn down with its container */
    }
  }
  charts = [];
}

/** One arm card. `flags` are the endpoint's integrity flags for this arm. */
function card(summary, flags) {
  const flagHtml = flags.length
    ? `<div class="arm-flags">${flags
        .map(
          (flag) =>
            `<div class="arm-flag ${esc(flag.severity)}">` +
            `<span class="af-code">${esc(flag.code)}</span> ${esc(flag.detail)}</div>`,
        )
        .join("")}</div>`
    : "";
  const positions = summary.positions.length
    ? `<table class="mini"><thead><tr><th>contract</th><th class="r">qty</th>` +
      `<th class="r">entry</th><th class="r">last</th><th class="r">unreal</th></tr></thead><tbody>` +
      summary.positions
        .map(
          (position) =>
            `<tr title="${esc(position.notes)}"><td>${esc(position.label)}</td>` +
            `<td class="r num">${esc(position.qty ?? DASH)}</td>` +
            `<td class="r num">${esc(pxOf(position.entry))}</td>` +
            `<td class="r num muted">${DASH}</td>` +
            `<td class="r num muted" title="${esc(position.notes)}">${DASH}</td></tr>`,
        )
        .join("") +
      `</tbody></table>`
    : `<div class="empty">no open positions</div>`;

  const vsControl =
    summary.isControl
      ? `<div class="arm-line"><span>vs control</span><b>— this IS the control (${esc(
          summary.arm,
        )})</b></div>`
      : `<div class="arm-line"><span>vs control</span><b class="${toneClass(
          summary.vsControl,
        )}">${usd(summary.vsControl)}</b></div>`;

  return `<div class="arm-card" data-arm="${esc(summary.arm)}" style="--arm:${esc(summary.color)}">
    <div class="arm-head"><span class="arm-name">${esc(summary.label)}</span>
      <span class="arm-pnl ${toneClass(summary.pnl)}">${usd(summary.pnl)}</span></div>
    <div class="arm-line"><span>bankroll</span><b class="num">${dollars(summary.bankroll)}</b></div>
    <div class="arm-line"><span>deployed</span><b class="num">${dollars(summary.deployed)}</b></div>
    <div class="arm-line"><span>cash</span><b class="num">${
      summary.cash == null ? DASH : dollars(summary.cash)
    }</b></div>
    <div class="arm-line"><span>return on bankroll</span><b class="num ${toneClass(
      summary.returnOnBankroll,
    )}">${pct(summary.returnOnBankroll)}</b></div>
    ${vsControl}
    <div class="arm-line"><span>closed / open</span><b class="num">${summary.closed} / ${
      summary.openCount
    }</b></div>
    <div class="arm-chart" id="chart-arm-${esc(summary.arm)}"></div>
    <div class="arm-curve-note">${
      summary.curve.length
        ? `realized equity over ${summary.curve.length} close(s), last ${esc(
            clockET(summary.curve[summary.curve.length - 1].t),
          )}`
        : "no closed positions — window not open, no curve to draw"
    }</div>
    ${positions}
    ${flagHtml}
  </div>`;
}

const pxOf = (n) => (isNum(n) ? n.toFixed(2) : DASH);

function exceptionBlock(summary, flags) {
  const shadow = summary.shadow;
  return `<div class="arm-card exception" style="--arm:${esc(ARM_COLOR.EXCEPTION)}">
    <div class="arm-head"><span class="arm-name">${esc(ARM_LABEL.EXCEPTION)}</span>
      <span class="arm-pnl ${toneClass(summary.pnl)}">${usd(summary.pnl)}</span></div>
    <div class="banner magenta">rendered separately on purpose — catalyst-clause P&amp;L is
      never summed into A/B/C, and a chart that added it would be lying about the arms.</div>
    <div class="arm-line"><span>bankroll</span><b class="num">${dollars(summary.bankroll)}</b></div>
    <div class="arm-line"><span>closed / open</span><b class="num">${summary.closed} / ${
      summary.openCount
    }</b></div>
    ${summary.positions.length ? `<table class="mini"><tbody>${summary.positions
      .map(
        (position) =>
          `<tr><td>${esc(position.label)}</td><td class="r num">${esc(
            position.qty ?? DASH,
          )}</td><td class="r num">${pxOf(position.entry)}</td></tr>`,
      )
      .join("")}</tbody></table>` : `<div class="empty">no exception-path positions</div>`}
    ${flags.length ? `<div class="arm-flags">${flags
        .map((flag) => `<div class="arm-flag ${esc(flag.severity)}"><span class="af-code">${esc(flag.code)}</span> ${esc(flag.detail)}</div>`)
        .join("")}</div>` : ""}
  </div>`;
}

function render(payload) {
  const arms = payload.arms || [];
  const byArm = new Map(arms.map((arm) => [arm.arm, arm]));
  const flags = mismatchesByArm(payload.mismatches || []);
  const control = payload.control_arm || "A";
  const summaries = ["A", "B", "C"]
    .filter((arm) => byArm.has(arm))
    .map((arm) =>
      armSummary(byArm.get(arm), { controlArm: control, controlPnl: byArm.get(arm).realized_pnl }),
    );

  const crossCheck = payload.cross_check || {};
  const flagCount = (payload.mismatches || []).length;
  const crossHtml = `<div class="banner ${flagCount ? "bad" : "warn"}">
    <b>${flagCount} integrity flag${flagCount === 1 ? "" : "s"}</b> — ${
      crossCheck.broker_checked
        ? "journal vs broker"
        : "journal-internal only; NO broker cross-check was performed and none is implied"
    }. ${esc(crossCheck.note || "")}</div>`;

  const body = summaries.length
    ? `<div class="arm-grid">${summaries
        .map((summary) => card(summary, flags.get(summary.arm) || []))
        .join("")}</div>`
    : `<div class="empty big">no arms — ${esc(
        payload.note || "the journal holds no arm meta yet",
      )}</div>`;

  const windowNote = payload.sufficient
    ? ""
    : `<div class="banner warn">insufficient history for a scored comparison${
        payload.reason ? `: ${esc(payload.reason)}` : ""
      }</div>`;

  el("arms-body").innerHTML = crossHtml + windowNote + body +
    (byArm.has("EXCEPTION") ? exceptionBlock(armSummary(byArm.get("EXCEPTION")), flags.get("EXCEPTION") || []) : "");

  teardownCharts();
  for (const summary of summaries) {
    const container = el(`chart-arm-${summary.arm}`);
    if (!container) continue;
    const chart =
      summary.arm === "C" && summary.shadow
        ? shadowOverlayChart(container, summary.curve, summary.shadow)
        : equityChart(container, summary.curve, { color: summary.color });
    if (chart) charts.push(chart);
  }
  if (summaries.some((summary) => summary.arm === "C" && summary.shadow)) {
    const node = el("arms-body");
    const legend = document.createElement("div");
    legend.className = "chart-legend";
    legend.innerHTML =
      `<span class="lg"><i style="background:${ARM_COLOR.C}"></i>arm C realized</span>` +
      `<span class="lg"><i class="dashed" style="background:${ARM_COLOR.EXCEPTION}"></i>shadow roll (hypothetical, not a position)</span>`;
    node.appendChild(legend);
  }
  el("arms-stamp").textContent = `arms · updated ${new Date()
    .toLocaleTimeString("en-GB", { hour12: false })}`;
}

export async function loadArms() {
  if (inFlight) return;
  inFlight = true;
  try {
    render(await getJSON("/api/arms"));
  } catch (error) {
    el("arms-body").innerHTML = `<div class="banner bad">/api/arms failed: ${esc(
      String(error.message || error),
    )}</div>`;
    el("arms-stamp").textContent = "arms · error";
  } finally {
    inFlight = false;
  }
}

export function startArms() {
  stopArms();
  loadArms();
  timer = setInterval(() => {
    if (!document.hidden) loadArms();
  }, POLL_MS);
}

export function stopArms() {
  if (timer) clearInterval(timer);
  timer = null;
  teardownCharts();
}
