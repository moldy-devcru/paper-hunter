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
