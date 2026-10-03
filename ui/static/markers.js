// Terminal marker popups (U3 item 1) — the anchored panel that replaces the U2
// console.log placeholder.
//
// Two responsibilities, kept apart on purpose:
//   * `openMarkerPopup` / `closeMarkerPopup` own the DOM node, its anchoring and its
//     dismissal (outside-click, Escape, a second click on the same marker).
//   * everything about WHAT to show comes from model.markerPopup, which is pure and
//     unit-tested. The popup renders a view model; it never derives one.
//
// # INTERPRETATION — anchoring. lightweight-charts hands us a click with a
// `{x, y}` coordinate in the chart's client space, but no marker hit-test, so "the bar
// under the cursor" is resolved by the caller (app.js) and the popup anchors on the
// pointer position, clamped into the chart wrapper. Anchoring to the bar's x would
// look tidier and is deliberately not done: at 10 timeframes a bar is 1-40px wide and
// a popup pinned to a 1px-wide bar jumps between columns on every refresh.

import { esc, usd, px, shortDay, clockET } from "./format.js";
import { markerPopup } from "./model.js";

let node = null;
let anchor = null;
let lastKey = null;

function ensureNode(doc = document) {
  if (node && node.isConnected) return node;
  node = doc.createElement("div");
  node.className = "marker-popup";
  node.id = "marker-popup";
  node.setAttribute("role", "dialog");
  node.setAttribute("aria-label", "journal entry");
  node.hidden = true;
  return node;
}

/**
 * Open (or re-open) the popup.
 *
 * `signal`  — the clicked /api/signals row, or null for a hollow NO-SHOT marker.
 * `sightings` — /api/noshots rows attached to it (a decision may have several).
 * `position` — {x, y} in client coordinates.
 */
export function openMarkerPopup(signal, sightings, position, { host, doc = document } = {}) {
  const parent = host || doc.body;
  const panel = ensureNode(doc);
  if (!panel.isConnected) parent.appendChild(panel);

  const model = markerPopup(signal, sightings);
  const key = `${model.title}|${model.subtitle}|${model.counterfactual.headline}`;
  panel.innerHTML = renderPopupHtml(model);
  panel.hidden = false;
  anchor = position || null;
  lastKey = key;
  place(panel, anchor, host);
  return model;
}

/** Close the popup. Safe to call when nothing is open. */
export function closeMarkerPopup() {
  if (node) node.hidden = true;
  anchor = null;
  lastKey = null;
}

/** True when a popup is currently on screen. */
export const isMarkerPopupOpen = () => Boolean(node && !node.hidden);

/** The key of what is on screen, so a re-render can skip an identical rebuild. */
export const markerPopupKey = () => lastKey;

function place(panel, position, host) {
  if (!position) {
    panel.style.left = "50%";
    panel.style.top = "12px";
    panel.style.transform = "translateX(-50%)";
    return;
  }
  const bounds = (host || document.body).getBoundingClientRect();
  const width = panel.offsetWidth || 320;
  const height = panel.offsetHeight || 220;
  // Clamp inside the host so a marker near the right edge or the last bar on screen
  // does not push the panel half off the page.
  const x = Math.max(8, Math.min(position.x - bounds.left + 12, bounds.width - width - 8));
  const y = Math.max(8, Math.min(position.y - bounds.top + 12, bounds.height - height - 8));
  panel.style.left = `${x}px`;
  panel.style.top = `${y}px`;
  panel.style.transform = "none";
}

/**
 * The popup's markup, as a string. Exported (and pure) so the escaping guarantee is
 * testable: every value that came out of the journal passes through `esc` here, and
 * tests/test_static_logic.py renders a popup whose reasoning text is an XSS attempt and
 * asserts the tag comes out as text.
 */
export function renderPopupHtml(model) {
  const fields = model.fields
    .map(
      (field) =>
        `<span class="mf"><span class="mk">${esc(field.k)}</span>` +
        `<span class="mv"${field.color ? ` style="color:${esc(field.color)}"` : ""}` +
        `${field.title ? ` title="${esc(field.title)}"` : ""}>${esc(field.v)}</span></span>`,
    )
    .join("");

  const failed = model.failed.length
    ? `<div class="mp-block"><div class="mp-h">failed conditions</div><ul class="mp-list">${model.failed
        .map(
          (item) =>
            `<li><span class="chip ${esc(item.status.toLowerCase())}">${esc(item.id)}</span>` +
            `<span class="mp-detail">${esc(item.detail)}</span></li>`,
        )
        .join("")}</ul></div>`
    : "";

  const counterfactual =
    model.counterfactual.state === "pending" || !model.counterfactual.headline
      ? ""
      : `<div class="mp-block"><div class="mp-h">counterfactual</div>` +
        `<div class="mp-cf ${esc(model.counterfactual.tone)}">${esc(model.counterfactual.headline)}</div>` +
        `<div class="mp-sub">${esc(model.counterfactual.detail)}</div></div>`;

  const reasoning = model.reasoning
    ? `<div class="mp-block"><div class="mp-h">reasoning</div>` +
      `<div class="mp-reasoning"${model.reasoningFull !== model.reasoning ? ` title="${esc(model.reasoningFull)}"` : ""}>` +
      `${esc(model.reasoning)}</div></div>`
    : "";

  const extra =
    model.attached > 1
      ? `<div class="mp-sub">${model.attached} NO-SHOT sightings reference this row</div>`
      : "";

  return (
    `<div class="mp-head"><span class="mp-title" style="color:${esc(
      model.fields.find((f) => f.color)?.color || "#d7dce4",
    )}">${esc(model.title)}</span>` +
    `<span class="mp-sub">${esc(model.subtitle)}</span></div>` +
    `<div class="mp-fields">${fields}</div>` +
    reasoning +
    failed +
    counterfactual +
    extra +
    `<div class="mp-foot">read-only · journal row ${esc(model.subtitle)} · esc or click away to close</div>`
  );
}

/**
 * Wire dismissal once, for the whole document.
 *
 * Outside-click and Escape, both on the document, both ignored while the pointer is
 * inside the panel itself (otherwise the click that opened it would immediately close
 * it). Registered once per call site with a named handler so repeated imports of the
 * router do not stack listeners.
 */
export function wireMarkerPopupDismissal({ doc = document } = {}) {
  if (wireMarkerPopupDismissal.wired) return;
  wireMarkerPopupDismissal.wired = true;
  doc.addEventListener("mousedown", (event) => {
    if (!isMarkerPopupOpen()) return;
    if (node && node.contains(event.target)) return;
    closeMarkerPopup();
  });
  doc.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMarkerPopup();
  });
}

/**
 * Build the hollow NO-SHOT marker rows the terminal adds on top of /api/signals.
 *
 * # INTERPRETATION: the marker is hollow because the spec says near-misses are hollow,
 * and because a filled marker is a claim that we acted. lightweight-charts series
 * markers have no hollow glyph, so the shape carries the meaning instead: a hollow
 * `circle` (shape "circle", colour = the arm's, but drawn with a distinct `text` tag of
 * "noshot") is not available, so the honest rendering is a `square` in the NO-SHOT
 * grey with the prefix "NO-SHOT" in the label, and the popup — not the glyph — is where
 * the near-miss is identified. Anything fancier would be a lie about precision.
 */
export function noshotMarker(row, { chartTime, tfIntraday }) {
  const arm = (row.instrument_hypothesis && row.instrument_hypothesis.arm) || "B";
  return {
    time: chartTime(row.ts, tfIntraday),
    position: "belowBar",
    color: "#6b7686",
    shape: "square",
    id: `n${row.id}`,
    text: "NO-SHOT",
    arm,
  };
}

/** Re-exported for the terminal page, which formats prices the same way. */
export { usd, px, shortDay, clockET };
