// paper-hunter terminal — Phase U2 frontend.
//
// No build chain: this file is a plain ES module loaded straight from disk by the
// browser, and the one third-party library (TradingView Lightweight Charts 5.2.1) is
// vendored into ui/static/vendor/ with no CDN reference anywhere — the LAN has to work
// with the internet down. See ui/static/vendor/PROVENANCE.md.
//
// What this file is responsible for: hash routing across the five spec'd pages, the
// Terminal page (candles + overlays + subpanes + markers), a 45s poll, and telling the
// truth about the feed. What it deliberately does NOT do: place orders, invent data,
// or hide an empty state behind a chart that looks alive.
//
// # INTERPRETATION — subpane time-scale sync. The spec asks for "synced subpanes via
// subscribeVisibleLogicalRangeChange". In 5.2.1 that turns out to be unnecessary for
// native panes and impossible to do per-pane: `chart.timeScale()` takes no pane index
// and is the only time-scale accessor in the bundle, i.e. one time-scale model backs
// every pane, so the panes (and the crosshair) are synced by construction. Rather than
// fake a sync loop, `state.timeScales` is the list a multi-chart layout WOULD mirror
// through, and `syncVisibleRange()` runs the mirror on every visible-range change —
// currently a one-element list, so a no-op that costs nothing and is the single place to
// change if a future version reintroduces per-pane scales. The subscription itself is
// load-bearing twice over: it drives the visible-range HUD, and it detects the user
// panning left of the loaded window so the page can re-request a wider window (the
// spec's "zooming backwards is a re-request with a wider window").

const TF_LIST = ["5m", "10m", "15m", "30m", "1h", "4h", "1d", "1w", "1M", "1Q"];
const INTRADAY_TF = new Set(["5m", "10m", "15m", "30m", "1h", "4h"]);
const DEFAULT_TF = "15m";
const SYMBOL = "SPY";
const POLL_MS = 45_000;
// T4: a bar is "loud" at >= 1.5x its own 20-bar average volume. Computed client-side
// from /api/bars because the cache has no relvol column and inventing one server-side
// would be a second definition of a number the hunt panel already owns.
const RELVOL_MULT = 1.5;
const RELVOL_WINDOW = 20;
// Panning left of loaded data re-requests a wider window. Bounded: a research tool
// that quietly pulls a year of 1m bars because someone double-clicked is a worse tool.
const MAX_WIDENS = 5;
const MIN_WIDEN_GAP_MS = 20_000;

// Spec's arm color map. A gray because it trades once and that is the point; amber B,
// blue C, and the exception path magenta so catalyst-clause P&L can never be mistaken
// for ordinary P&L at a glance.
const ARM_COLOR = {
  A: "#9aa4b2",
  B: "#e0a63a",
  C: "#4c9aff",
  EXCEPTION: "#e05fd0",
};
const KIND_SHAPE = {
  TRADE: "arrowUp",
  STOP: "arrowDown",
  ROLL: "circle",
  PROPOSAL: "square",
  VETO: "x",
};
const OVERLAY_COLOR = {
  ema50: "#f0b429",
  ema200: "#8f7bff",
  bb_middle: "#3fb9c8",
  bb_upper: "#3fb9c8",
  bb_lower: "#3fb9c8",
  rsi: "#e0a63a",
  macd: "#4c9aff",
  macd_signal: "#e0a63a",
  macd_histogram: "#7f8c9b",
};

const PAGES = {
  terminal: null,
  arms: { phase: "U3", body: "Per-arm bankroll, deployed cash, open positions, equity curve and the shadow-roll sim. The endpoints are live; the page is not built." },
  ledger: { phase: "U3", body: "Every decision row with search, sort and date filters, references rendered as correction-of links. The endpoints are live; the page is not built." },
  hunt: { phase: "U3", body: "The hunt plan, the NO-SHOT ledger with counterfactuals, and the veto histogram. The endpoints are live; the page is not built." },
  calibration: { phase: "U4", body: "Flow baselines, threshold-distance histogram, IV-rank history, pending calibrations. The endpoints are live; the page is not built." },
};

const el = (id) => document.getElementById(id);
const state = {
  page: "terminal",
  tf: DEFAULT_TF,
  from: null, // YYYY-MM-DD or null (server picks the default window)
  to: null,
  on: new Set(["ema50", "ema200", "bb", "rsi", "macd", "volume"]),
  bars: [],
  startFloor: null, // earliest window.from this session has displayed (YYYY-MM-DD)
  rows: null,
  signals: [],
  widens: 0,
  lastWiden: 0,
  userMoved: false, // set by real wheel/drag input; the page never widens on its own
  polling: true,
  chart: null,
  timeScales: [],
  series: {},
  markers: null,
  byKey: new Map(),
  inFlight: false,
};

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------

const px = (n) => (n == null || Number.isNaN(n) ? "—" : n.toFixed(2));

function vol(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}

function clock(d = new Date()) {
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function etParts(iso) {
  // Bars are UTC ISO with a trailing Z; the session day is an ET wall-clock fact, and
  // daily-and-up timeframes are rendered as business days, so the ET date is what the
  // chart axis needs. en-CA formats as YYYY-MM-DD.
  const s = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(iso));
  const [y, m, d] = s.split("-").map(Number);
  return { year: y, month: m, day: d };
}

/** Chart time for a bar/marker: a UTCTimestamp intraday, a BusinessDay daily-and-up. */
function chartTime(iso, tf) {
  if (INTRADAY_TF.has(tf)) return Math.floor(Date.parse(iso) / 1000);
  return etParts(iso);
}

function timeKey(t) {
  return typeof t === "object" ? `${t.year}-${t.month}-${t.day}` : String(t);
}

const etDate = (iso) =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date(iso));

// ---------------------------------------------------------------------------
// data access
// ---------------------------------------------------------------------------

async function getJSON(path) {
  const response = await fetch(path, { headers: { accept: "application/json" } });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = JSON.stringify(body.detail);
    } catch (_) {
      /* a non-JSON error body is still an error worth showing */
    }
    throw new Error(`${path} -> ${detail}`);
  }
  return response.json();
}

function windowParams() {
  const params = new URLSearchParams({ symbol: SYMBOL, tf: state.tf });
  if (state.from) params.set("from", state.from);
  if (state.to) params.set("to", state.to);
  return params;
}

const serverIndicators = () =>
  [...state.on].filter((n) => n !== "volume");

// ---------------------------------------------------------------------------
// chart construction
// ---------------------------------------------------------------------------

function buildChart() {
  const chart = window.LightweightCharts.createChart(el("chart"), {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "#0b0d10" },
      textColor: "#8b95a4",
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 11,
      panes: { separatorColor: "#1e232b", separatorHoverColor: "#2c333d" },
    },
    grid: {
      vertLines: { color: "#14181e" },
      horzLines: { color: "#14181e" },
    },
    crosshair: {
      mode: window.LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: "#3a434f", width: 1, style: 3, labelBackgroundColor: "#1d2530" },
      horzLine: { color: "#3a434f", width: 1, style: 3, labelBackgroundColor: "#1d2530" },
    },
    rightPriceScale: { borderColor: "#1e232b", scaleMargins: { top: 0.12, bottom: 0.08 } },
    timeScale: {
      borderColor: "#1e232b",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 4,
      barSpacing: 7,
      fixLeftEdge: false,
      lockVisibleTimeRangeOnResize: true,
    },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
    handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: { time: true } },
    localization: { locale: "en-US" },
  });

  const S = window.LightweightCharts;
  const series = {};
  series.candle = chart.addSeries(S.CandlestickSeries, {
    upColor: "#26a69a",
    downColor: "#ef5350",
    borderUpColor: "#26a69a",
    borderDownColor: "#ef5350",
    wickUpColor: "#26a69a",
    wickDownColor: "#ef5350",
    priceLineColor: "#4c9aff",
    priceLineWidth: 1,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  const line = (color, options = {}) =>
    chart.addSeries(
      S.LineSeries,
      {
        color,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        ...options,
      },
      0,
    );
  series.ema50 = line(OVERLAY_COLOR.ema50);
  series.ema200 = line(OVERLAY_COLOR.ema200);
  series.bb_upper = line(OVERLAY_COLOR.bb_upper, { lineStyle: 2 });
  series.bb_middle = line(OVERLAY_COLOR.bb_middle, { lineStyle: 2 });
  series.bb_lower = line(OVERLAY_COLOR.bb_lower, { lineStyle: 2 });

  // Subpanes: native v5 panes, so they share the one time scale and one crosshair.
  // addPane(false) leaves preserveEmptyPane off, so a pane whose series are all
  // invisible collapses by itself: toggling is applyOptions({visible}) and nothing
  // else — no series is created or destroyed and the layout follows the toggles.
  const volPane = chart.addPane();
  series.volume = chart.addSeries(
    S.HistogramSeries,
    {
      priceFormat: { type: "volume" },
      priceLineVisible: false,
      lastValueVisible: false,
    },
    volPane.paneIndex(),
  );
  const rsiPane = chart.addPane();
  series.rsi = chart.addSeries(
    S.LineSeries,
    {
      color: OVERLAY_COLOR.rsi,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      title: "RSI 14",
    },
    rsiPane.paneIndex(),
  );
  const macdPane = chart.addPane();
  series.macd = chart.addSeries(
    S.LineSeries,
    { color: OVERLAY_COLOR.macd, lineWidth: 1, priceLineVisible: false, lastValueVisible: false },
    macdPane.paneIndex(),
  );
  series.macd_signal = chart.addSeries(
    S.LineSeries,
    { color: OVERLAY_COLOR.macd_signal, lineWidth: 1, priceLineVisible: false, lastValueVisible: false },
    macdPane.paneIndex(),
  );
  series.macd_histogram = chart.addSeries(
    S.HistogramSeries,
    { priceLineVisible: false, lastValueVisible: false },
    macdPane.paneIndex(),
  );

  volPane.setStretchFactor(1);
  rsiPane.setStretchFactor(1.1);
  macdPane.setStretchFactor(1.2);
  chart.panes()[0].setStretchFactor(3.4);

  for (const level of [70, 30]) {
    series.rsi.createPriceLine({
      price: level,
      color: level === 70 ? "#4a2f2f" : "#2f4a35",
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: false,
      title: String(level),
    });
  }

  series.markers = window.LightweightCharts.createSeriesMarkers(series.candle, []);
  series.macd_histogram.applyOptions({ color: OVERLAY_COLOR.macd_histogram });

  state.chart = chart;
  state.series = series;
  // One time scale in 5.2.1 (see the note at the top of this file). Kept as a list so
  // a multi-chart layout only has to push more entries here.
  state.timeScales = [chart.timeScale()];

  chart.subscribeCrosshairMove((param) => onCrosshair(param));
  chart.subscribeClick((param) => onClick(param));
  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => onVisibleRange(range));
  for (const type of ["wheel", "pointerdown", "keydown"]) {
    el("chart").addEventListener(type, () => {
      state.userMoved = true;
    });
  }

  return chart;
}

/** Mirror a logical range onto every time scale that needs hand-syncing. */
function syncVisibleRange(range) {
  if (!range) return;
  for (const scale of state.timeScales) {
    if (scale === state.chart.timeScale()) continue;
    scale.setVisibleLogicalRange(range);
  }
}

function onVisibleRange(range) {
  syncVisibleRange(range);
  if (!range) return;
  hud(range);
  maybeWiden(range);
}

// ---------------------------------------------------------------------------
// widening the window when the operator pans past the loaded data
// ---------------------------------------------------------------------------

function maybeWiden(range) {
  if (state.inFlight || state.bars.length === 0) return;
  // Gated on operator input, not on the range alone: fitContent() legitimately lands
  // the left edge at (or just under) 0 on every load and every timeframe switch, and a
  // widen triggered by the page's own first paint is a page arguing with itself.
  if (!state.userMoved) return;
  if (range.from >= 0) return;
  if (state.widens >= MAX_WIDENS) return;
  if (Date.now() - state.lastWiden < MIN_WIDEN_GAP_MS) return;

  // Widen from the earliest start this session has actually displayed, never from
  // "now minus N": the server's default window is anchored to the ET date, which at
  // 21:00 ET is a day behind the UTC date, so `now - 5d` can be a day LATER than what
  // is already loaded. A widen that narrows the window is a bug wearing a feature's
  // clothes, and it silently deletes a session from the chart.
  const floor = state.startFloor ? new Date(state.startFloor) : new Date();
  floor.setUTCDate(floor.getUTCDate() - (INTRADAY_TF.has(state.tf) ? 5 : 200));
  const iso = floor.toISOString().slice(0, 10);
  if (state.startFloor && iso >= state.startFloor) return;

  state.from = iso;
  state.startFloor = iso;
  state.widens += 1;
  state.lastWiden = Date.now();
  // The viewport the operator was looking at, in wall-clock terms, so it survives the
  // re-request (logical indices shift when earlier bars arrive; times do not).
  const scale = state.chart.timeScale();
  const at = (idx) => {
    const clamped = Math.max(0, Math.min(state.bars.length - 1, Math.round(idx)));
    const bar = state.bars[clamped];
    return bar ? chartTime(bar.t, state.tf) : undefined;
  };
  const keep = { from: at(range.from), to: at(range.to) };
  console.info(
    `[terminal] panned past the loaded window; requesting from ${iso} ` +
      `(widen ${state.widens}/${MAX_WIDENS})`,
  );
  refresh({ keepRange: keep });
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

function relvolColors(bars) {
  // Mean of the PRIOR `RELVOL_WINDOW` buckets, current bar excluded — including the
  // current bar would damp every spike toward the threshold and hide exactly the bars
  // this is meant to surface. Fewer than 20 prior bars => no highlight, no guessing.
  return bars.map((bar, i) => {
    const up = bar.c >= bar.o;
    const base = up ? "rgba(38,166,154,0.32)" : "rgba(239,83,80,0.32)";
    if (i < RELVOL_WINDOW) return base;
    let sum = 0;
    for (let k = i - RELVOL_WINDOW; k < i; k += 1) sum += bars[k].v;
    const avg = sum / RELVOL_WINDOW;
    if (avg > 0 && bar.v >= RELVOL_MULT * avg) return "#e0a63a";
    return base;
  });
}

function applyBars(payload) {
  const bars = payload.bars || [];
  state.bars = bars;
  state.byKey = new Map();
  const candles = bars.map((bar) => {
    const t = chartTime(bar.t, state.tf);
    state.byKey.set(timeKey(t), bar);
    return { time: t, open: bar.o, high: bar.h, low: bar.l, close: bar.c };
  });
  const colors = relvolColors(bars);
  const windowFrom = payload.window && payload.window.from ? payload.window.from.slice(0, 10) : null;
  if (windowFrom) {
    // The earliest window this session has shown, so a widen can only ever go left.
    state.startFloor = state.startFloor && state.startFloor < windowFrom ? state.startFloor : windowFrom;
  }
  state.series.candle.setData(candles);
  state.series.volume.setData(
    bars.map((bar, i) => ({ time: chartTime(bar.t, state.tf), value: bar.v, color: colors[i] })),
  );
  stampProvenance(payload);
  el("overlay").classList.toggle("show", bars.length === 0);
  if (bars.length === 0) {
    el("overlay-title").textContent = "no bars in the window";
    el("overlay-detail").textContent =
      `The bar cache holds nothing for ${SYMBOL} at ${state.tf}. That is a fact, ` +
      "not an error: the cache is built by the fetcher, and nothing has asked it for " +
      "this window yet.";
  }
  renderReadout(bars.length ? bars.length - 1 : -1);
}

function applyIndicators(payload) {
  const rows = (payload && payload.rows) || [];
  state.rows = rows;
  const line = (name) =>
    rows
      .filter((row) => row[name] != null)
      .map((row) => ({ time: chartTime(row.t, state.tf), value: row[name] }));
  for (const name of ["ema50", "ema200", "bb_upper", "bb_middle", "bb_lower", "rsi", "macd", "macd_signal"]) {
    const values = line(name);
    if (values.length || name in state.series) state.series[name].setData(values);
  }
  const hist = rows
    .filter((row) => row.macd_histogram != null)
    .map((row) => {
      const value = row.macd_histogram;
      return {
        time: chartTime(row.t, state.tf),
        value,
        color: value >= 0 ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)",
      };
    });
  state.series.macd_histogram.setData(hist);
  applyVisibility();
  // The indicator rows just changed under the readout: without this the OHLCV line
  // keeps rendering values from the previous payload (or dashes, when the bar count
  // grew) until the operator happens to move the crosshair.
  renderReadout(state.bars.length - 1);
}

function applySignals(payload) {
  const rows = (payload && payload.rows) || [];
  state.signals = rows;
  const markers = rows.map((row) => {
    const arm = ARM_COLOR[row.arm] || "#8b95a4";
    return {
      time: chartTime(row.t, state.tf),
      position: row.kind === "STOP" ? "belowBar" : "aboveBar",
      color: arm,
      shape: KIND_SHAPE[row.kind] || "circle",
      id: `j${row.journal_id}`,
      text: `${row.arm} ${row.kind}`,
    };
  });
  state.series.markers.setMarkers(markers);
  // Empty pre-window is the honest state, so say so instead of implying a bug.
  el("tab-stamp").textContent = rows.length
    ? `read-only · ${rows.length} signal${rows.length === 1 ? "" : "s"}`
    : "read-only · 0 signals in window";
}

function applyVisibility() {
  const on = (name) => state.on.has(name);
  state.series.volume.applyOptions({ visible: on("volume") });
  state.series.ema50.applyOptions({ visible: on("ema50") });
  state.series.ema200.applyOptions({ visible: on("ema200") });
  for (const name of ["bb_upper", "bb_middle", "bb_lower"]) {
    state.series[name].applyOptions({ visible: on("bb") });
  }
  state.series.rsi.applyOptions({ visible: on("rsi") });
  state.series.macd.applyOptions({ visible: on("macd") });
  state.series.macd_signal.applyOptions({ visible: on("macd") });
  state.series.macd_histogram.applyOptions({ visible: on("macd") });
}

// ---------------------------------------------------------------------------
// chrome: provenance, health, readout
// ---------------------------------------------------------------------------

function stampProvenance(payload) {
  const prov = (payload && payload.feed_provenance) || {};
  const node = el("prov");
  const byBar = Object.entries(prov.by_bar || {})
    .map(([feed, n]) => `${feed}: ${n}`)
    .join(", ");
  const text = `provenance: ${prov.label || "unknown"} · ${payload.count ?? 0} bars`;
  node.textContent = text;
  node.classList.remove("sip", "partial", "unknown");
  const worst = String(prov.worst || "").toLowerCase();
  node.classList.add(worst === "sip" || worst === "consolidated" ? "sip" : "partial");
  node.title =
    `worst feed in window: ${prov.worst ?? "n/a"} (${byBar || "no bars"}). ` +
    "On the free tier the newest 15 minutes are IEX-only; the cache never asks for a " +
    "SIP window younger than that, so a partial stamp is correct, not a defect.";
}

async function loadHealth() {
  const dot = el("health-dot");
  const text = el("health-text");
  try {
    const health = await getJSON("/api/health");
    const status = health.status || "unknown";
    dot.className = `dot ${status === "ok" ? "ok" : status === "degraded" ? "degraded" : "bad"}`;
    const barcache = (health.dbs && health.dbs.barcache) || {};
    const cached = (barcache.series || []).reduce((sum, s) => sum + (s.bars || 0), 0);
    text.textContent = `${status} · ro · ${cached} bars cached`;
    dot.parentElement.title =
      `read_only=${health.read_only} · server_time=${health.server_time} · ` +
      `cache: ${barcache.path || "n/a"} (${barcache.mode || "rw"})`;
  } catch (error) {
    dot.className = "dot bad";
    text.textContent = `health error: ${error.message}`;
  }
}

function renderReadout(index) {
  const node = el("readout");
  const bar = index >= 0 ? state.bars[index] : null;
  if (!bar) {
    node.innerHTML = "<span>no bars in window</span>";
    return;
  }
  const row = state.rows && state.rows[index] ? state.rows[index] : {};
  const prev = index > 0 ? state.bars[index - 1] : null;
  const change = prev ? ((bar.c - prev.c) / prev.c) * 100 : null;
  const relvol = relvolAt(index);
  const cls = change == null ? "" : change >= 0 ? "up" : "down";
  const parts = [
    `<span>O <b>${px(bar.o)}</b></span>`,
    `<span>H <b>${px(bar.h)}</b></span>`,
    `<span>L <b>${px(bar.l)}</b></span>`,
    `<span>C <b>${px(bar.c)}</b></span>`,
    `<span class="${cls}">${change == null ? "" : (change >= 0 ? "+" : "") + change.toFixed(2) + "%"}</span>`,
    `<span>V <b>${vol(bar.v)}</b></span>`,
    `<span>relvol <b>${relvol == null ? "—" : relvol.toFixed(2) + (relvol >= RELVOL_MULT ? " 🔊" : "")}</b></span>`,
    `<span>feed <b>${bar.feed}</b></span>`,
  ];
  if (state.on.has("ema50")) parts.push(`<span>ema50 <b>${px(row.ema50)}</b></span>`);
  if (state.on.has("ema200")) parts.push(`<span>ema200 <b>${px(row.ema200)}</b></span>`);
  if (state.on.has("bb")) {
    parts.push(`<span>bb <b>${px(row.bb_lower)}</b>·<b>${px(row.bb_middle)}</b>·<b>${px(row.bb_upper)}</b></span>`);
  }
  if (state.on.has("rsi")) parts.push(`<span>rsi <b>${px(row.rsi)}</b></span>`);
  if (state.on.has("macd")) {
    parts.push(
      `<span>macd <b>${px(row.macd)}</b> sig <b>${px(row.macd_signal)}</b> hist <b>${px(row.macd_histogram)}</b></span>`,
    );
  }
  node.innerHTML = parts.join("");
}

function relvolAt(index) {
  if (index < RELVOL_WINDOW) return null;
  let sum = 0;
  for (let k = index - RELVOL_WINDOW; k < index; k += 1) sum += state.bars[k].v;
  const avg = sum / RELVOL_WINDOW;
  return avg > 0 ? state.bars[index].v / avg : null;
}

function indexForTime(time) {
  if (time == null) return -1;
  const key = timeKey(time);
  // Indicators and markers are aligned to bars by index, so the readout needs the bar
  // index, not just the bar. Bars are sorted and dense, but not assumed dense.
  for (let i = 0; i < state.bars.length; i += 1) {
    if (timeKey(chartTime(state.bars[i].t, state.tf)) === key) return i;
  }
  return -1;
}

function onCrosshair(param) {
  if (!param || param.time === undefined) {
    renderReadout(state.bars.length - 1);
    return;
  }
  renderReadout(indexForTime(param.time));
}

function onClick(param) {
  // U3 replaces this with the signal popup (and the NO-SHOT counterfactual card).
  const index = param && param.time !== undefined ? indexForTime(param.time) : -1;
  if (index < 0) return;
  const marker = state.signals.find((row) => timeKey(chartTime(row.t, state.tf)) === timeKey(chartTime(state.bars[index].t, state.tf)));
  if (marker) {
    console.log("[U3] signal marker click", {
      arm: marker.arm,
      kind: marker.kind,
      journal_id: marker.journal_id,
      price: marker.price,
      price_source: marker.price_source,
      reasoning: marker.reasoning,
    });
  } else {
    console.log("[U3] bar click", { t: state.bars[index].t, ...state.bars[index] });
  }
}

function hud(range) {
  const scale = state.chart.timeScale();
  const from = scale.logicalToCoordinate(range.from);
  const to = scale.logicalToCoordinate(range.to);
  const stamp = el("updated");
  stamp.title = `viewport: ${Math.round(range.to - range.from + 1)} bars, x ${Math.round(from)}..${Math.round(to)}px`;
}

// ---------------------------------------------------------------------------
// load cycle
// ---------------------------------------------------------------------------

async function refresh({ fit = false, keepRange = null } = {}) {
  if (state.page !== "terminal" || state.inFlight) return;
  state.inFlight = true;
  const scale = state.chart.timeScale();
  const before = keepRange ? null : scale.getVisibleLogicalRange();
  try {
    const barsParams = windowParams();
    const jobs = [
      getJSON(`/api/bars?${barsParams}`),
      getJSON(`/api/signals?${symbolAndWindow()}&limit=2000`),
      loadHealth(),
    ];
    const names = serverIndicators();
    if (names.length) jobs.push(getJSON(`/api/indicators?${barsParams}&set=${names.join(",")}`));
    const [bars, signals, , indicators] = await Promise.all(jobs);
    applyBars(bars);
    if (indicators) applyIndicators(indicators);
    applySignals(signals);
    applyVisibility();

    if (keepRange) {
      if (keepRange.from != null && keepRange.to != null) scale.setVisibleRange(keepRange);
    } else if (fit || !before) {
      scale.fitContent();
    } else {
      scale.setVisibleLogicalRange(before);
    }
    el("updated").textContent = `updated ${clock()}`;
    el("updated").classList.toggle("paused", !state.polling);
  } catch (error) {
    el("overlay").classList.add("show");
    el("overlay-title").textContent = "load failed";
    el("overlay-detail").textContent = String(error.message || error);
    el("updated").textContent = `error ${clock()}`;
    el("updated").classList.add("paused");
    console.error(error);
  } finally {
    state.inFlight = false;
  }
}

function symbolAndWindow() {
  const params = new URLSearchParams({ symbol: SYMBOL });
  if (state.from) params.set("from", state.from);
  if (state.to) params.set("to", state.to);
  return params.toString();
}

// ---------------------------------------------------------------------------
// controls, routing, polling
// ---------------------------------------------------------------------------

function selectTimeframe(tf) {
  if (tf === state.tf) return;
  state.tf = tf;
  for (const button of document.querySelectorAll("button.tf")) {
    button.setAttribute("aria-pressed", String(button.dataset.tf === tf));
  }
  // A wider window is never right for a new timeframe: the server's default per-timeframe
  // window is the one that fits the chart, and an inherited explicit window would show
  // 15m's five days of 5m bars as a smear.
  state.from = null;
  state.startFloor = null;
  state.widens = 0;
  state.userMoved = false;
  refresh({ fit: true });
}

function toggleIndicator(name) {
  if (state.on.has(name)) state.on.delete(name);
  else state.on.add(name);
  for (const button of document.querySelectorAll("button.tog")) {
    if (button.dataset.ind === name) button.setAttribute("aria-pressed", String(state.on.has(name)));
  }
  applyVisibility();
  const names = serverIndicators();
  if (!names.length) {
    if (state.rows) {
      for (const key of Object.keys(state.rows[0] || {})) {
        if (key !== "t" && key !== "c" && state.series[key]) state.series[key].setData([]);
      }
    }
    state.rows = null;
    renderReadout(state.bars.length - 1);
    return;
  }
  refresh();
}

function route() {
  const hash = (location.hash || "#/terminal").replace(/^#\/?/, "");
  const page = PAGES[hash] !== undefined ? hash : "terminal";
  state.page = page;
  for (const link of document.querySelectorAll("#tabbar a")) {
    link.classList.toggle("active", link.dataset.page === page);
  }
  const terminal = el("page-terminal");
  const placeholder = el("page-placeholder");
  if (page === "terminal") {
    terminal.hidden = false;
    placeholder.hidden = true;
    if (!state.chart) buildChart();
    else state.chart.applyOptions({ autoSize: true });
    refresh({ fit: !state.bars.length });
  } else {
    terminal.hidden = true;
    placeholder.hidden = false;
    el("ph-title").textContent = page;
    el("ph-phase").textContent = PAGES[page].phase;
    el("ph-body").textContent = PAGES[page].body;
  }
}

function startPolling() {
  setInterval(() => {
    if (state.polling && !document.hidden) refresh();
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    state.polling = !document.hidden;
    const stamp = el("updated");
    if (state.polling) {
      stamp.classList.remove("paused");
      refresh();
    } else {
      stamp.classList.add("paused");
      stamp.textContent = "paused (tab hidden)";
    }
  });
}

function wire() {
  for (const button of document.querySelectorAll("button.tf")) {
    button.addEventListener("click", () => selectTimeframe(button.dataset.tf));
  }
  for (const button of document.querySelectorAll("button.tog")) {
    button.addEventListener("click", () => toggleIndicator(button.dataset.ind));
  }
  window.addEventListener("hashchange", route);
}

buildChart();
wire();
route();
startPolling();
