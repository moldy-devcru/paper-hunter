// Network access. One function, because the read-only guarantee is partly a property
// of this file: it only ever issues GETs and only ever to /api/, so a future page that
// wants to POST something finds there is no helper to reach for.
//
// # INTERPRETATION: the U2 comment in app.js says every fetch in the frontend is a
// GET. U3 splits the frontend into modules, and that assertion was a test that parsed
// ONE file — which would have become a test that quietly stopped covering the other
// seven. The guarantee now lives here, and tests/test_ui_static.py greps every .js in
// ui/static/ for write verbs and for a getJSON() path outside /api/.

/** GET a JSON endpoint under /api/. Throws with the server's own detail on failure. */
export async function getJSON(path) {
  if (typeof path !== "string" || !path.startsWith("/api/")) {
    throw new Error(`refusing to fetch a non-API path: ${path}`);
  }
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

/** Build a query string from a plain object, dropping empty values. */
export function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === "") continue;
    search.set(key, String(value));
  }
  return search.toString();
}

/**
 * Run several GETs, tolerating individual failures.
 *
 * A hunt page with a dead histogram still has a hunt plan; turning the whole page into
 * an error panel because one panel failed is how a research tool becomes untrustworthy.
 * Returns `{ok, data, errors}` so each panel can say what it could not load.
 */
export async function getAll(jobs) {
  const entries = Object.entries(jobs);
  const settled = await Promise.allSettled(entries.map(([, path]) => getJSON(path)));
  const data = {};
  const errors = {};
  settled.forEach((outcome, i) => {
    const [name] = entries[i];
    if (outcome.status === "fulfilled") data[name] = outcome.value;
    else errors[name] = String((outcome.reason && outcome.reason.message) || outcome.reason);
  });
  return { ok: Object.keys(errors).length === 0, data, errors };
}
