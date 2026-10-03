// Formatting helpers. Pure: no DOM, no fetch, no state. This module is imported by
// the Node test harness (tests/test_static_logic.py) so the number formatting the
// terminal shows is the same code path the tests score.
//
// Convention inherited from U2: money and prices are monospace, two decimals, and a
// missing value is an em dash — never a zero. A zero is a measurement; a dash is the
// absence of one, and the run's whole claim is that those are different.

export const DASH = "—";

export const isNum = (n) => typeof n === "number" && Number.isFinite(n);

/** Price, two decimals. */
export const px = (n) => (isNum(n) ? n.toFixed(2) : DASH);

/** Signed dollars, e.g. "+$200.00" / "-$5.00". */
export function usd(n, { sign = true, cents = true } = {}) {
  if (!isNum(n)) return DASH;
  const body = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: cents ? 2 : 0,
    maximumFractionDigits: cents ? 2 : 0,
  });
  if (n === 0) return `$0${cents ? ".00" : ""}`;
  return `${n < 0 ? "-" : sign ? "+" : ""}$${body}`;
}

/** Unsigned dollars. */
export const dollars = (n, cents = true) => (isNum(n) ? usd(n, { sign: false, cents }) : DASH);

/** Fraction as a signed percentage: 0.02 -> "+2.00%". */
export function pct(n, { sign = true, digits = 2 } = {}) {
  if (!isNum(n)) return DASH;
  const value = n * 100;
  if (value === 0) return `${(0).toFixed(digits)}%`;
  return `${value < 0 ? "-" : sign ? "+" : ""}${Math.abs(value).toFixed(digits)}%`;
}

/** Compact volume: 1_234_567 -> "1.2M". */
export function vol(n) {
  if (!isNum(n)) return DASH;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}

/** Wall-clock stamp from an ISO string, ET, 24h. */
export const clockET = (iso) => {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return DASH;
  return at.toLocaleTimeString("en-GB", {
    hour12: false,
    timeZone: "America/New_York",
  });
};

/** YYYY-MM-DD in ET for an ISO string (business-day axis + date filters). */
export const etDate = (iso) => {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return DASH;
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(at);
};

/** "2026-03-05" -> "Mar 5". Only for dense table cells; ISO stays in the title. */
export const shortDay = (iso) => {
  if (typeof iso !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return DASH;
  const [, m, d] = iso.split("-");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[Number(m) - 1] || m} ${Number(d)}`;
};

/** HTML-escape. Every string that reaches innerHTML goes through this. */
export function esc(value) {
  if (value == null) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Truncate on a word boundary when possible, with a real ellipsis. */
export function truncate(text, limit = 240) {
  if (text == null) return "";
  const value = String(text);
  if (value.length <= limit) return value;
  const cut = value.slice(0, limit);
  const space = cut.lastIndexOf(" ");
  return `${(space > limit * 0.5 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

/** CSS class for a signed number, using the terminal's up/down palette. */
export const toneClass = (n) => (!isNum(n) ? "" : n > 0 ? "up" : n < 0 ? "down" : "flat");
