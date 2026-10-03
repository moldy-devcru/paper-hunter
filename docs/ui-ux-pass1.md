# UI UX pass 1 — information hierarchy

**Status:** done. **Scope:** the four non-chart UI tabs (`#hunt`, `#arms`,
`#calibration`, `#ledger`) of the read-only LAN terminal.

## The complaint this pass answers

Operator feedback on the terminal, verbatim:

> very dense, compact text, hard to immediately discern

Every tab rendered the same way: a wall of 11px table rows with the numbers that
mattered buried in the middle of it, and the *least* important content (methodology
caveats, "all clear" banners, empty-state essays) taking the most visual space. Reading
a tab meant reading all of it carefully to find three numbers.

Two facts about this machine shaped the whole pass:

1. **It is one day old and the journal is empty.** On the production host
   `data/journal.db` does not exist yet. "No data" is the *current truth*, not an error
   state — so empty states had to stop being styled like failures.
2. **Everything on the page is equally quiet, which makes nothing readable.** A
   footnote and a bankroll were rendered at the same size and weight, so the reader had
   no signal about where to start.

## Rules adopted (these are the pass-2 contract)

| Rule | Reason |
| --- | --- |
| Numbers you came for are BIG (22px); everything else stays 11px | A large value reads as a different *kind* of object, not a bigger version of the same one. This is the single change that most improves "immediately discern". |
| The summary row is first on every tab | You should never scroll to learn the state of the thing. |
| Empty state = ONE line + a `?` disclosure | The reasoning is still written down and still reachable; it just stops shouting at a state that is currently *normal*. |
| Only findings get banners | "Nothing is wrong" is not a finding. See "All-clear demotion" below. |
| Methodology and integrity notes are collapsed by default | A disclosure is a closed box. A banner is a shout. |
| At zero rows, table header / pager / counters are hidden | They describe rows that do not exist. |

## Per-tab changes

### Hunt (`ui/static/hunt.js`)

- **4 summary cards:** run status · plan cells · sightings · vetoes. Run status carries
  the session date as its sub-line, so the card answers "which day is this" too.
- **Empty states** for plan cells, sightings and the veto histogram are one line each,
  with the full explanation behind a `?`.
- **"No event veto" demoted.** The green all-clear banner is now a quiet inline chip.
  Banners are reserved for *active* vetoes and for the `UNKNOWN` state (rulebook
  unreadable → no verdict is claimed), because both of those are real findings.
- Veto histogram and the "pending, not 'no edge'" discipline note are unchanged in
  content, demoted in volume.

### Arms (`ui/static/arms.js`)

- **5 summary cards:** bankroll · deployed · realized P&L · vs control · window state.
- **A/B/C comparison table** — shared metric rows down the left, arms across the top,
  each with a rulebook subtitle chip rendered from the rulebook rather than hardcoded
  in prose: A `control`, B `inert until T5 warms`, C `entry window 09:45–15:30 ET`.
  The subtitle is the rulebook claim rendered where it is needed to interpret the
  number beside it.
- **Integrity notes collapsed into ONE disclosure.** Previously three separate amber
  blocks (broker cross-check, insufficient history, integrity flags) each shouted. The
  disclosure carries a count badge that turns red when something is actually blocked, so
  a problem still surfaces without being expanded.
- **Position cards render only for arms that have positions.** The metric lines that
  used to be repeated on every arm card moved to the comparison table — printing the
  same number twice on one tab is how a reader ends up unsure which is current.
- **Equity/shadow-roll curves render only when curve data exists**, in their own
  section, instead of an empty bordered box per arm.
- The exception path's magenta "rendered separately on purpose" **banner became a quiet
  line**. It is a permanent structural note, not a finding; the wording is unchanged
  because the claim (catalyst-clause P&L is never summed into A/B/C) is load-bearing.

### Calibration (`ui/static/calibration.js`)

- **6 summary cards:** status (`calibration_pending` vs `sufficient`) · proposed N ·
  qualified sessions · baseline · latest session · IV warmup. The status card is derived
  from the same `n` block the panel below renders, so the headline and the detail cannot
  disagree.
- **Empty states** for flow, distance buckets and IV store are one-liners + `?`.
- **Table hygiene:** dash-only/empty cells are muted rather than printed as loud as a
  real zero; row height increased; header row and the session/date column are frozen so
  a scrolling baseline never loses which session a number belongs to.
- **Blank chart areas get a purposeful state** ("plotting…" → removed on draw, or
  "nothing to plot — sessions exist but carry no volume"). `charts.js` returns `null`
  rather than an empty canvas, so without this a 220px empty rectangle reads as a
  rendering failure — exactly the wrong impression on a one-day-old machine.
- **Checklist banner demoted.** The permanent "read-only status view" banner was the
  loudest thing on the tab and said the same thing every load. It is now a status chip
  with live counts plus an "About this checklist" disclosure. Genuine `BLOCKED` /
  contradictions in the table remain visible.

### Ledger (`ui/static/ledger.js`)

- **3 summary cards:** journal/matching rows · rows shown · last written.
- **Filters grouped** into `SEARCH` / `CLASSIFICATION` / `DATE` clusters, so the bar
  reads as three decisions instead of six unlabeled widgets.
- **Active filters render as removable chips.** Each chip names one filter and clears
  exactly that one; `reset` still clears the whole set. Previously the only way to know a
  filter was on was to read the empty-state sentence and count clauses in it.
- **Empty states distinguish two opposite facts:** "The journal is empty" (with the
  server's own note) vs "No rows match these filters" (naming the filters). The old
  single sentence told an operator with no filters on that their filters had excluded
  everything, which is a false statement about their own query.
- **At zero rows: table, header, pager and row counters are all suppressed.**
- **Append-only policy moved into a compact disclosure.** The old full-width banner
  repeated the same structural fact above every table; it is now a one-line disclosure
  with the full reasoning inside. The footer keeps a one-line reminder.

## Bug found and fixed in passing

`shortDay()` in `ui/static/format.js` split the whole ISO string on `-`, so the third
field was `"03T06:23:34Z"` and `Number()` of it was `NaN`. **Every rendered date in the
UI read `Oct NaN`** — including the new ledger "last written" card. Fixed by matching
`^(\d{4})-(\d{2})-(\d{2})` and reading only the captured groups.

## What did not change (deliberately)

- No new dependencies, no build step. Vanilla ES modules, as before.
- **GET-only.** No new routes, no `StaticFiles` mount. Every new element reads data the
  page already fetched.
- 45s poll, hidden-tab pause, and hash routing all unchanged and re-verified in-browser.
- No CDN/external asset references added.

## Verification

- `pytest` — 948 passed. `ruff check` clean. (`ruff format --check` reports the same 52
  pre-existing unformatted files before and after this change; it is not the repo's
  gate and was left alone.)
- Rendered all four tabs in headless Chromium against **both** a real empty journal
  (no `journal.db` at all) and the two-week seeded fixture journal. Zero console
  errors or unhandled exceptions in either state.
- Interaction-checked in-browser: per-filter chips appear, clearing one chip leaves the
  others intact, an impossible date filter produces the "no rows match" state with the
  table/pager suppressed, reset restores all 8 fixture rows, hash routing switches tabs.

## Baseline for pass 2 (typography / spacing)

Pass 1 fixed *hierarchy* — what is big, what is collapsed, what is one line. It
deliberately left the underlying type scale and spacing rhythm alone. Pass 2 should
address:

- The global 11px body size and whether 13px is a better floor for dense tables.
- Row height and padding rhythm (partially addressed: calibration tables only).
- Line length of `.mp-detail` prose inside disclosures vs the table text beside it.
- Whether the 22px summary value should scale up on wide windows.
- Remaining muted-dash treatment in arms position rows.