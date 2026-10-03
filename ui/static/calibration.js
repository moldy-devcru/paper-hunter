// Calibration page (U4): the T6 story in pictures — per-session deep-OTM call/put volume
// with the accumulating baseline mean and the forming P90, the 1pp threshold-distance
// histogram with the working 3% threshold marked, the IV history with proxy provenance
// separated from real observations, and the window-start checklist rendered from what the
// store can actually prove.
//
// One endpoint (`/api/calibration`) and four panels. The panels are rendered from pure
// view models in model.js so the arithmetic is scoreable from Node; this file owns the
// DOM, the polling and the honest empty states.
//
// # INTERPRETATION — the distance histogram is plain DIVs, not a chart. Its whole point
// is a VERTICAL rule at the threshold: the threshold is a distance (an x-axis quantity),
// and a price line in a charting library is horizontal, so marking it would have meant
// either drawing the wrong axis or hand-rolling an overlay. Stacked flexbox with one
// marker div is less machinery and marks the right thing. Same reasoning as the veto
// histogram on the Hunt page, same conclusion for a different reason.

import { getJSON } from "./net.js";
import { esc, vol, DASH, isNum } from "./format.js";
import {
  flowVolume,
  proposedN,
  distanceHistogram,
  ivHistory,
  windowChecklist,
  statRow,
  emptyState,
  disclosure,
  WORKING_DEEP_OTM_PCT,
  MIN_IV_OBSERVATIONS,
} from "./model.js";
import {
  flowVolumeChart,
  ivHistoryChart,
  PROVENANCE_COLOR,
  CALL_COLOR,
  PUT_COLOR,
} from "./charts.js";

const POLL_MS = 120_000;
const el = (id) => document.getElementById(id);
const state = { timer: null, inFlight: false, charts: [] };

// UX PASS 1: a cell whose whole content is an em dash is the ABSENCE of a measurement,
// and rendering it in the same weight as a number is what made the calibration tables
// read as dense. `dashCell` mutes it; `cellNum` wraps a real number. Dash-only columns
// are still shown (hiding a whole column because it is empty moves the layout under the
// reader mid-poll) but they stop competing for attention.
// PASS 2: the muted treatment moved into one `dash` class in the stylesheet, so a dash
// cell here, a dash in an arm line and a dash in a stat subtitle are styled by the same
// rule instead of by whoever remembered to write `muted`.
const dashCell = (value) =>
  `<td class="r num dash" title="no value recorded">${esc(value == null ? DASH : value)}</td>`;
const cellNum = (html) => `<td class="r num">${html}</td>`;

/**
 * UX PASS 2 fix #4: what a panel shows when there is nothing to plot.
 *
 * Pass 1 gave the blank canvas a sentence inside it, which fixed the "is this broken?"
 * impression but kept the rectangle — 220px of dashed box holding one line of text, on
 * a page whose table and fact list were pushed below it. The fix is to give the space
 * back: render no chart container at all, and let the compact empty state plus the
 * numbers below it take the room. `charts.js` returns null rather than an empty canvas,
 * so this is the only place that has to know why.
 */
function compactChartState(line, why = "") {
  return `<div class="chart-state compact"><span>${esc(line)}</span>${
    why ? `<span class="muted"> — ${esc(why)}</span>` : ""
  }</div>`;
}

function teardownCharts() {
  for (const chart of state.charts) {
    try {
      chart.remove();
    } catch (_) {
      /* already gone with its container */
    }
  }
  state.charts = [];
}

// ---------------------------------------------------------------------------
// 1. per-session volume, baseline mean, forming P90
// ---------------------------------------------------------------------------

function renderFlow(payload) {
  const model = flowVolume(payload.flow, payload.p90);
  const n = proposedN(payload.n);
  const nText = n.value == null ? DASH : `${n.value.toFixed(3)}x baseline`;
  const head = `<div class="plan-meta">${model.sessions} session(s) recorded · baseline N=${esc(
    model.sessionsNeeded,
  )} · volume P90 <b class="${esc(model.p90Status === "defined" ? "up" : "pending")}">${esc(
    model.p90Status,
  )}</b> (chart line, contract counts) · <b>proposed multiplier N</b>: <b class="${
    n.enough ? "up" : "pending"
  }">${esc(nText)}</b> from ${esc(n.sessionsQualified)}/${esc(
    n.sessionsConsidered,
  )} qualifying session(s), <b>status=${esc(n.status)}</b>, not frozen</div>`;

  if (model.empty) {
    el("calib-flow").innerHTML =
      head +
      emptyState(
        "No flow_baseline sessions yet",
        (payload.reason || "the soak writes one row per session after the 15:30 close") +
          ". That is a fact, not an error — nothing has run the soak yet.",
      );
    return;
  }

  const first = model.points[0];
  const last = model.points[model.points.length - 1];
  // UX PASS 1: "baseline complete" is an all-clear, and an all-clear in a banner at
  // the top of the tab is the same mistake the Arms and Hunt pages had. It becomes a
  // chip on the status line; the forming case stays a banner because it is a real
  // warning about a number that does not yet mean what it appears to mean.
  // UX PASS 2 fix #1: the all-clear chip had the same "64/20" ambiguity as the card, so
  // it names the same two facts in the same order.
  const status = model.complete
    ? `<span class="chip pass" title="baseline complete (${model.sessions} sessions against the ${model.sessionsNeeded}-session minimum) — the P90 is a real line now, and it still moves until N is frozen">baseline complete · ${model.sessions} sessions, minimum ${model.sessionsNeeded}</span>`
    : `<div class="banner warn">P90 is <b>forming</b> — ${model.sessions} of ${
        model.sessionsNeeded
      } sessions collected, <b>needs ${model.needs} more</b> before it means anything. ` +
      "A P90 drawn off 1–2 sessions is a number about those sessions, not about the market.</div>";

  const facts = `<div class="calib-facts">
    <div><span>applied threshold</span><b>${model.appliedThresholdPct == null ? DASH : `${model.appliedThresholdPct}pp`}</b></div>
    <div><span>trailing mean (${esc(model.sessionsNeeded)}-session)</span><b>${vol(
      model.baselineMeanLatest,
    )}</b></div>
    <div><span>volume P90 (chart line)</span><b>${vol(model.p90Value)}</b></div>
    <div><span>proposed N (P90 of ratios)</span><b>${nText}</b></div>
    <div><span>proposed N call / put</span><b>${
      n.callValue == null ? DASH : `${n.callValue.toFixed(3)}x`
    } / ${n.putValue == null ? DASH : `${n.putValue.toFixed(3)}x`}</b></div>
    <div><span>excluded samples</span><b>${esc(n.excludedCount)}${
      Object.keys(n.excludedByReason).length
        ? ` (${esc(
            Object.entries(n.excludedByReason)
              .map(([reason, count]) => `${reason}: ${count}`)
              .join(", "),
          )})`
        : ""
    }</b></div>
    <div><span>latest session</span><b>${esc(last.date)}</b></div>
    <div><span>spot range</span><b>${vol(first.sessionSpot)} → ${vol(last.sessionSpot)}</b></div>
    <div><span>holes / delayed</span><b>${model.holes} / ${
        model.points.filter((point) => point.isDelayed).length
      }</b></div>
  </div>`;

  // UX PASS 1: the session column is the row identity in every one of these tables, so
  // it freezes with the header — scrolling a 20-session baseline no longer leaves the
  // reader unable to say which row a number belongs to.
  // PASS 2 fix #5: the closing note moved BELOW the table as a `.panel-note`, and the
  // note itself was the thing crowding the last row: it sat directly under the tbody
  // with a few pixels to spare, so the final session line and its caveat read as one
  // crowded block. A note under a table gets its own block of space and a rule.
  const table = `<table class="grid freeze"><thead><tr><th class="sticky-col">session</th><th class="r">call</th>
    <th class="r">put</th><th class="r">total</th><th class="r">trailing mean</th>
    <th class="r">forming P90</th><th class="r">spot</th></tr></thead><tbody>${
      [...model.points]
        .reverse()
        .map(
          (point) =>
            `<tr title="${esc(point.feed || "feed unrecorded")}${
              point.isDelayed ? " · delayed feed" : ""
            }${point.baselineDays == null ? "" : ` · ${point.baselineDays}/${model.sessionsNeeded} baseline days`}">
              <td class="num sticky-col">${esc(point.date)}</td>
              ${isNum(point.call) ? cellNum(`<span style="color:${CALL_COLOR}">${vol(point.call)}</span>`) : dashCell(null)}
              ${isNum(point.put) ? cellNum(`<span style="color:${PUT_COLOR}">${vol(point.put)}</span>`) : dashCell(null)}
              ${isNum(point.total) ? cellNum(vol(point.total)) : dashCell(null)}
              <td class="r num dash">${vol(point.baselineMean)}</td>
              ${isNum(point.formingP90) ? cellNum(`<span style="color:#e05fd0">${vol(point.formingP90)}</span>`) : dashCell(null)}
              ${isNum(point.sessionSpot) ? cellNum(point.sessionSpot.toFixed(2)) : dashCell(null)}
            </tr>`,
        )
        .join("")
    }</tbody></table>`;

  // UX PASS 2 fix #4: the chart container is only in the markup when there is something
  // to draw into it. When the store has sessions but no volume (or the chart library is
  // absent) the panel renders the compact state in its place and the fact list + table
  // reclaim the 220px, instead of a dashed rectangle holding one sentence. `measured` is
  // the model's own count of sessions that carry volume, so the chart's presence cannot
  // disagree with the table's contents.
  const hasVolume = model.measured > 0;
  const chartBlock = hasVolume
    ? `<div class="calib-chart" id="calib-flow-chart"><div class="chart-state" id="calib-flow-state">plotting…</div></div>` +
      `<div class="chart-legend">
       <span class="lg"><i style="background:${CALL_COLOR}"></i>deep-OTM call volume</span>
       <span class="lg"><i style="background:${PUT_COLOR}"></i>deep-OTM put volume</span>
       <span class="lg"><i style="background:#8f7bff"></i>trailing ${esc(
         model.sessionsNeeded,
       )}-session mean</span>
       <span class="lg"><i class="dashed" style="background:#e05fd0"></i>volume P90 (${esc(
         model.p90Status,
       )})</span>
     </div>`
    : compactChartState(
        "no volume series to plot yet",
        `${model.sessions} session row(s) recorded, none carrying deep-OTM volume — the numbers are in the table below`,
      );

  el("calib-flow").innerHTML =
    head + status + facts + chartBlock + table +
    `<div class="panel-note">the mean line is a short-window mean until session ${
      model.fullFrom + 1
    } · forming P90 is recomputed from scratch on every session, so it is a line through what has been collected, not today's value extended backwards</div>`;

  if (!hasVolume) return;
  const chart = flowVolumeChart(el("calib-flow-chart"), model);
  if (chart) {
    state.charts.push(chart);
    // The chart drew over its container; drop the placeholder state.
    const placeholder = el("calib-flow-state");
    if (placeholder) placeholder.remove();
  } else {
    // The library is missing or the model carried no series after all: swap the
    // reserved canvas for the compact state rather than leaving an empty box.
    const canvas = el("calib-flow-chart");
    if (canvas) canvas.replaceWith(compactChartState("no volume series to plot yet"));
  }
}

// ---------------------------------------------------------------------------
// 2. threshold-distance histogram
// ---------------------------------------------------------------------------

function renderDistance(payload) {
  const applied =
    (payload.flow || []).find((row) => isNum(row.deep_otm_threshold_pct)) || {};
  const model = distanceHistogram(
    payload.distance_histogram,
    isNum(applied.deep_otm_threshold_pct)
      ? applied.deep_otm_threshold_pct
      : WORKING_DEEP_OTM_PCT,
  );
  const source = isNum(applied.deep_otm_threshold_pct)
    ? `threshold read from flow_baseline.deep_otm_threshold_pct (${model.threshold}pp)`
    : `no session recorded yet — showing the soak's working threshold (${model.threshold}pp)`;

  if (model.empty) {
    el("calib-distance").innerHTML = `<div class="plan-meta">${esc(source)}</div>` +
      emptyState(
        "No distance buckets stored",
        "The soak writes one 1pp bucket per percentage point from spot. Nothing has " +
          "written them yet, so there is no deep-vs-near split to show.",
      );
    return;
  }

  const rows = model.buckets
    .map((bucket) => {
      const atOrBeyond = bucket.pct >= model.threshold;
      return `<div class="dbar ${atOrBeyond ? "deep" : "near"}" title="${bucket.pct}pp from spot: call ${vol(
        bucket.call,
      )}, put ${vol(bucket.put)}${atOrBeyond ? " — counted as deep-OTM by the soak" : ""}">
        <div class="dpair">
          <i style="flex:${bucket.call || 0.0001};background:${CALL_COLOR}"></i>
          <i style="flex:${bucket.put || 0.0001};background:${PUT_COLOR}"></i>
        </div>
        <span class="hlabel">${bucket.pct}</span>
        <span class="hcount">${vol(Math.max(bucket.call, bucket.put))}</span>
        ${bucket.pct === Math.floor(model.threshold) ? '<span class="dmark">▲ threshold</span>' : ""}
      </div>`;
    })
    .join("");

  el("calib-distance").innerHTML =
    `<div class="plan-meta">${esc(source)} · ${esc(model.note)}</div>` +
    `<div class="calib-facts">
      <div><span>deep-OTM volume (≥${model.threshold}pp)</span><b>${vol(model.deepVolume)}</b></div>
      <div><span>inside the band (&lt;${model.threshold}pp)</span><b>${vol(model.nearVolume)}</b></div>
      <div><span>calls / puts</span><b>${vol(model.callTotal)} / ${vol(model.putTotal)}</b></div>
      <div><span>buckets</span><b>${model.buckets.length} × 1pp</b></div>
    </div>` +
    `<div class="dist" title="call above, put below, per 1pp bucket">${rows}</div>` +
    `<div class="chart-legend">
       <span class="lg"><i style="background:${CALL_COLOR}"></i>calls</span>
       <span class="lg"><i style="background:${PUT_COLOR}"></i>puts</span>
       <span class="lg"><i style="background:#f85149"></i>▲ = the threshold the soak applied</span>
     </div>` +
    `<div class="plan-meta">bars are linear in volume within each pair, so a tiny bucket can look flat — the tooltip and the table carry the number</div>`;
}

// ---------------------------------------------------------------------------
// 3. IV history with provenance
// ---------------------------------------------------------------------------

function renderIv(payload) {
  const model = ivHistory((payload.ivrank || {}).series || [], {
    minObservations: MIN_IV_OBSERVATIONS,
  });
  const source = (payload.ivrank || {}).source_note || "";
  if (model.empty) {
    el("calib-iv").innerHTML = emptyState(
      "No IV observations in the store",
      (source || "") +
        " — before the soak runs and before the VIX seed is backfilled, T5 has nothing " +
        "to read, and the warmup count it blocks on is zero.",
    );
    return;
  }
  const legend = model.series
    .map(
      (entry) =>
        `<span class="lg"><i class="${entry.isProxy ? "dashed" : ""}" style="background:${
          entry.isProxy ? PROVENANCE_COLOR.proxy : PROVENANCE_COLOR.real
        }"></i>${esc(entry.underlying)} ${esc(entry.tenorKey)} — ${
          entry.proxyPoints
            ? `${entry.proxyPoints} proxy / ${entry.realPoints} real`
            : `${entry.realPoints} real`
        }</span>`,
    )
    .join("");
  const seam = model.seam;
  const seamBanner =
    seam.state === "ordered"
      ? `<div class="banner ok">seam visible: proxy seed ends <b>${esc(
          String(seam.proxyLast),
        )}</b>, real observations begin <b>${esc(String(seam.realFirst))}</b>. Still needs a
          human to confirm no proxy row can reach a real SPY rank.</div>`
      : seam.state === "no_proxy_seed"
        ? `<div class="banner warn">no <code>is_proxy</code> rows in the store — the VIX warmup
          seed has not been backfilled, so there is no proxy/real seam to reconcile.</div>`
        : seam.state === "no_real_observations"
          ? `<div class="banner warn">proxy seed ends <b>${esc(String(seam.proxyLast))}</b> but
          no real chain observation exists yet: warmup is entirely synthetic.</div>`
          : `<div class="banner bad">real observations begin <b>${esc(String(seam.realFirst))}</b>,
          on or before the proxy seed ends (<b>${esc(String(seam.proxyLast))}</b>) — overlapping
          or reversed provenance.</div>`;
  const conflicts = model.guardConflicts.length
    ? `<div class="banner bad"><b>${model.guardConflicts.length} provenance guard conflict(s)</b> —
       the <code>is_proxy</code> flag and the <code>VIX</code> underlying key disagree:
       <ul class="mp-list">${model.guardConflicts
         .slice(0, 8)
         .map(
           (conflict) =>
             `<li><span class="mk">${esc(conflict.as_of)}</span>
              <span class="mp-detail">${esc(conflict.underlying)} — ${esc(conflict.note)}</span></li>`,
         )
         .join("")}</ul></div>`
    : "";
  const warm = model.warm
    ? `<span class="chip pass" title="warmup reached: ${model.realCount} real observation(s) ≥ MIN_OBSERVATIONS=${model.minObservations}">warmup reached ${model.realCount}/${model.minObservations}</span>`
    : `<div class="banner ${model.realCount ? "warn" : "bad"}">warmup <b>not</b> reached:
       ${model.realCount}/${model.minObservations} real observation(s)${
         model.proxyCount
           ? ` — ${model.proxyCount} proxy row(s) are warmup seed and do not count`
           : ""
       }. T5 reports PENDING and blocks until this clears.</div>`;

  // UX PASS 2 fix #4, same rule as the flow panel: the 200px canvas exists only when
  // there is a real chain line to draw into it. A proxy-only warmup is a STATE the
  // banners above already report in words, so it does not also need a rectangle.
  const hasReal = model.realCount > 0;
  const chartBlock = hasReal
    ? `<div class="calib-chart" id="calib-iv-chart"><div class="chart-state" id="calib-iv-state">plotting…</div></div>`
    : compactChartState(
        "no real chain observations to plot",
        "the proxy seed is warmup, not the chain — it is counted in the table below and does not draw a line",
      );

  el("calib-iv").innerHTML =
    `<div class="plan-meta">${esc(source)} · proxy segments are drawn dashed AND in a
     different colour: colour alone fails a monochrome print, and the seed is not the chain</div>` +
    seamBanner + conflicts + warm +
    `<div class="chart-legend">${legend}</div>` +
    chartBlock +
    `<table class="grid freeze"><thead><tr><th class="sticky-col">underlying</th><th>tenor key</th>
      <th class="r">real</th><th class="r">proxy</th><th>first</th><th>last</th>
      <th class="r">latest IV</th></tr></thead><tbody>${
        model.series
          .map(
            (entry) =>
              `<tr><td class="sticky-col">${esc(entry.underlying)}${
                entry.isProxy ? ' <span class="chip pending">proxy seed</span>' : ""
              }</td><td>${esc(entry.tenorKey)}</td><td class="r num">${entry.realPoints}</td>
               <td class="r num ${entry.proxyPoints ? "" : "dash"}">${entry.proxyPoints}</td>
               <td class="num">${esc(entry.first || DASH)}</td>
               <td class="num">${esc(entry.last || DASH)}</td>
               <td class="r num ${entry.latest && isNum(entry.latest.iv) ? "" : "dash"}">${
                 entry.latest && isNum(entry.latest.iv) ? entry.latest.iv.toFixed(2) : DASH
               }</td></tr>`,
          )
          .join("")
      }</tbody></table>`;

  if (!hasReal) return;
  const chart = ivHistoryChart(el("calib-iv-chart"), model);
  if (chart) {
    state.charts.push(chart);
    const placeholder = el("calib-iv-state");
    if (placeholder) placeholder.remove();
  } else {
    const canvas = el("calib-iv-chart");
    if (canvas) canvas.replaceWith(compactChartState("no real chain observations to plot"));
  }
}

// ---------------------------------------------------------------------------
// 4. window-start checklist
// ---------------------------------------------------------------------------

const CHECK_GLYPH = { done: "✅", blocked: "⛔", progress: "🟡", todo: "⬜", unknown: "❔" };
const CHECK_TITLE = {
  done: "provable from the store",
  blocked: "the store proves this is not true yet",
  progress: "partway — the remaining count is in the reason",
  todo: "not assessable from here; a human or a code fact decides it",
  unknown: "not enough data to say anything",
};

function renderChecklist(payload) {
  const model = windowChecklist(payload);
  const rows = model.items
    .map(
      (item) =>
        `<tr class="chk ${esc(item.state)}"><td class="glyph">${CHECK_GLYPH[item.state]}</td>
         <td><b>${esc(item.label)}</b><div class="chk-reason" title="${esc(
           CHECK_TITLE[item.state],
         )}">${esc(item.reason)}</div></td>
         <td class="state">${esc(item.state)}</td></tr>`,
    )
    .join("");
  const pending = payload.pending_calibrations || [];
  // UX PASS 1: the read-only banner was the loudest thing on the tab and it was the
  // same sentence every load — a permanent caveat, not a finding. The live part of it
  // (the counts) is now a status line with badges, and the explanation is a disclosure.
  const blocked = model.counts.blocked || 0;
  const statusLine = `<div class="plan-meta checklist-status">
    <span class="chip ${model.ready ? "pass" : blocked ? "fail" : "pending"}">${esc(
      model.summary,
    )}</span>
    <span class="muted">read-only status view — nothing here can be ticked</span>
  </div>`;
  const notes = disclosure({
    label: "About this checklist",
    body:
      `<div class="mp-detail">Ticking nothing here is the normal state: this page can only ` +
      `report what the store proves, and the ratification's box is ticked by a human. ` +
      `No item here can be checked off from this screen.</div>` +
      `<div class="mp-detail" style="margin-top:4px">${
        pending.length
          ? `Rulebook.pending_calibrations (read straight from the YAML): ${pending
              .map((entry) => `<code>${esc(entry)}</code>`)
              .join(", ")}`
          : "Rulebook.pending_calibrations is empty"
      }</div>` +
      `<div class="mp-detail" style="margin-top:4px">strategy_version ${esc(
        payload.strategy_version || DASH,
      )} — the checklist itself is the operator's document (docs/ratification.md §(d)) ` +
      `and this page never edits it.</div>`,
  });
  el("calib-checklist").innerHTML =
    statusLine +
    `<table class="grid chk-table"><tbody>${rows}</tbody></table>` +
    notes;
}

/**
 * UX PASS 1: the tab's headline numbers.
 *
 * The status card is the one that matters: `calibration_pending` vs `sufficient` is the
 * question T6 asks, and it used to be reachable only by reading a status string buried
 * mid-paragraph in the flow panel. It is derived from the same `n` block the panel
 * renders, not recomputed, so the two can never disagree.
 *
 * UX PASS 2 fixes #1 and #2 live here:
 *   #1 the baseline card read "64/20", which looks like a broken fraction rather than
 *      64 sessions against a 20-session minimum — the number and its requirement are
 *      now two labelled facts ("64 sessions" / "minimum 20 · needs 44 more").
 *   #2 the proposed-N subtitle was long enough to ellipsise, cutting the words that
 *      carry the caveat. It is shorter now, and `.stat-sub` wraps to two lines rather
 *      than truncating, so no subtitle on any tab can lose its qualifier again.
 */
function renderSummary(payload) {
  const flow = flowVolume(payload.flow, payload.p90);
  const n = proposedN(payload.n);
  const iv = ivHistory((payload.ivrank || {}).series || [], {
    minObservations: MIN_IV_OBSERVATIONS,
  });
  const status = n.status === "sufficient" ? "sufficient" : "calibration_pending";
  const last = flow.points.length ? flow.points[flow.points.length - 1] : null;
  el("calib-summary").innerHTML = statRow([
    {
      label: "calibration",
      value: status,
      tone: status === "sufficient" ? "up" : "pending",
      word: true,
      sub: n.status === "sufficient" ? `from ${n.samples} sample(s)` : "not enough sessions",
      title: "sufficient = the proposed N can be scored from the sessions on record",
    },
    {
      label: "proposed N",
      value: n.value == null ? DASH : `${n.value.toFixed(3)}x`,
      sub: "× the baseline mean, not frozen",
      title: "P90 of the per-session ratios; a ratio is dimensionless, a volume P90 is not",
    },
    {
      label: "qualified sessions",
      value: `${n.sessionsQualified}/${n.sessionsConsidered}`,
      sub: n.excludedCount ? `${n.excludedCount} sample(s) excluded` : "none excluded",
      title: "sessions that qualified for the ratio distribution, of those considered",
    },
    {
      // FIX #1: "64/20" was ambiguous — a fraction, a ratio, or a session count? The
      // sessions are the number; the minimum they have to clear is the subtitle, named
      // as a minimum rather than left as a second number in the same slot.
      label: "baseline sessions",
      value: `${flow.sessions}`,
      sub: flow.complete
        ? `minimum ${flow.sessionsNeeded} met`
        : `minimum ${flow.sessionsNeeded} · needs ${flow.needs} more`,
      tone: flow.complete ? "up" : "pending",
      title: `${flow.sessions} session(s) recorded against the ${flow.sessionsNeeded}-session baseline minimum`,
    },
    {
      label: "latest session",
      value: last ? last.date : DASH,
      // PASS 2: a date is a figure, but a short one — the value slot is sized for
      // money, and a 10-character date at 22px mono crowds the tile's edge.
      date: true,
      sub: last ? `${vol(last.total)} deep-OTM contracts` : "no sessions yet",
    },
    {
      label: "IV warmup",
      value: `${iv.realCount}/${iv.minObservations}`,
      sub: iv.warm ? "T5 can pass" : "T5 reports PENDING",
      tone: iv.warm ? "up" : "pending",
      title: "real chain observations against MIN_OBSERVATIONS; proxy seed does not count",
    },
  ]);
}

// ---------------------------------------------------------------------------
// load cycle
// ---------------------------------------------------------------------------

function render(payload) {
  teardownCharts();
  renderSummary(payload);
  renderFlow(payload);
  renderDistance(payload);
  renderIv(payload);
  renderChecklist(payload);
  el("calib-stamp").textContent = `calibration · ${
    payload.flow_sessions || 0
  } session(s) · updated ${new Date().toLocaleTimeString("en-GB", { hour12: false })}`;
}

export async function loadCalibration() {
  if (state.inFlight) return;
  state.inFlight = true;
  try {
    render(await getJSON("/api/calibration"));
  } catch (error) {
    el("calib-flow").innerHTML = `<div class="banner bad">/api/calibration failed: ${esc(
      String(error.message || error),
    )}</div>`;
    el("calib-distance").innerHTML = `<div class="empty">no data</div>`;
    el("calib-iv").innerHTML = `<div class="empty">no data</div>`;
    el("calib-checklist").innerHTML = `<div class="empty">no data</div>`;
    el("calib-stamp").textContent = "calibration · error";
  } finally {
    state.inFlight = false;
  }
}

export function startCalibration() {
  stopCalibration();
  loadCalibration();
  // 120s, not the terminal's 45s: this panel is a per-session-soak record, not a tick
  // chart, and nothing here changes faster than a 17:30 ET job writes a row.
  state.timer = setInterval(() => {
    if (!document.hidden) loadCalibration();
  }, POLL_MS);
}

export function stopCalibration() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  teardownCharts();
}