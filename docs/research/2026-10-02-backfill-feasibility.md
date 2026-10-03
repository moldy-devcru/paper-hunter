# Backfill feasibility: can the T6 flow baseline be reconstructed from history?

Research note — 2026-10-02 (docs verified 2026-10-02/03). Answers the PRIORITY-1 question
behind `executor/backfill_flow.py`: **T6 is calibrated from a 20-day deep-OTM 0DTE volume
baseline, and the live soak (`executor/soak.py`) accumulates it one session at a time.
Do we have to wait 4 weeks, or can the history be reconstructed?**

**Verdict: PATH A is feasible, with two honest caveats that are properties of the data, not
of the code.** Alpaca serves **per-contract historical daily bars** for options on the
free/Basic tier (data since **February 2024**), and a 1-day bar's `v` is the same quantity
the live soak reads out of a chain snapshot's `dailyBar.v`. The catch is that Alpaca
**cannot tell us which contracts were listed on a past date**, so the backfill has to
enumerate the candidate 0DTE ladder itself and account for what it did not find. The code
reports that coverage on every day rather than letting a partial ladder pass as a quiet
market.

---

## 1. What the free tier actually serves for options history

From [Historical Option Data](https://docs.alpaca.markets/us/docs/historical-option-data)
(fetched 2026-10-02, page `updatedAt: 2025-09-24`):

> 🚧 Data availability
>
> Currently we only offer historical option data since **February 2024**.

> | Source | Description |
> | **Indicative** | Indicative Pricing Feed is a free derivative of the original OPRA feed: the quotes are not actual OPRA quotes, they're just indicative derivatives. The trades are also derivatives and they're delayed by 15 minutes. |
> | **OPRA** | …only available to subscribed users. |

Combined with the tier matrix in
[About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api)
(Basic = free, default on paper *and* live; options = Indicative Pricing Feed; 200
historical calls/min), and the earlier note
[`2026-10-02-options-data-sources.md`](2026-10-02-options-data-sources.md) §1:

* Historical options data on Basic is the **indicative, ~15-min-delayed, OPRA-derived**
  feed. It is not free real OPRA, and the backfilled rows must say so — they carry the
  same `feed='indicative'`, `is_delayed=1` provenance the live soak writes, plus
  `origin='backfill'`.
* **Hard data floor: February 2024.** Any backfill window before that returns empty
  windows, not errors, so a run over 2023 would silently produce *no rows* — the job
  treats a session with no data as `no_data` and never as a 0.0-volume session.
* 200 historical calls/min. A 90-day backfill costs on the order of 3–5 calls/session
  (§4), so the whole window is a few hundred calls — minutes of budget, not hours.

### 1a. Per-contract historical daily bars — YES (this is the path)

[`GET /v1beta1/options/bars`](https://docs.alpaca.markets/us/reference/optionbars)
(fetched 2026-10-03, `updatedAt: 2026-05-27`):

* Query params, taken from the OpenAPI `components.parameters` block:
  `symbols` (**required**, comma-separated contract symbols, **limit 100**),
  `timeframe` (**required**; `1Min`…`1Week`, `1M`…`12M`), `start`, `end`, `limit`
  (default 1000, max 10000), `page_token`, `sort`.
* Response: `{"bars": {"<OCC symbol>": [ {t,o,h,l,c,v,n,vw}, … ]}, "next_page_token": …,
  "currency": …}` — keyed by contract symbol, same keying discipline as the chain
  snapshot.
* `start`/`end` accept `YYYY-MM-DD` **or** RFC-3339, and are inclusive, so a single
  session is `start=end=<date>`.
* **There is no `feed` query parameter on this route.** The feed is not selectable per
  request, which is convenient for us: every backfilled row is drawn from the same
  indicative universe the live soak samples, so live and backfilled rows are
  self-consistent by construction rather than by an assumption.
* Note on pagination: the response is "sorted by symbol first, then by bar timestamp",
  and `limit` "applies to the total number of data points, not per symbol". For
  `timeframe=1Day` over a *single* session, one bar per symbol, so a 100-symbol chunk is
  one page — the per-symbol `v` is exactly that session's contract volume.

**Why bars and not trades.** The live soak's volume input is `dailyBar.v` from a chain
snapshot. The per-print endpoint
([`GET /v1beta1/options/trades`](https://docs.alpaca.markets/us/reference/optiontrades),
same `symbols`/`start`/`end`/`limit`/`page_token`/`sort` contract, response
`{"trades": {sym: [{t,x,p,s,c}, …]}}`) would let us sum sizes, but summing prints
reconstructs a daily volume that is only *probably* the bar's `v`, needs pagination
through every print of every 0DTE contract, and would then be summed with a second
piece of arithmetic that the live path does not use. Same feed, same session, same
quantity, one less way to be wrong: **bars.**

### 1b. Historical chain snapshots — NO (this is the whole complication)

[`GET /v1beta1/options/snapshots/{underlying_symbol}`](https://docs.alpaca.markets/us/reference/optionchain)
takes `feed`, `limit`, `updated_since`, `page_token`, `type`, `strike_price_gte/lte`,
`expiration_date`, `expiration_date_gte/lte`, `root_symbol`. Verified by parsing the
reference's OpenAPI block: **there is no `asof`, no historical `date`, and no
point-in-time parameter of any kind.** `expiration_date*` *filters* the currently
listed chain; `updated_since` is a chain-refresh cursor, not an as-of selector.

So: **we cannot ask Alpaca "which SPY contracts were listed for expiry 2026-06-11?"** A
contract that has expired is not in the snapshot. The backfill therefore *enumerates*
candidate OCC symbols for the 0DTE expiry from the session's own SPY close (a strike
grid over ±`--range-pct`, at `--strike-step` increments) and treats "no bar for that
symbol" as **no volume**, which is the honest reading: a contract with no daily bar did
not trade that day.

### 1c. What the "since February 2024" claim covers

The claim sits on the *Historical Option Data* page, which introduces the historical
options API as a whole and links to the bars/trades references. It is not scoped to bars
only, and it is a data-retention statement, not an endpoint-specific one. The
practical consequence for this job is one thing: **a backfill window that starts before
2024-02 returns nothing for those sessions**, and the job reports them as `no_data`
rather than writing zeros.

### 1d. Explicitly NOT available (and therefore not attempted)

* Point-in-time historical **Greeks/IV** for any past session (same conclusion as the
  earlier data-source note). The backfill writes `flow_baseline` rows **only**; it does
  not touch `iv_observations`, so **T5's IV-rank history still has to accumulate live.**
  A past 0DTE contract's IV is not reconstructible from a daily bar, and pretending
  otherwise would put a fabricated tenor in a percentile series.
* Open interest, in any form, on any free tier. Not used by the flow math.
* Anything about the *direction* of flow (call vs put volume is a count of contracts, not
  notional, and not a signed flow). Same as the live soak's definition.

---

## 2. PATH B (the honest-empty branch) and why it was not taken

PATH B was: *if only current-style snapshots exist, declare the backfill impossible, and
propose alternatives.* It is **not** the case — PATH A exists. For the record, these are
the alternatives that were available and are *not* needed, so nobody re-opens them later:

* **Cboe daily volume summaries** — free, but they are per-underlying/aggregate volumes,
  not per-contract, so they cannot be bucketed by distance-from-spot. They answer a
  different question than T6 asks.
* **Shorter baseline** — accept 4 weeks of soaking. This is the fallback if the lead's
  live run shows the endpoint refusing historical option data on Basic (see §5).
* **Massive/Polygon Starter ($29/mo)** — consolidated, 2y history, daily OI. Not bought;
  nothing in the backfill requires it, and the brief's "don't spend until a documented
  need appears" rule still holds.

## 3. What the backfill deliberately does NOT do

* **No fabricated zeros.** A session where the 0DTE ladder returns no volume at all is
  reported as `no_data` and **no row is written**. The live soak already refuses to write
  a zero row when no 0DTE expiry is listed, because a 0.0-volume session is a fake
  observation in a distribution `N` is calibrated from; the backfill inherits that rule
  verbatim.
* **No IV backfill** (§1d).
* **No re-derivation of the aggregation math.** The backfill calls
  `soak.aggregate_flow` and `soak.build_flow_row` — the same functions the live soak
  calls. A forked copy of the bucketing would be a second definition of "deep-OTM".
* **No UPDATE/DELETE.** `flow_baseline` stays append-only; `origin` is written at insert.

## 4. Cost of a 90-day window

Per session: 1 SPY daily-bar call is **not** per session (one range call covers the whole
window), then `ceil(2 * strikes_in_range / 100)` option-bar calls. At the defaults
(±10% of spot at $1 steps on a ~$650 SPY, 0DTE expiry): ~130 strikes × 2 rights = 260
symbols ≈ **3 calls/session** ⇒ ~270 calls for 63 sessions. At 200/min that is ~2 minutes
of budget; the client paces itself through the existing `RateLimiter` and the job
reports its own call count. At `--range-pct 5` the cost roughly halves and the histogram
still re-derives any threshold up to 5pp exactly.

## 5. What is documented-but-unverified (the operator's first run should check this)

Everything in §1a is read out of the reference's OpenAPI block, not out of a live call —
**no credentials exist in the build environment, so nothing here was executed against
Alpaca.** The two claims a first live run should confirm, in the order they will bite:

1. **A single expired 0DTE contract symbol returns its session's daily bar** (rather than
   `422 invalid symbol`, which would mean the enumeration approach needs a per-symbol
   fallback). The job treats a *batch-level* 4xx as a failed session and reports it; it
   does **not** silently retry symbol-by-symbol, because that would turn a systematic
   rejection into a slow, quiet undercount.
2. **Basic-tier keys are entitled to `/v1beta1/options/bars` history at all.** The tier
   matrix names the indicative feed as the free options entitlement and the historical
   page is not paywalled, so the expectation is yes. If the first run 403s, the fallback
   is PATH B's: keep the code, run nothing, and soak live (the run reports the HTTP
   status verbatim so the diagnosis does not have to be re-derived).

Both are single `--from/--to` one-day runs; the exact commands are in the module
docstring of `executor/backfill_flow.py`.

## 6. Verdict

**PATH A. Implemented.** `executor/backfill_flow.py` reconstructs each past session's
deep-OTM 0DTE volume and 1pp distance histogram from
`GET /v1beta1/options/bars?symbols=…&timeframe=1Day`, reuses the live soak's aggregation
and row-building functions verbatim, and writes `flow_baseline` rows with
`origin='backfill'` so a reader can always tell reconstructed history from soaked
history. It does not run the live backfill itself, and it does not touch the IV store.

## Sources (all fetched 2026-10-02/03)

- [Historical Option Data](https://docs.alpaca.markets/us/docs/historical-option-data) —
  Feb-2024 floor, Indicative-vs-OPRA table
- [Historical option bars reference](https://docs.alpaca.markets/us/reference/optionbars) —
  `GET /v1beta1/options/bars`, params, response envelope
- [Historical option trades reference](https://docs.alpaca.markets/us/reference/optiontrades) —
  `GET /v1beta1/options/trades` (the per-print alternative, documented, not used)
- [Option chain reference](https://docs.alpaca.markets/us/reference/optionchain) —
  no point-in-time parameter
- [About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api) —
  Basic tier entitlements, 200 historical calls/min
- [`2026-10-02-options-data-sources.md`](2026-10-02-options-data-sources.md) §1 and
  [`2026-10-02-flow-data-market.md`](2026-10-02-flow-data-market.md) §4 — prior findings
