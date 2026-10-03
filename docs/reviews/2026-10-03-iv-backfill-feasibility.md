# T5 IV Tenor Backfill Feasibility

**Date:** 2026-10-03 · **Branch:** `feature/frozen-rule-rulings` · **Author:** feasibility spike (research only, no production code touched)

**Question put to the spike:** can T5's IV tenor series be **backfilled** from historical data instead of waiting for live soak days?

---

## Verdict

**INFEASIBLE under the currently frozen key semantics — and the reason is structural, not a data-availability problem.**

Backfill fails for **two independent reasons**, either of which alone is fatal:

1. **The 1-year lookback, not the data floor, is the binding constraint.** `iv_rank_lookback: "1y"` (`config/rules.example.yaml:251`) resolves to `DEFAULT_LOOKBACK_DAYS = 365` (`executor/iv_rank.py:80`), and `IvRankStore.iv_rank` gates warmup on `if len(window) < floor` (`executor/iv_rank.py:583`, where `window = self.history(..., lookback_days=lookback_days)` at `:540`). The trailing 365 days before window open contain **210 sessions**, not the 718 that a Feb-2024-to-present backfill could supply. Backfilling more than a year of history is *arithmetically discarded* by the frozen lookback.
2. **The `$5` absolute strike bucket rotates with spot, and it is the wall the debate doc already named.** Inside that 210-session window the busiest possible tenor key collects **22 observations**; across the *entire* 718-session history the best any key reaches is **25**. `MIN_OBSERVATIONS = 60` is not approached in either case. So **yes — backfill hits exactly the same wall as live soak**, and no amount of extra history helps.

**What backfill *does* buy:** a corrected series that is warm going forward, and — more importantly — it makes the *key-schema* ruling (§6) cheap enough to actually test before the next window. That is worth doing, but it is a v2 rulebook change on the operator's signature, not a fix that rescues November.

**Two corrections to the premises of the question, stated up front:**

- **The repo has no Black-Scholes machinery.** The spike was briefed to "find and cite it." It does not exist in code: `grep -rni 'black.?scholes\|bsm\|norm_cdf\|ndtr'` across `executor/` returns nothing. The only Black-Scholes mentions in the repo are prose in `docs/reviews/2026-10-03-algo-outsider-review.md` (F9, an unimplemented BS *grid* for arm C's strike selection) and the one-line description of Alpaca's own IV field in `docs/research/2026-10-02-options-data-sources.md:47`. Any IV-inversion backfill would be **net-new code**, including a solver, a rate source, and a dividend source — none of which exist.
- **No Alpaca credential is reachable from this environment.** `AlpacaClient.from_env()` raises `AlpacaConfigError: missing Alpaca credential env var(s): ALPACA_PAPER_KEY, ALPACA_PAPER_SECRET`; neither is set in the environment. **Zero probe calls were made.** Everything in §3 about the API is read from the repo's own already-verified reference notes, not from a live response, and is labelled as such. No result in this document is invented.

---

## 1. Does the client have a historical options bars route? Yes — and it is OHLCV, not IV

`GET /v1beta1/options/bars` is wired and tested:

- `OPTIONS_HISTORICAL_BARS_PATH = "/v1beta1/options/bars"` — `executor/alpaca_client.py:104`
- `MAX_OPTION_SYMBOLS_PER_REQUEST = 100` — `executor/alpaca_client.py:110`, enforced in `chunk_option_symbols` (`:123`) and re-checked in `get_option_daily_bars` (`:1200`), which raises rather than silently truncating a ladder
- `AlpacaClient.get_option_daily_bars` — `executor/alpaca_client.py:1167-1213`
- route/pagination/limit contract pinned by tests: `tests/test_alpaca_routes.py:41,61,115-116,155,187`

The response envelope is `{"bars": {<OCC symbol>: [{t,o,h,l,c,v,n,vw}]}, …}` — **no `impliedVolatility` field anywhere.** That is not a defect of the client; it is what the route serves, and it is the same fact `docs/research/2026-10-02-backfill-feasibility.md` §1a records from the reference.

**So backfill would mean inverting the bar `c` through Black-Scholes against the same day's SPY close** — and, per the correction above, that machinery does not exist and would have to be written. The maths is *benign* (see §3b), which makes this a build-cost question, not a physics question.

## 2. Symbol discovery: the real cost, and the wall it runs into

### 2a. Per-day symbol set size and total request volume

Alpaca serves **no point-in-time chain** (`docs/research/2026-10-02-backfill-feasibility.md` §1b: no `asof`, no historical `date`), so a backfill must *enumerate* candidate OCC symbols per session from that session's own SPY close, exactly as `backfill_flow.py` already does for T6.

For arm C's frozen `dte: [90, 180]` band (`arms.C.entry.dte`, read by `soak._bands_from_rules` at `executor/soak.py:413-426`), the band contains **~20 listed expiries** (SPY weeklies plus third-Fridays) across the 90-day window. Measured in `scratch/iv_backfill_cost.py`:

| strikes either side of ATM | symbols/day | calls/day | calls over 718 sessions | Basic-tier budget (200 calls/min) |
|---|---|---|---|---|
| 1 | 40 | 1 | 718 | 3.6 min |
| 3 | 120 | 2 | 1,436 | 7.2 min |
| 5 | 200 | 2 | 1,436 | 7.2 min |
| 11 | 440 | 5 | 3,590 | 17.9 min |

**Cost is not the obstacle.** Even the widest enumeration is under 20 minutes of rate-limit budget across the whole available history. Whoever thought this was expensive should be told plainly: it is cheap, and it still does not work.

### 2b. The strike-rotation wall — the key question, answered

`dte_tenor_key` emits `dte{bucket}-{side}-{strike_bucket}` where `strike_bucket = math.floor(strike / bucket_size + 0.5) * bucket_size` (`executor/iv_rank.py:283`, formula at `:291`; key emitted at `:280`; `STRIKE_BUCKET_SIZE = 5.0` at `:77`). The soak writes the **nearest-to-spot** contract in the arm's band (`soak.arm_contract_for_band`, `:430-462`, selector at `:460`), and the gate reads the same selection (`hunt_plan._iv_contract_for_arm`, `:671-716`, selector at `:716`). So the strike axis tracks spot — the finding Amendment 3 of the debate doc recorded, now quantified against the actual frozen lookback.

Measured on real SPY daily closes (`scratch/iv_backfill_lookback.py`, Yahoo chart pull in `scratch/spy_closes.json`, using the repo's own `dte_tenor_key`/`strike_bucket` so the numbers cannot drift from frozen semantics):

```
MIN_OBSERVATIONS=60, lookback=365d, as_of=2026-11-02 (window open)
trailing 365d: 210 sessions (2025-11-03..2026-09-03), SPY 631.97..777.88

CURRENT frozen key (absolute $5 strike bucket):
  dte bucket fixed at 126 (best case)      keys= 30  max=22  keys>=60=0
  dte bucket rotating over 13 arm-C buckets keys=159  max= 4  keys>=60=0

Widening the ABSOLUTE strike bucket (same 365d window, dte fixed):
  $10 -> keys=15 max=40  keys>=60=0
  $20 -> keys= 8 max=64  keys>=60=1
  $25 -> keys= 7 max=60  keys>=60=1
  $50 -> keys= 4 max=96  keys>=60=2

FULL 650-session history from the 2024-02 Alpaca floor, lookback ignored:
  $5 absolute -> keys=59 max=25  keys>=60=0
```

Three things follow, and they are the whole verdict:

1. **Even the best-case single key inside the frozen lookback gets 22 of the 60 it needs.** The gap is ~2.7x, not a rounding miss.
2. **The full 718-session backfill tops out at 25** for the luckiest key. There is no historical window, at any depth, in which the frozen `$5`-absolute key reaches 60 on SPY in this regime. The debate doc's independent measurement ("no key has reached 60 since 2020, best 54") is consistent; the tighter 365-day lookback makes the answer worse.
3. **Backfill therefore hits the *same* wall as live soak, and a strictly worse one.** Live soak at least gets 1 fresh observation per session going forward; a backfill tops out at 22–25 and then decays as the lookback rolls. It is not a substitute for soaking — it is a *shorter* substitute.

**Rotation is not the whole story, and the naive workaround does not rescue it.** One might imagine writing *every* DTE bucket per session so the gate's pick is always covered. Measured: that fixes the DTE axis and leaves the strike axis untouched — `$5` strike with all 13 DTE buckets written gives **max 22**, still zero keys at 60. The DTE axis is not the binding constraint; the strike axis is.

## 3. Data depth, plan limits, and what the bars actually support

*(No live calls were made — no credential is reachable. Everything here is read from the repo's existing, dated reference notes; treat it as documented-but-unverified, exactly as `docs/research/2026-10-02-backfill-feasibility.md` §5 labels its own findings.)*

### 3a. Depth and entitlement

- **Hard floor: February 2024** (`docs/research/2026-10-02-backfill-feasibility.md` §1, citing the Historical Option Data page). Sessions from 2024-02-01 to window open: **718**.
- **Basic tier (free) carries the entitlement**; options on Basic are the **Indicative Pricing Feed** — OPRA-derived, not real OPRA, trades delayed 15 min. Backfilled rows would need the same `feed='indicative'` / `is_delayed=1` provenance the live soak writes, plus an `origin='backfill'` marker.
- **200 historical calls/min** — irrelevant at the volumes in §2a.

### 3b. Is IV derivable from the bars? Mathematically yes, at these tenors

Black-Scholes inversion is numerically well-conditioned at arm C's tenors because vega is large. Computed in `scratch/iv_backfill_cost.py` (spot 650, r=4%, q=1.3%, σ=16%):

| contract | delta | DTE | vega ($ per 1.00 vol) | IV vol-points per close error |
|---|---|---|---|---|
| ATM 120d (what the gate reads) | 0.55 | 120 | 147.18 | $0.05 → 0.034 pts |
| delta-0.80 120d (what C trades) | 0.82 | 120 | 95.35 | $0.05 → 0.052 pts |
| delta-0.80 165d (band top) | 0.87 | 165 | 88.70 | $0.05 → 0.056 pts |
| ATM 45d (arm B band top) | 0.53 | 45 | 90.70 | $0.05 → 0.055 pts |

A one-cent close error is worth ~0.05 vol points — noise that a percentile rank over 60+ observations can absorb. The classical failure mode for inversion (**deep-ITM → vega collapse → IV indeterminate**) does not bite at delta 0.82. So the inversion is *sound*; it just buys nothing, because the key never reaches 60.

### 3c. Three provenance problems a backfill would create

These are the costs that are easy to miss and expensive to get wrong:

1. **The live series and a backfilled series would not be the same measurement.** Live reads `contract.implied_volatility` off the chain snapshot; a backfill inverts a daily bar close. One is a model output from a quote, the other from a trade-derived indicative close. Mixing them inside one percentile series is a silent methodological splice. It needs to be either separated or flagged loudly.
2. **There is no honest `source` value for a backfilled row.** `IVSource = Literal["alpaca_chain", "cboe_vix_csv", "manual"]` (`executor/iv_rank.py:82`). An inverted-from-bar IV is none of the three. Adding one is a schema change; reusing `manual` would be a lie in a column that exists precisely to keep provenance honest.
3. **The `is_proxy` flag is a blunt instrument.** It is a single bit meaning "Cboe VIX, a different index" (`executor/iv_rank.py:17-23`, honesty rule 2 in the module docstring). A bar-inverted SPY IV is *not* a proxy — it is the right index, wrong construction. Reusing the bit would make a derived number look like a first-class observation.

None of these are reasons to refuse the backfill. They are reasons it is **not the two-day job it looks like**, on top of not being useful.

## 4. Pre-registration: does the frozen text say "observations" or "live-recorded observations"?

The exact frozen language, quoted:

> **T5: Options-chain corroboration — no hostile IV setup: entry-tensor IV rank < 50 for
> premium-buying (B), or for C, IV on the chosen strike within normal band**
> — `docs/brief.md:57-59`

> `iv_rank_max: 50` / `iv_rank_lookback: "1y"` (arm B, `config/rules.example.yaml:250-251`); `requirement: iv_on_chosen_strike_within_normal_band`, `iv_rank_max: 50` (arm C, `:253-259`)

> An IV rank needs `MIN_OBSERVATIONS = 60` observations in the tenor key.
> — `docs/ratification.md:572`

> "IV rank needs ~1y of history for the chosen strike — open data question in the brief."
> — `config/rules.example.yaml:285`

> …so we have to accumulate it ourselves, one EOD poll per day, from before the window opens.
> — `executor/iv_rank.py:5-8`

**Both readings are available, and this is genuinely an interpretation ruling for the operator.**

- **Reading A — "observations" is unqualified.** The frozen documents say *observations*, *history*, *~1y of history*. Nothing in `brief.md`, the rulebook, or the ratification says observations must be *live-recorded*, *EOD-polled*, or *point-in-time snapshot*. On this reading a backfilled observation that occupies the same key with a defensible `source` counts, and the ~1y requirement is about **window length**, which is exactly what §2b shows is unobtainable anyway.
- **Reading B — "accumulate… one EOD poll per day" is part of the freeze.** `executor/iv_rank.py:5-8` says the series is built by polling, and the module's whole honesty apparatus (`is_proxy`, `source`, the two-key design) is built around the assumption that a stored IV is what a live chain said on a past date. On this reading a reconstructed IV is a different kind of object and does not count toward the frozen 60.

**Which reading is right matters less than it looks, and that is the honest finding:** under Reading A the answer is still **no**, because §2b caps the key at 22–25 observations inside the frozen 1-year lookback. Reading B only removes an option that was already closed.

**Where it *would* matter:** if the operator simultaneously rules on the lookback (e.g. 2y or "all available history") in the same breath as a key-schema change, then Reading A vs B becomes load-bearing, because more history would then be countable. **Recommendation: do not rule on the two questions in isolation.** Rule them together or neither.

## 5. Verdict arithmetic, stated plainly

| | live soak only | backfill (frozen key, frozen 1y lookback) |
|---|---|---|
| observations in the gate's key at window open 2026-11-02 | 0 | **≤ 22** (best case) |
| `MIN_OBSERVATIONS` | 60 | 60 |
| reaches 60 on | **2027-01-23** (60 sessions from window open) | never, within the lookback |
| requests | ~1 EOD poll/session | 718–3,590 calls, < 20 min of budget |
| net new code | none | BS inversion solver + rate source + dividend source + `source` enum + provenance flag |

**Can arm C's tenor key reach 60 observations before 2026-11-02? No — under the frozen key, by a factor of ~2.7x, and not by *any* depth of history.** The strike-rotation wall applies, and it applies identically to backfill and to live soak. Request cost and inversion difficulty are both non-issues; the wall is the key schema.

## 6. What the spike actually recommends

Backfill is **not** the answer to November. Three things are worth the operator's time instead, in priority order:

1. **Rule the key schema (already queued as v2 item 1, `docs/ratification.md` §e).** The measurement above makes the ruling cheap to make well. Within the frozen 365-day lookback, `$20` absolute buckets reach 64 observations and `2%` **moneyness-relative** buckets reach 210 — the moneyness version is the one the debate doc already names as the candidate fix, and it is the only variant that clears 60 with room to spare. Note this **changes what T5 measures** and belongs in the v2 version seam with its own ratification entry, not in a bugfix.
2. **If backfill is wanted anyway, scope it as "seed the corrected series for v2", not "rescue November."** It is cheap in requests and sound in the maths, and it is the only way to have a *moneyness-keyed* series warm on day one of the next window instead of 60 sessions in. That requires new BS code and a new `source` value, so it is a scoped build with a named owner, not a script.
3. **Do not spend the ruling on Reading A vs B in isolation** (§4) — it changes nothing until §6.1 is decided.

**What would change this verdict:** a longer frozen lookback (2y+), *combined with* a key-schema ruling. Those two together would make backfill genuinely sufficient. Either alone does not.

---

### Reproduction

- `scratch/iv_backfill_lookback.py` — the load-bearing numbers (§2b, §5). Uses the repo's own `dte_tenor_key`/`strike_bucket`; SPY closes from `scratch/spy_closes.json` (Yahoo chart API pull, 921 sessions, 2023-01-03..2026-09-03).
- `scratch/iv_backfill_cost.py` — symbol-set sizing, request volume, vega/inversion conditioning (§2a, §3b).
- `scratch/iv_backfill_arithmetic.py` — rotation counts over multiple sample starts and strike-bucket-width sensitivity (§2b).
- Research scratch only; no production code was modified.