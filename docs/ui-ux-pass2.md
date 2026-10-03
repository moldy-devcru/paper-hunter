# UI/UX pass 2 — typography, spacing, and five review fixes

Pass 1 (`docs/ui-ux-pass1.md`) fixed the information hierarchy: what is a banner, what
is a chip, what is a table. Pass 2 is the layer under it — the type scale, the spacing
rhythm, and the five specific things a read-through of pass 1 caught.

Four non-chart tabs were in scope: `#hunt`, `#arms`, `#calibration`, `#ledger`. No new
dependencies, no build chain, GET-only server unchanged in posture.

## The five review fixes

### 1. The baseline card no longer reads like a fraction

`Baseline 64/20` looked like a broken ratio: 64 of 20? over a threshold? The card now
says what it counts.

- label: `baseline sessions`
- value: the session count (24 in the seeded fixture)
- subtitle: `minimum 20 met` / `minimum 20 · needs 20 more`

The all-clear chip beside the flow panel was flattened to match: `baseline complete ·
24 sessions, minimum 20`.

### 2. No truncated subtitles, anywhere

`.stat-sub` was clamped to one line with an ellipsis, so the Proposed-N subtitle lost its
tail on a narrow tile — and that tail was exactly the clause that stops a reader treating
the multiplier as frozen. Subtitles now wrap, and the text was shortened to
`× the baseline mean, not frozen` at the same time.
`test_pass2_no_summary_subtitle_is_clamped_or_truncated` pins the absence of the clamp in
CSS; the rendered check (`scrollWidth > clientWidth` on any `.stat-sub`) is part of the
browser pass below.

### 3. The veto chip keeps its rulebook-specific wording

Pass 1 demoted the "no event veto" banner to a quiet chip and the confirmation got lost:
the chip said `event veto: none`, which a reader cannot check against anything. The claim
a safety confirmation has to make is *which* kinds were looked for.

- `ui/api.py` now sends `event_veto_kinds` next to the existing `event_veto` — a sibling
  key, not a new field inside the veto dict, because that dict has an exact-shape test
  assertion and its meaning is "what today's cells said" whereas the new key is "what the
  rulebook would have said".
- `eventVetoBanner(eventVeto, { kinds })` names them: `event veto: none of fomc, cpi
  recorded in any cell today`.
- The chip reads `event veto: none (fomc, cpi)`, carries the full sentence in its
  tooltip, and a `?` disclosure spells out that the rulebook's own
  `event_calendar.veto_kinds` was matched against each cell's T5 condition text.
- The kinds are never hardcoded in the frontend. `config/loader.py` declares
  `veto_kinds: list[Literal["fomc", "cpi"]]` and the server sends whatever it read.
- `UNKNOWN` still renders as a banner: a rulebook we could not read is not an all-clear.

### 4. Empty calibration charts are compact, not blank

A chart panel with no series used to reserve a 220px rectangle and draw a dashed border
around nothing. Now `renderFlow()` / `renderIv()` only emit the chart container when
there is something to draw; otherwise they emit a one-line empty state plus a disclosure
carrying the why. The tables and facts around them take the space back.

- flow: `no volume series to plot yet — 24 session row(s) recorded, none carrying deep-OTM volume`
- IV: explains that the store's proxy seed rows are warmup, not a real chain line
- with data, the charts render exactly as before

### 5. The table footnote no longer crowds the last row

The frozen-note line moved out of the table wrapper into `.panel-note`, rendered after
the table with real space above it. Measured in the browser: 15px between the last row
and the note on calibration, and the last row is fully visible in all four tabs.

## Typography and spacing

A small documented scale in `style.css`, so components stop inventing sizes:

| token | value | used for |
| --- | --- | --- |
| `--fs-value` | 22px | card values (figures) |
| `--fs-head` | 11px | panel heads, chips, eyebrows |
| `--fs-body` | 13px | prose: table cells, empty states, disclosures |
| `--fs-label` | 11px | muted secondary lines |
| `--fs-tick` | 10px | chart ticks, footnotes |

- **Sans for prose, mono for figures.** Two font stacks: `--sans` and `--mono`. Table
  cells, empty states and disclosures are sans at `--fs-body`; money, counts, ratios and
  tick labels stay mono so digits align. Body text is no longer uniformly
  tiny-low-contrast monospace.
- **Word vs figure.** `statCard()` gained `word` and `date` modifiers. A word value
  (`plan unavailable`, `not open`) takes the sans stack at a size that fits; a date stays
  mono and steps down one size instead of clipping.
- **Spacing rhythm.** `--sp-block: 8px` and `--sp-section: 22px` replaced the ad-hoc
  margins; row padding is `--row-pad-y: 6px` / `--row-pad-x: 8px`, up from 3px/6px.
- **Alignment.** Numeric columns are marked `num: true` in `LEDGER_COLUMNS` and get
  right-aligned cells *and* a right-aligned header — a right-aligned figure under a
  left-aligned head is a misalignment on every row.
- **Muted cells.** One `.dash` class for "no measurement", used in tables, stat
  subtitles, arm lines and popups, replacing whoever-remembered-`muted`.
- **Contrast.** `--fg-dim: #98a2b0` and `--fg-faint: #7b8694` are the new muted colours.
  A test computes the contrast ratio of every muted token against `--bg` and against
  `--panel` and fails below 4.5:1.
- **Grouping.** Whitespace carries the grouping; separator rules between closely related
  elements were dropped rather than added.
- **Ledger.** Filter cluster and chip spacing widened so the row does not read as one
  sentence.

## Bug found while verifying: the calibration panel drew a table of dashes

Browser verification of the seeded fixture showed the flow panel with "24 sessions
missing a volume" and no chart, while `/api/calibration` was plainly sending volumes.

`flowVolume()` read `row.deep_otm_call_volume` / `deep_otm_put_volume` /
`deep_otm_total_volume`. The endpoint renames those to `call_volume` / `put_volume` /
`total_volume` on the way out (`ui/api.py`), so every volume came back null against a
real store. The unit fixtures in `tests/test_static_logic.py` had been written from the
schema rather than from a response, so they agreed with the bug.

Fixed in `model.js`; the fixtures now use the wire shape, and
`test_ui_static.py::test_the_calibration_model_reads_the_wire_key_names` pins the model
against the endpoint's own emitted key set so the two cannot drift apart again.

## Verification

- `ruff check .` clean; `957 passed`.
- Headless Chromium against both a seeded fixture and an empty journal, all four tabs:
  zero console errors on both, no clipped subtitle or value, last row fully visible in
  every table, and screenshots read back by eye.

One trap worth recording: a stale headless Chrome left listening on a fixed
`--remote-debugging-port` silently serves the *next* run the old page with the old ES
modules already in its memory cache, which reads exactly like "my fix had no effect" —
`curl` of the same file shows the new source while the page runs the old one. The
verification harness now picks a random debug port per run.