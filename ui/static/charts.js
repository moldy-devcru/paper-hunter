// Small chart builders for the U3 pages, on the same vendored lightweight-charts the
// terminal uses (no CDN, no build chain — see vendor/PROVENANCE.md).
//
// # INTERPRETATION: equity curves are LineSeries on a bare chart, not a second pane of
// the terminal chart. Three mini charts, one per arm, each its own <div>: they sit in
// three cards side by side and are read independently, so coupling them through the
// terminal's single time scale would buy crosshair sync nobody asked for at the cost of
// a chart per card that cannot be resized independently. The veto histogram is NOT a
// chart at all — see model.vetoHistogram for why plain stacked divs beat a canvas here.

const CHART_BASE = {
  layout: {
    background: { type: "solid", color: "#0b0d10" },
    textColor: "#8b95a4",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 10,
  },
  grid: { vertLines: { visible: false }, horzLines: { color: "#14181e" } },
  rightPriceScale: { borderColor: "#1e232b", scaleMargins: { top: 0.15, bottom: 0.15 } },
  timeScale: { borderColor: "#1e232b", visible: false, fixLeftEdge: true, fixRightEdge: true },
  crosshair: { mode: 0, vertLine: { visible: false }, horzLine: { color: "#2c333d" } },
  handleScroll: false,
  handleScale: false,
  localization: { locale: "en-US" },
};

/**
 * An equity line. `points` is `[{t: ISO, equity: number}]` straight from
 * `/api/arms`. Returns null when there is nothing to draw, so the caller can render its
 * own honest empty state instead of an empty rectangle.
 */
export function equityChart(container, points, { color = "#4c9aff", dashed = false } = {}) {
  const S = window.LightweightCharts;
  if (!S || !container || !points || points.length === 0) return null;
  const chart = S.createChart(container, { ...CHART_BASE, autoSize: true, height: 96 });
  const series = chart.addSeries(S.LineSeries, {
    color,
    lineWidth: 1,
    lineStyle: dashed ? 2 : 0,
    priceLineVisible: false,
    lastValueVisible: true,
    crosshairMarkerVisible: true,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  const data = points
    .filter((point) => point && point.equity != null && point.t)
    .map((point) => ({ time: Math.floor(Date.parse(point.t) / 1000), value: point.equity }));
  if (data.length === 0) {
    chart.remove();
    return null;
  }
  series.setData(data);
  chart.timeScale().fitContent();
  return chart;
}

/**
 * Arm C's card: the live equity line with the shadow-roll total drawn over it as a
 * dashed comparison. The shadow curve is a single point per leg close (the sim has no
 * time series of its own), so it is drawn as a dashed line through those points and
 * labelled as a comparison — it is never summed into the arm's numbers, which is the
 * whole reason it is a different colour and a different line style.
 */
export function shadowOverlayChart(container, curve, shadow) {
  const S = window.LightweightCharts;
  if (!S || !container || !curve || curve.length === 0 || !shadow) return null;
  const chart = S.createChart(container, { ...CHART_BASE, autoSize: true, height: 96 });
  const live = chart.addSeries(S.LineSeries, {
    color: "#4c9aff",
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: true,
    crosshairMarkerVisible: true,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  live.setData(
    curve
      .filter((point) => point.equity != null && point.t)
      .map((point) => ({ time: Math.floor(Date.parse(point.t) / 1000), value: point.equity })),
  );
  const legs = (shadow.legs || [])
    .filter((leg) => leg.expiry)
    .map((leg) => ({ time: Math.floor(Date.parse(`${leg.expiry}T20:00:00Z`) / 1000), value: leg.pnl }));
  if (legs.length > 0) {
    const sim = chart.addSeries(S.LineSeries, {
      color: "#e05fd0",
      lineWidth: 1,
      lineStyle: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: true,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    sim.setData(legs);
  }
  chart.timeScale().fitContent();
  return chart;
}

// ---------------------------------------------------------------------------
// U4: the calibration charts
// ---------------------------------------------------------------------------

/** Provenance palette. Proxy and real must never be the same colour. */
export const PROVENANCE_COLOR = { real: "#4c9aff", proxy: "#d9a441" };
/** Call/put volume sides, and the two scalar rules drawn over them. */
export const CALL_COLOR = "#26a69a";
export const PUT_COLOR = "#ef5350";
export const MEAN_COLOR = "#8f7bff";
export const P90_COLOR = "#e05fd0";

/** UTC midnight of a YYYY-MM-DD session date, as a lightweight-charts UTCTimestamp. */
export function dayTime(date) {
  const parsed = Date.parse(`${String(date).slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(parsed) ? date : Math.floor(parsed / 1000);
}

/**
 * Per-session deep-OTM volume: calls and puts side by side, with the accumulating
 * baseline mean as a line and the forming P90 as a horizontal rule.
 *
 * # INTERPRETATION — the spec allows bars "up and down or side by side". Side-by-side is
 * the choice, via two histogram series on one value scale: the operator's question is
 * which side printed, and mirrored bars answer "calls were bigger" instead of "the green
 * one was bigger", which is a question about colour.
 *
 * The mean is a LINE because it has one value per session (it is a trailing window mean,
 * so it moves as the window fills), and the P90 is a horizontal RULE because it is a
 * scalar fact about the whole window — drawing it as a series would imply it was known
 * per session, which is only true for the points already collected and is exactly what
 * `percentileSeries` exists to avoid overstating. Both are drawn only when the server
 * gave a value: no line is better than a line at zero.
 */
export function flowVolumeChart(container, model) {
  const S = window.LightweightCharts;
  if (!S || !container || !model || model.empty) return null;
  const chart = S.createChart(container, { ...CHART_BASE, autoSize: true, height: 220 });
  const side = (color, key) => {
    const series = chart.addSeries(S.HistogramSeries, {
      color,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    series.setData(
      model.points
        .filter((point) => point[key] != null)
        .map((point) => ({ time: dayTime(point.date), value: point[key] })),
    );
    return series;
  };
  const calls = side(CALL_COLOR, "call");
  side(PUT_COLOR, "put");
  if (model.baselineMeanLatest != null) {
    chart
      .addSeries(S.LineSeries, {
        color: MEAN_COLOR,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
        title: "trailing mean",
      })
      .setData(
        model.points
          .filter((point) => point.baselineMean != null)
          .map((point) => ({ time: dayTime(point.date), value: point.baselineMean })),
      );
  }
  if (model.p90Value != null) {
    calls.createPriceLine({
      price: model.p90Value,
      color: P90_COLOR,
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: `P90 ${model.p90Status}`,
    });
  }
  chart.timeScale().fitContent();
  return chart;
}

/**
 * IV observations over time: one line per `underlying|tenor_key`, drawn segment by
 * segment so the VIX-proxy seed can never read as a continuation of the real chain.
 *
 * Proxy segments are dashed AND a different colour. Colour alone fails for a reader who
 * cannot separate the hues, and "the seed is not the chain" is the single fact this
 * panel exists to make impossible to miss — so it survives both a monochrome print and a
 * glance. A lone point in a segment is not plotted (a single value is a measurement, not
 * a trend, and it reads as a real observation on the line), but it is counted in the
 * panel's provenance summary next to the chart.
 */
export function ivHistoryChart(container, model) {
  const S = window.LightweightCharts;
  if (!S || !container || !model || model.empty) return null;
  const chart = S.createChart(container, { ...CHART_BASE, autoSize: true, height: 200 });
  for (const entry of model.series) {
    const plottable = entry.segments.filter(
      (segment) => segment.points.length > 1 || entry.segments.length === 1,
    );
    for (const segment of plottable) {
      chart
        .addSeries(S.LineSeries, {
          color: PROVENANCE_COLOR[segment.provenance] || PROVENANCE_COLOR.real,
          lineWidth: 1,
          lineStyle: segment.provenance === "proxy" ? 2 : 0,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: true,
          title: `${entry.underlying} ${entry.tenorKey} (${segment.provenance})`,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
        })
        .setData(segment.points.map((point) => ({ time: dayTime(point.date), value: point.iv })));
    }
  }
  chart.timeScale().fitContent();
  return chart;
}
