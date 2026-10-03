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
  distanceHistogram,
  ivHistory,
  windowChecklist,
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
  const head = `<div class="plan-meta">${model.sessions} session(s) recorded · N=${esc(
    model.sessionsNeeded,
  )} · P90 <b class="${esc(model.p90Status === "defined" ? "up" : "pending")}">${esc(
    model.p90Status,
  )}</b> · percentile: ${esc(model.p90Method)}</div>`;

  if (model.empty) {
    el("calib-flow").innerHTML =
      head +
      `<div class="empty big">no flow_baseline sessions yet. ${esc(
        payload.reason || "the soak writes one row per session after the 15:30 close",
      )}. That is a fact, not an error — nothing has run the soak yet.</div>`;
    return;
  }

  const first = model.points[0];
  const last = model.points[model.points.length - 1];
  const status = model.complete
    ? `<div class="banner ok">baseline complete (${model.sessions}/${model.sessionsNeeded}) — ` +
      `the P90 is a real line now, and it still moves until N is frozen.</div>`
    : `<div class="banner warn">P90 is <b>forming</b> — ${model.sessions} of ${
        model.sessionsNeeded
      } sessions collected, <b>needs ${model.needs} more</b> before it means anything. ` +
      "A P90 drawn off 1–2 sessions is a number about those sessions, not about the market.</div>";

  const facts = `<div class="calib-facts">
    <div><span>applied threshold</span><b>${model.appliedThresholdPct == null ? DASH : `${model.appliedThresholdPct}pp`}</b></div>
    <div><span>trailing mean (${esc(model.sessionsNeeded)}-session)</span><b>${vol(
      model.baselineMeanLatest,
    )}</b></div>
    <div><span>P90</span><b>${vol(model.p90Value)}</b></div>
    <div><span>latest session</span><b>${esc(last.date)}</b></div>
    <div><span>spot range</span><b>${vol(first.sessionSpot)} → ${vol(last.sessionSpot)}</b></div>
    <div><span>holes / delayed</span><b>${model.holes} / ${
        model.points.filter((point) => point.isDelayed).length
      }</b></div>
  </div>`;

  const table = `<table class="grid"><thead><tr><th>session</th><th class="r">call</th>
    <th class="r">put</th><th class="r">total</th><th class="r">trailing mean</th>
    <th class="r">forming P90</th><th class="r">spot</th></tr></thead><tbody>${
      [...model.points]
        .reverse()
        .map(
          (point) =>
            `<tr title="${esc(point.feed || "feed unrecorded")}${
              point.isDelayed ? " · delayed feed" : ""
            }${point.baselineDays == null ? "" : ` · ${point.baselineDays}/${model.sessionsNeeded} baseline days`}">
              <td class="num">${esc(point.date)}</td>
              <td class="r num" style="color:${CALL_COLOR}">${vol(point.call)}</td>
              <td class="r num" style="color:${PUT_COLOR}">${vol(point.put)}</td>
              <td class="r num">${vol(point.total)}</td>
              <td class="r num muted">${vol(point.baselineMean)}</td>
              <td class="r num" style="color:#e05fd0">${vol(point.formingP90)}</td>
              <td class="r num muted">${point.sessionSpot == null ? DASH : point.sessionSpot.toFixed(2)}</td>
            </tr>`,
        )
        .join("")
    }</tbody></table>`;

  el("calib-flow").innerHTML =
    head + status + facts + `<div class="calib-chart" id="calib-flow-chart"></div>` +
    `<div class="chart-legend">
       <span class="lg"><i style="background:${CALL_COLOR}"></i>deep-OTM call volume</span>
       <span class="lg"><i style="background:${PUT_COLOR}"></i>deep-OTM put volume</span>
       <span class="lg"><i style="background:#8f7bff"></i>trailing ${esc(
         model.sessionsNeeded,
       )}-session mean</span>
       <span class="lg"><i class="dashed" style="background:#e05fd0"></i>P90 (${esc(
         model.p90Status,
       )})</span>
     </div>` +
    `<div class="plan-meta">the mean line is a short-window mean until session ${
      model.fullFrom + 1
    } · forming P90 is recomputed from scratch on every session, so it is a line through what has been collected, not today's value extended backwards</div>` +
    table;

  const chart = flowVolumeChart(el("calib-flow-chart"), model);
  if (chart) state.charts.push(chart);
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
    el("calib-distance").innerHTML = `<div class="plan-meta">${esc(source)}</div>
      <div class="empty big">no distance buckets stored. The soak writes one 1pp bucket per
      percentage point from spot; nothing has written them yet.</div>`;
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
    el("calib-iv").innerHTML = `<div class="empty big">no IV observations in the store. ${esc(
      source,
    )} — before the soak runs and before the VIX seed is backfilled, T5 has nothing to read.</div>`;
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
    ? `<div class="banner ok">warmup reached: ${model.realCount} real observation(s) ≥
       MIN_OBSERVATIONS=${model.minObservations}.</div>`
    : `<div class="banner ${model.realCount ? "warn" : "bad"}">warmup <b>not</b> reached:
       ${model.realCount}/${model.minObservations} real observation(s)${
         model.proxyCount
           ? ` — ${model.proxyCount} proxy row(s) are warmup seed and do not count`
           : ""
       }. T5 reports PENDING and blocks until this clears.</div>`;

  el("calib-iv").innerHTML =
    `<div class="plan-meta">${esc(source)} · proxy segments are drawn dashed AND in a
     different colour: colour alone fails a monochrome print, and the seed is not the chain</div>` +
    seamBanner + conflicts + warm +
    `<div class="chart-legend">${legend}</div>` +
    `<div class="calib-chart" id="calib-iv-chart"></div>` +
    `<table class="grid"><thead><tr><th>underlying</th><th>tenor key</th>
      <th class="r">real</th><th class="r">proxy</th><th>first</th><th>last</th>
      <th class="r">latest IV</th></tr></thead><tbody>${
        model.series
          .map(
            (entry) =>
              `<tr><td>${esc(entry.underlying)}${
                entry.isProxy ? ' <span class="chip pending">proxy seed</span>' : ""
              }</td><td>${esc(entry.tenorKey)}</td><td class="r num">${entry.realPoints}</td>
               <td class="r num ${entry.proxyPoints ? "" : "muted"}">${entry.proxyPoints}</td>
               <td class="num">${esc(entry.first || DASH)}</td>
               <td class="num">${esc(entry.last || DASH)}</td>
               <td class="r num">${
                 entry.latest && isNum(entry.latest.iv) ? entry.latest.iv.toFixed(2) : DASH
               }</td></tr>`,
          )
          .join("")
      }</tbody></table>`;

  const chart = ivHistoryChart(el("calib-iv-chart"), model);
  if (chart) state.charts.push(chart);
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
  el("calib-checklist").innerHTML =
    `<div class="banner ${
      model.counts.blocked ? "bad" : model.counts.done === model.items.length ? "ok" : "warn"
    }"><b>read-only status view</b> — ${esc(model.summary)}. Ticking nothing here is the
     normal state: this page can only report what the store proves, and the ratification's
     box is ticked by a human. No item here can be checked off from this screen.</div>` +
    `<table class="grid chk-table"><tbody>${rows}</tbody></table>` +
    `<div class="plan-meta">${
      pending.length
        ? `Rulebook.pending_calibrations (read straight from the YAML): ${pending
            .map((entry) => `<code>${esc(entry)}</code>`)
            .join(", ")}`
        : "Rulebook.pending_calibrations is empty"
    }</div>` +
    `<div class="plan-meta">strategy_version ${esc(
      payload.strategy_version || DASH,
    )} · read-only status, live values — the checklist itself is the operator's document
      (docs/ratification.md §(d)) and this page never edits it</div>`;
}

// ---------------------------------------------------------------------------
// load cycle
// ---------------------------------------------------------------------------

function render(payload) {
  teardownCharts();
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