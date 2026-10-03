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
import {
  armSummary,
  mismatchesByArm,
  ARM_COLOR,
  ARM_LABEL,
  statRow,
  emptyState,
  disclosure,
} from "./model.js";
import { equityChart, shadowOverlayChart } from "./charts.js";

// UX PASS 1: per-arm subtitles, read from the rulebook's own rulings rather than
// invented here. Arm B is inert until T5's IV warmup clears (ratification R5) and arm
// C's entry window was declared 09:45-15:30 ET (R6); both are the kind of thing a
// reader otherwise has to go and look up, and neither is a number this page can derive.
// They are labels, not state: the arm's actual positions are the source of truth for
// what happened, and these say what the rulebook permits.
const ARM_SUBTITLE = {
  A: "control",
  B: "inert until T5 warms",
  C: "entry window 09:45-15:30 ET",
};

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

/**
 * UX PASS 1: the A/B/C comparison, as shared metric ROWS against arm COLUMNS.
 *
 * The old layout was three self-contained cards, which is the right shape for a chart
 * and the wrong shape for a comparison: to ask "is B beating A" the eye had to hold
 * three cards in memory and subtract. A row is a metric and a column is an arm, so the
 * comparison is a horizontal glance and the control is the first column every time.
 * Per-arm subtitle chips carry what the rulebook permits; the numbers below are what
 * the journal recorded, and the two are never merged.
 */
function comparisonTable(summaries) {
  const head = `<tr><th>metric</th>${summaries
    .map(
      (summary) =>
        `<th class="arm-col" style="--arm:${esc(summary.color)}">${esc(summary.arm)}
          <div class="arm-sub">${esc(ARM_SUBTITLE[summary.arm] || "")}</div></th>`,
    )
    .join("")}</tr>`;

  const rows = [
    ["bankroll", (s) => `<span class="num">${dollars(s.bankroll)}</span>`],
    ["deployed", (s) => `<span class="num">${dollars(s.deployed)}</span>`],
    ["cash", (s) => `<span class="num">${s.cash == null ? DASH : dollars(s.cash)}</span>`],
    [
      "realized P&L",
      (s) => `<span class="num ${toneClass(s.pnl)}">${usd(s.pnl)}</span>`,
    ],
    [
      "return on bankroll",
      (s) => `<span class="num ${toneClass(s.returnOnBankroll)}">${pct(s.returnOnBankroll)}</span>`,
    ],
    [
      "vs control",
      (s) =>
        s.isControl
          ? `<span class="muted">this IS the control</span>`
          : `<span class="num ${toneClass(s.vsControl)}">${usd(s.vsControl)}</span>`,
    ],
    ["closed / open", (s) => `<span class="num">${s.closed} / ${s.openCount}</span>`],
  ]
    .map(
      ([label, pick]) =>
        `<tr><th class="row-label">${esc(label)}</th>${summaries
          .map((summary) => `<td>${pick(summary)}</td>`)
          .join("")}</tr>`,
    )
    .join("");

  return `<table class="cmp"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

/** UX PASS 1: the tab's headline numbers, above the table rather than inside it. */
function renderSummary(summaries) {
  const by = (arm) => summaries.find((summary) => summary.arm === arm);
  const control = summaries.find((summary) => summary.isControl) || by("A");
  const bankroll = summaries
    .map((summary) => summary.bankroll)
    .filter(isNum)
    .reduce((acc, value) => acc + value, 0);
  const deployed = summaries.reduce((acc, summary) => acc + (summary.deployed || 0), 0);
  const openCount = summaries.reduce((acc, summary) => acc + summary.openCount, 0);
  const live = summaries.filter((summary) => summary.windowOpen);
  const best = summaries
    .filter((summary) => !summary.isControl && isNum(summary.vsControl))
    .sort((a, b) => b.vsControl - a.vsControl)[0];

  el("arms-summary").innerHTML = statRow([
    {
      label: "bankroll",
      value: summaries.length ? dollars(bankroll) : DASH,
      sub: summaries.length ? `${summaries.length} arms` : "no arms",
      title: "sum of the declared per-arm bankrolls",
    },
    {
      label: "deployed",
      value: dollars(deployed),
      sub: openCount ? `${openCount} open position(s)` : "nothing deployed",
      title: "notional of open positions, from the journal",
    },
    {
      label: "realized P&L",
      value: usd(control ? control.pnl : null),
      sub: control ? `arm ${control.arm}, the control` : DASH,
      tone: control ? toneClass(control.pnl) : "",
    },
    {
      label: "vs control",
      value: best ? usd(best.vsControl) : DASH,
      sub: best ? `arm ${best.arm}, best of the rest` : "no scored history",
      tone: best ? toneClass(best.vsControl) : "",
      title: "best non-control arm against the control's realized P&L",
    },
    {
      label: "window",
      value: live.length ? "open" : "not open",
      tone: live.length ? "up" : "",
      // PASS 2: a word, not a figure — see statCard's `word` flag.
      word: true,
      sub: live.length ? `${live.length} arm(s) active` : "no positions yet",
      title: "an arm is live when it has a position or a close in the journal",
    },
  ]);
}

/**
 * UX PASS 1: an open-position card, for one arm that actually has positions.
 *
 * The metric lines that used to live here (bankroll, deployed, cash, return, vs
 * control) moved to the comparison table — printing the same number twice on one tab
 * is how a reader ends up unsure which one is current. What stays here is what the
 * table cannot say: the individual contracts, and this arm's integrity flags.
 */
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
            `<td class="r num dash">${DASH}</td>` +
            `<td class="r num dash" title="${esc(position.notes)}">${DASH}</td></tr>`,
        )
        .join("") +
      `</tbody></table>`
    : "";

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
    ${vsControl}
    <div class="arm-line"><span>entry price / last / unreal</span><b class="muted">no mark in
      the journal — unrealized P&amp;L is not computed</b></div>
    ${positions}
    ${flagHtml}
  </div>`;
}

const pxOf = (n) => (isNum(n) ? n.toFixed(2) : DASH);

function exceptionBlock(summary, flags) {
  const shadow = summary.shadow;
  // UX PASS 1: this used to be a full-width magenta BANNER, i.e. the exception path
  // announced itself as loudly as the actual arm numbers on a tab whose whole point is
  // comparing those numbers. It is a permanent structural note, so it is now a quiet
  // line under the heading — still unmissable to someone reading this block, no longer
  // the loudest thing on the tab. The wording is unchanged because the claim is load
  // bearing: catalyst-clause P&L is never summed into A/B/C.
  return `<div class="arm-card exception" style="--arm:${esc(ARM_COLOR.EXCEPTION)}">
    <div class="arm-head"><span class="arm-name">${esc(ARM_LABEL.EXCEPTION)}</span>
      <span class="arm-pnl ${toneClass(summary.pnl)}">${usd(summary.pnl)}</span></div>
    <div class="arm-line"><span>bankroll</span><b class="num">${dollars(summary.bankroll)}</b></div>
    <div class="arm-line"><span>closed / open</span><b class="num">${summary.closed} / ${
      summary.openCount
    }</b></div>
    <div class="arm-note">rendered separately on purpose — catalyst-clause P&amp;L is
      never summed into A/B/C, and a chart that added it would be lying about the arms.</div>
    ${summary.positions.length ? `<table class="mini"><tbody>${summary.positions
      .map(
        (position) =>
          `<tr><td>${esc(position.label)}</td><td class="r num">${esc(
            position.qty ?? DASH,
          )}</td><td class="r num">${pxOf(position.entry)}</td></tr>`,
      )
      .join("")}</tbody></table>` : ""}
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

  // UX PASS 1: the two amber blocks (the broker-cross-check caveat and the
  // insufficient-history note) become ONE collapsed "Integrity notes" disclosure.
  // Neither was ever a finding: one says a check was never performed, the other says
  // the window is young. As full-width banners they were the first two things on the
  // tab and the biggest type on it, which made permanent caveats read as live warnings.
  // A real flag still gets a badge on the disclosure label, so it is findable.
  const flagList = (payload.mismatches || []).length
    ? `<ul class="mp-list">${payload.mismatches
        .map(
          (flag) =>
            `<li><span class="mk">${esc(flag.arm || "?")} ${esc(flag.code || "")}</span>
             <span class="mp-detail ${esc(flag.severity || "")}">${esc(flag.detail || "")}</span></li>`,
        )
        .join("")}</ul>`
    : `<div class="mp-detail">no integrity flags: the journal is internally consistent.</div>`;
  const integrity = disclosure({
    label: "Integrity notes",
    count: flagCount,
    tone: flagCount ? "bad" : "",
    hint: flagCount
      ? `${flagCount} flag${flagCount === 1 ? "" : "s"} — read these`
      : "journal-internal only, no broker cross-check",
    body:
      `<div class="mp-detail"><b>Broker cross-check:</b> ${
        crossCheck.broker_checked
          ? "journal vs broker"
          : "journal-internal only. NO broker cross-check was performed and none is implied."
      } ${esc(crossCheck.note || "")}</div>` +
      `<div class="mp-detail" style="margin-top:4px"><b>Scored comparison:</b> ${
        payload.sufficient
          ? "enough history for a scored comparison."
          : `insufficient history for a scored comparison${
              payload.reason ? `: ${esc(payload.reason)}` : ""
            }`
      }</div>` +
      `<div style="margin-top:6px">${flagList}</div>`,
  });

  // Position cards render only when there are positions to describe; the comparison
  // table carries the tab otherwise. An empty positions table repeated three times is
  // three identical statements that nothing has happened yet.
  const withPositions = summaries.filter((summary) => summary.positions.length);
  // The equity curves used to live inside those cards, so hiding the cards would have
  // hidden the curves too. They move into their own collapsed block that renders only
  // when at least one arm has a close to plot: a chart with nothing to draw is the one
  // thing on this tab that genuinely cannot be faked.
  const withCurve = summaries.filter((summary) => summary.curve.length);
  const curves = withCurve.length
    ? `<h3 class="panel">realized equity</h3><div class="arm-grid">${withCurve
        .map(
          (summary) => `<div class="arm-card" data-arm="${esc(summary.arm)}" style="--arm:${esc(summary.color)}">
            <div class="arm-head"><span class="arm-name">${esc(summary.label)}</span>
              <span class="arm-pnl ${toneClass(summary.pnl)}">${usd(summary.pnl)}</span></div>
            <div class="arm-chart" id="chart-arm-${esc(summary.arm)}"></div>
            <div class="arm-curve-note">realized equity over ${summary.curve.length} close(s), last ${esc(
              clockET(summary.curve[summary.curve.length - 1].t),
            )}</div>
          </div>`,
        )
        .join("")}</div>`
    : "";
  const body = summaries.length
    ? comparisonTable(summaries) + curves +
      (withPositions.length
        ? `<h3 class="panel">open positions</h3><div class="arm-grid">${withPositions
            .map((summary) => card(summary, flags.get(summary.arm) || []))
            .join("")}</div>`
        : "")
    : emptyState(
        "No arms in the journal yet",
        payload.note ||
          "the journal holds no arm meta yet — arms appear once the executor has " +
            "journalled a decision. This is the expected state before the first session.",
      );

  el("arms-body").innerHTML = integrity + body +
    (byArm.has("EXCEPTION") ? exceptionBlock(armSummary(byArm.get("EXCEPTION")), flags.get("EXCEPTION") || []) : "");
  renderSummary(summaries);
  el("arms-summary").hidden = false;

  teardownCharts();
  for (const summary of withCurve) {
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
