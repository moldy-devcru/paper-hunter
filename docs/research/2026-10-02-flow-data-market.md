# Market survey: options-flow data, the OPRA supply chain, and a T6 recommendation

Research note — 2026-10-02. Companion to
[`2026-10-02-options-data-sources.md`](2026-10-02-options-data-sources.md), which established that
**T6 (deep-OTM 0DTE volume gate) has no honest free source.** This note answers the operator's
follow-up: what does the paid market look like, where does the data physically come from, and what
is the cheapest honest path to a T6 ratio.

**Verdict up front:** the operator's "$500/mo for Unusual Whales" memory is **stale**. Retail
options-flow products cluster at **$50-150/mo**, not $500. The cheapest *retail* flow product with
real-time OPRA is still ~$50/mo — **well above the $10-40 band**. But the band is not empty on the
*API* side: **MarketData.app Trader at $30/mo (annual billing) is real-time consolidated OPRA**,
and it is a self-serve signup with no sales call. If T6 is computed by *us* from raw trades, the
retail flow products are strictly unnecessary — they are the same tape plus a Discord bot.

**The single most decision-relevant finding in this document** is the OPRA non-display fee: **$2,000
per month per category** for systems that consume OPRA data programmatically. An automated T6
calculator is exactly the "non-display system" OPRA describes. Combined with the fact that
**delayed (>15 min) OPRA data carries $0 in OPRA fees**, this reframes the whole question: *real-time
programmatic options data is not a $30 problem, it is a $2,000+/mo problem. Delayed data is free, and
for an EOD-evaluated T6 gate, delayed is sufficient.* See §5.

---

## 1. Pricing landscape

Confidence column: **V** = read off the vendor's own pricing page in this session; **2P** = two
independent third parties agree; **1P** = single third party, treat as soft; **U** = unverified.

### 1.1 Retail flow-first products

| Product | Price (mo) | Confidence | What you actually get | T6 fit |
| :-- | :-- | :-- | :-- | :-- |
| **Unusual Whales** — Free | $0 | **V** | Options flow **delayed 2 trading days**; news 15-min; alerts 30-min | ❌ useless |
| **Unusual Whales** — Retail Basic | $50 ($42 annual, $504/yr) | **V** | Real-time flow, "Full Tape, 100% market coverage"; 25 alerts; 5 watchlists | ❌ no API at this tier |
| **Unusual Whales** — Retail Pro | $75 ($63 annual, $756/yr) | **V** | Same feed, unlimited alerts/watchlists/dashboards, 2× usage | ❌ no API at this tier |
| **Unusual Whales** — Retail Max | $120 ($102 annual, $1,224/yr) | **V** | + 1-min GEX Periscope, 3× usage | ❌ no API at this tier |
| **Unusual Whales** — Professional / Business | not published on the retail page | **U** | API access; "Not a retail trader? Explore Professional and Business plans" | ⚠️ this is where a 4-5 figure number lives |
| **Cheddar Flow** — Standard | ~$85 (annual w/ 25% off advertised) | **1P** (2 sources) | Real-time flow, curated/algo-ranked | ❌ no public API |
| **Cheddar Flow** — Pro | ~$99 | **1P** (2 sources) | + dark pool data + "AI Power Alerts" | ❌ no public API |
| **FlowAlgo** — monthly | $149 (was $199) | **V** (own site) | Realtime flow, dark pool, voice alerts, on-demand history | ❌ no API |
| **FlowAlgo** — quarterly / annual | $129 / $99 per month equivalent | **V** (own site + 2P) | same | ❌ |
| **BlackBoxStocks** | $79.97–$99.97 (annual), Premium $149 | **1P** (2 sources conflict) | Real-time flow; **dark pool + full flow in the $149 Premium tier**, not entry plans | ❌ no API |
| **SpotGamma** — Essential | $99/mo ($74 annual) | **V** (support page) | Gamma/Delta/Charm pressure models; "ingests all options traded across the US exchanges" | ⚠️ model output, not a tape |
| **SpotGamma** — Alpha | $299/mo | **V** | + HIRO 0DTE hedging indicator | ⚠️ |
| **SpotGamma** via GroupBuyTrading | from $19/mo | **1P** | shared/rotating seat | ⚠️ ToS + durability risk |
| **MenthorQ** — Premium | $129/mo (first month $39 promo) | **V** (own page) | 20+ quant models incl. options flow sentiment, gamma positioning | ⚠️ |
| **MenthorQ** — Pro | $349/mo | **V** | more models | ⚠️ |

**Cheddar Flow direct pricing page: 403 (Cloudflare interstitial), recorded once, not retried.**
Prices above are third-party. Treat the $85/$99 split as **UNVERIFIED-direct**, sourced from
[profitbuilders.io (2026-05-23)](https://profitbuilders.io/vs/cheddar-flow) and
[optionstrading.org (2026-07-29)](https://www.optionstrading.org/blog/cheddar-flow-review/);
[SourceForge](https://sourceforge.net/software/product/Cheddar-Flow/) says "starts at $56/mo" and
[purepowerpicks](https://purepowerpicks.com/cheddar-flow-review/) says $75 — the spread is wide
enough that **someone should open the checkout page in a browser before treating any number as real.**

**On the $500/mo memory:** the UW pricing page I fetched today shows retail max out at $120/mo. The
"Professional and Business plans" call-to-action is where an API-bearing tier would live, and that
is almost certainly what the $500 figure refers to — or it is simply an older number. **No source I
found puts any retail options-flow product at $500/mo today.** Recording this as a correction, not a
hypothesis: I read the live page.

### 1.2 Adjacent products (not flow-first, but priced in-band)

| Product | Price (mo) | Confidence | Note |
| :-- | :-- | :-- | :-- |
| **OptionStrat** — free / Standard / Pro | $0 / $29 / $49 | **1P** (2 sources) | Visualization + strategy builder, not a flow feed. Explicitly **OPRA-only**: "OptionStrat calculates and charts trades using data provided exclusively by OPRA" |
| **OptionStrat** — Live Tools tier | $39.99 | **1P** | via a competitor's comparison page |
| **OptionStrat** — top tier | $99.99 | **1P** | |
| **OptionStrat** — professional users | **N/A** | **V** | "Live data is only available to individual and non-professional users… unable to receive live data due to the licensing costs required for professional use" — a clean confirmation of the §5 licensing wall |
| **Trade Ideas** — Standard / Premium | $127 monthly, $89 annual / $178 annual | **1P** (2 sources) | Equity scanner + AI. Little options flow; not a T6 candidate |
| **Market Chameleon** — Options Trader / Total Access | price not captured (JS-rendered page) | **U** | "complete option market data **directly from the exchanges, with only a 15-minute delay**" — a direct-from-exchange supply-chain claim |
| **Benzinga Pro** | not verified this session | **U** | — |
| **Wallmine / SwaggyStocks / AlphaFlow / Flowgram / Stocktwits premium** | not verified this session | **U** | no credible current pricing found; several appear to be defunct or rebrand-only. **Not worth further research time** — none surfaced in any comparison piece as a live-flow alternative. |

**Bottom line for §1: there is no honest real-time retail flow product in the $10-40 band.** The
entry point is Unusual Whales Retail Basic at $50/mo, which is real-time, full-tape, no-delay — and
which still has **no API**. A human-alert product at $50-99/mo is a completely different product
category from what T6 needs.

---

## 2. Reddit and community sentiment

Threads are real, dated, and quoted from search snippets (no full-thread fetches, per constraints).
Titles + URLs + dates so they can be re-checked.

| Date | Thread | Substance |
| :-- | :-- | :-- |
| 2026-05-01 | [r/options — "Whats your preferred options flow data tool"](https://www.reddit.com/r/options/comments/1t0fhj9/whats_your_preferred_options_flow_data_tool/) | "Unusual Whales edges for flow + GEX/VEX, but **Cheddar Flow's real-time dark pool kills**. TradesViz for deep charts." Also, most usefully: "**I'd rather trust price/volume than noisy flow prints.**" |
| 2025-12-27 | [r/options — "Unusual whales - worth it?"](https://www.reddit.com/r/options/comments/1pwlj6l/unusual_whales_worth_it/) | "unusual whales is solid but honestly **pricey for what you get** if you're just starting out. their real value is the flow alerts and congress trades, but you're paying **$50+/mo** for that." |
| 2025-09-18 | [r/options — "Option flows: is it useful to you"](https://www.reddit.com/r/options/comments/1njtggp/option_flows_is_it_useful_to_you/) | "**I don't really find it useful because 95% of those option purchases on 0dte are very wrong and end up losing money.**" — the sharpest community challenge to the whole T6 premise |
| 2025-04-10 | [r/algotrading — "How hard is it to build your own options flow database instead of paying for FlowAlgo, etc.?"](https://www.reddit.com/r/algotrading/comments/1jvxi68/how_hard_is_it_to_build_your_own_options_flow/) | "**I think it's worth doing this** because a lot of this is very similar to what you have to do in production trading anyway, and the metrics tracking (large trades, odd-lots tracking, trade labeling) makes it easy to do analysis" |
| 2024-12-07 | [r/algotrading — "APIs for option flow like cheddarflow, flowalgo, etc.?"](https://www.reddit.com/r/algotrading/comments/1h8xy4g/apis_for_option_flow_like_cheddarflow_flowalgo_etc/) | "Any recommendations? I would ask for free ones, but **I feel like free DNE lol** polygon.io ?" — the gap is real and named |
| **2023-02-08** | [r/algotrading — "Raw options flow data?"](https://www.reddit.com/r/algotrading/comments/10wxled/raw_options_flow_data/) | **The load-bearing cost citation.** "Live options tick data for non-display use only will cost you between **$800 and $3000 per month** depending on provider and number of simultaneous symbols… If you want the full OPRA stream (all symbols, all quotes and trades) you need to collocate equipment in an appropriate data center and you're looking at **tens of thousands of dollars per month**." |
| 2025-07-21 | [r/options — "Market Data API"](https://www.reddit.com/r/options/comments/1m5tpo1/market_data_api/) | community price-check: "polygon.io/pricing?product=options — **$199/month for real-time data**" |
| 2025-02-11 | [r/algotrading — "API for Option prices and quotes?"](https://www.reddit.com/r/algotrading/comments/1impkz8/api_for_option_prices_and_quotes/) | "Thetadata's Standard Plan costs **$80/month**… You can also subscribe the **Value pack for $40/month and calculate IV and the Greeks yourself**." |
| 2025-12-09 | [r/algotrading — "Free APIs for tick data?"](https://www.reddit.com/r/algotrading/comments/1pidbzr/free_apis_for_tick_data/) | "**Databento is great and is definitely worth the money**" |
| 2025-02-10 | [r/algotrading — "Where Can I Get Historical Options Data?"](https://www.reddit.com/r/algotrading/comments/1ilxrr9/where_can_i_get_historical_options_data/) | "Most API providers have some kind of sample data so that you dont have to pay beforehand" |
| 2023-08-22 / 2024-03-21 | [r/options — "Which option flow scanner site do you have success with?"](https://www.reddit.com/r/options/comments/15xq9yv/) · ["cheddar flow vs Black Box vs flow algo etc"](https://www.reddit.com/r/options/comments/1bkcahx/cheddar_flow_vs_black_box_vs_flow_algo_etc/) | The consistent multi-year consensus: UW = cheaper + good, Cheddar = better dark pool but pricier, all of them = you are paying for curation/alerts, not for data you can't get elsewhere |

**Did I find a "cheap tier is derived/garbage data" warning?** Partially, and it is more nuanced than
expected:

1. The clearest *documented* derived-data case is not a paid tier at all — it is **Alpaca Basic**
   (§3.4 of the companion doc): "the quotes are not actual OPRA quotes, they're just indicative
   derivatives."
2. Among paid vendors, the recurring complaint is **not** "the data is fake" — it is **"noisy,"
   "signal-to-noise," and "you're paying for curation."** Cheddar's own pitch to justify its
   premium is curation, and Unusual Whales agrees in its own comparison copy: "Cheddar Flow
   positions itself as a premium, **curated signal** service. You are paying for their
   proprietary algo curation." Two competitors agreeing that the expensive tier is curation-over-
   identical tape is the most useful supply-chain signal in this document.
3. The 0DTE-specific warning is about **signal validity, not data quality** (the Sept 2025 thread
   above). Flagged in §6 as an experiment-design risk, not a data risk.

---

## 3. The supply chain

### 3.1 What OPRA is

The **Options Price Reporting Authority** is the SEC-registered consolidated tape for US equity
options. Per [Databento](https://databento.com/options): it "consolidates and disseminates last sale,
NBBO, local exchange quotes, and other regulatory market data across **all US equity options
exchanges**." Massive (ex-Polygon) describes itself as "an **authorized distributor of OPRA data,
covering trades, quotes and NBBO across all 17 US options exchanges**."

So: one regulator-mandated tape, seventeen member exchanges, and every retail product you have ever
paid for is downstream of the same consolidated print stream. OptionStrat says so outright: "That
means OptionStrat gets the same data that your trading platform does."

### 3.2 The chain, in text

```
   17 OPRA member exchanges (Cboe, Nasdaq PHLX/ISE/NMRX, MEMX, MIAX, BOX, C2, ARCA...)
        │  (each prints trades/quotes with a condition code)
        ▼
   OPRA consolidator — real-time NBBO + last sale, the US options "SIP"
        │
        ├──────────────────────► BROKER PLATFORM (IBKR, Schwab, Robinhood)
        │                          = your "same data as your trading platform"
        │
        ▼
   LICENSED DISTRIBUTORS / CONSOLIDATORS  (the paywall)
        • Massive (ex-Polygon)   — authorized OPRA distributor, self-serve API
        • Databento             — OPRA.PILLAR, usage-metered + $199/mo Standard
        • ThetaData             — $40 / $80 / $160 per month
        • MarketData.app        — $12 / $30 / $75 / $250 per month
        • ORATS                 — upstream Greeks/IV supplier (Tradier sits on top of this)
        • Cboe DataShop, Nasdaq, Refinitiv, Bloomberg, ICE — institutional, named-account, sales call
        │
        ▼
   RETAIL FLOW PRODUCTS  (adds a UI, a Discord bot, and an algo that says which print "matters")
        • Unusual Whales $50-120    • Cheddar Flow $85-99    • FlowAlgo $99-149
        • BlackBox $80-149          • SpotGamma $99-299     • MenthorQ $129-349
        │
        ▼
   YOU  ($0-500/mo)  ← the entire "flow intelligence" markup for a retail seat lives in this box
```

### 3.3 Licensing costs (the numbers that matter)

From ThetaData's OPRA fee guide (2026-05-29), which is a vendor restatement of the public OPRA fee
schedule:

| Use type | Fee |
| :-- | :-- |
| **Delayed (>15 min)** | **$0** — "There are currently no OPRA fees for using or redistributing data that is over 15 minutes delayed." (vendor registration may still be required to *redistribute*) |
| Non-professional, internal use | **$1.25 per user/month** (ThetaData pays on your behalf) |
| Professional, internal use | **$31.50 per user/month** |
| **Non-display (systems)** | **$2,000/month per category** — "Includes algorithmic trading, risk systems, and portfolio analytics where OPRA data can be accessed not just by one person and machine but by many. OPRA defines multiple categories of non-display usage, and fees can stack." |

And from the community (r/algotrading, 2023-02-08, `10wxled`): direct non-display live tick data
"**$800-3000/month**"; full-market OPRA with colocation "**tens of thousands per month**."

**Consequences, stated plainly:**

- A **human** looking at a delayed dashboard is nearly free to license.
- A **human** looking at a *real-time* dashboard is ~$1-32/mo in OPRA fees — trivial. Real-time is
  cheap for people.
- A **program** reading real-time OPRA is **$2,000/mo/category, non-negotiable**, because that is
  precisely the definition OPRA writes. Category stacking means a multi-purpose collector could be
  worse.
- OptionStrat's public statement that professionals are cut off from live data "due to the licensing
  costs required for professional use" is the same wall, seen from the consumer side.

**This is the real reason no cheap API sells real-time options trades cheaply.** It isn't vendor
greed; it is a fee schedule with a $2,000 line item aimed exactly at the thing we want to build.

### 3.4 "Derived/indicative" data — and why it is the free tier's only option

Alpaca's own documentation: the free **Indicative Pricing Feed** is "a free derivative of the
original OPRA feed: the quotes are not actual OPRA quotes, they're just indicative derivatives. The
trades are also derivatives and they're delayed by 15 minutes."

Why does this exist? Because of §3.3. Real-time OPRA access *costs money per user or per system*,
so a free tier can only ship a model. The ladder is consistent across vendors:

- **Free** → modelled/indicative quotes + delayed trades (Alpaca Basic, MarketData.app Free: 24h
  delay, 100 API credits/day)
- **Cheap** → *real consolidated trades, delayed 15 min* (Massive Options Starter $29, ThetaData Value
  $40, MarketData.app Starter $30, Tradier sandbox $0). **$0 in OPRA fees — legally clean.**
- **Retail "flow" products ($50-150)** → real-time consolidated tape + a UI and a Discord bot. The
  OPRA cost per human is ~$1-32/mo; the rest of your $50-150 is vendor margin on the *presentation*.
- **Real-time programmatic** → $2,000/mo/category in OPRA fees before the vendor's own price.
  Massive Options Advanced $199 and MarketData.app Trader $75 are retail *API wrappers* that absorb
  or route around the non-display fee for non-professional users; MarketData.app states plainly that
  real-time options "users need to sign the OPRA agreement" and that real-time is limited to
  non-professionals.

### 3.5 So: are the $500/mo products reselling the same OPRA feed the $30/mo ones get delayed?

**Yes — with two real and two fake differences.**

Real differences between tiers:
1. **Latency** (real-time vs 15 min vs 24 h) — the only difference that is about the data itself.
2. **Historical depth** — 1y / 5y / full archive, and point-in-time Greeks/IV. Note: **no vendor
   sells point-in-time historical Greeks/IV**; Massive staff confirmed on r/algotrading (2025-06)
   that they don't. This is why the companion doc requires self-building the IV-rank series.
3. **Condition-code decoding / sweep detection** — genuinely labor-intensive post-processing. A vendor
   that decodes OPRA condition codes, flags sweeps, and reassembles multi-leg orders has done real
   work that a weekend project has not.
4. **ATS/dark-pool coverage and corporate-structure resolution** — extra feeds, extra licensing.
5. **Alerting and curation** — the actual product.

Fake differences: any claim that the $500 product "sees" trades the $30 product does not. It doesn't.
It sees the same consolidated prints sooner and with a nicer opinion attached. The r/options
"noisy flow prints" comment and every competitor's own comparison copy agree on this.

---

## 4. API-first options for T6 specifically

The requirement is not a dashboard. It is: **programmatically, get every SPY 0DTE trade print with
size and timestamp, aggregate by moneyness bucket, and compare today's deep-OTM volume to a 20-day
baseline.** That is a REST/WebSocket poll of consolidated trades, and nothing more exotic.

| Source | Price (mo) | Options trades? | Real-time? | OPRA fees I'd owe | T6 verdict |
| :-- | :-- | :-- | :-- | :-- | :-- |
| **MarketData.app** — Free Forever | $0 (100 credits/day, 24h delay) | yes, delayed | no | $0 | ❌ 24h = no baseline-to-same-day logic |
| **MarketData.app** — Starter | **$30/mo, or $12/mo annual** | yes | stocks RT, **options 15-min delayed**, 5y history | **$0** | ✅ **best value in the band** — 15-min delay is irrelevant to an EOD gate |
| **MarketData.app** — Trader | **$75/mo, or $30/mo annual** | yes | **real-time stocks AND options**, full history | needs OPRA agreement signing; non-professional only | ✅ if an intraday T6 ever gets earned |
| **MarketData.app** — Prime | $250/mo | yes | real-time, no daily credit cap | same | ❌ overkill |
| **Massive (ex-Polygon)** — Options Basic | $0 | **options not included** (stocks only) | EOD | $0 | ❌ |
| **Massive** — Options Starter | $29 | yes (trades, quotes, candles, Greeks, IV) | 15-min delayed, 2y history | $0 | ✅ equivalent to MD.app Starter; already in the companion doc |
| **Massive** — Options Developer | $79 | yes | 15-min delayed | $0 | ⚠️ |
| **Massive** — Options Advanced | $199 | yes | **real-time** | non-display exposure | ⚠️ real-time programmatic = the $2,000 wall, routed/absorbed by Massive |
| **ThetaData** — Value | $40 | yes | real-time, self-compute Greeks/IV | vendor pays non-prof $1.25 | ⚠️ cheapest *real-time* API, but per the fee guide non-display use at $2,000/category is the risk |
| **ThetaData** — Standard | $80 | yes | real-time, vendor Greeks/IV | same | ⚠️ |
| **ThetaData** — top | $160 | yes | real-time, widest | same | ❌ |
| **Databento** — OPRA Standard | $199 | yes (PILLAR schema) | real-time subscription | licensed | ❌ for live; ✅ for **historical backfill** |
| **Databento** — usage-based | metered by bytes; $125 free credits | yes | historical | licensed | ✅ for backtesting T6 over months of history, which subscriptions don't give you cheaply |
| **Tradier** sandbox | $0 | yes | 15-min delayed | $0 | ✅ free fallback (already in companion doc) |

**Two things I could not verify this session and that decide the recommendation:**

1. **Does MarketData.app expose a per-trade options endpoint, or only quotes/candles?** Their docs
   index is JS-rendered and did not render the endpoint list through the text extractor. Their own
   marketing says "trades, quotes, candlesticks, greeks & IV" for Massive; MarketData.app's options
   page states real-time **quotes**/greeks/IV and 5y history but I did not see a trades endpoint
   listed. **Verify with one live call before buying anything.** If there is no tick-level trades
   endpoint, the answer is the *candles* endpoint (per-contract OHLCV with volume), which is
   sufficient for a **daily** T6 ratio but not for sweep detection.
2. **Cheddar Flow's actual checkout price** (§1.1). Cloudflare-blocked; needs a browser.

**Realistic monthly cost to compute our own T6 from raw consolidated trades, SPY-only:**

| Path | $/mo | Honest? |
| :-- | :-- | :-- |
| Tradier sandbox, 15-min delayed, self-compute | **$0** | ✅ honest, self-consistent baseline |
| Massive Options Starter, 15-min delayed | **$29** | ✅ honest, 2y history + daily OI |
| MarketData.app Starter, 15-min delayed | **$12-30** | ✅ honest, 5y history |
| ThetaData Value, real-time | **$40** | ⚠️ honest data, but check the non-display category question before automating |
| Massive Advanced / MarketData.app Trader, real-time | $75-199 | ⚠️ real-time; this is where the $2,000/category question lives |
| Databento OPRA, historical only | ~$0-125 credits | ✅ the cheapest way to backfill months of 0DTE SPY history |

---

## 5. Recommendation

**Path: stay at $0. Do not buy Unusual Whales, Cheddar Flow, or any other retail flow product for
this experiment.** If T6 must become real, spend **$29 on Massive Options Starter** (or $12-30 on
MarketData.app Starter) and compute the ratio ourselves. Full reasoning:

1. **T6 as an EOD gate does not need real-time data, and real-time data is the expensive kind.**
   The companion doc already recommends narrowing T6 to an EOD-evaluated gate, and §3.3 explains why
   that recommendation is not a compromise: real-time *programmatic* OPRA access is a $2,000/mo
   non-display fee by definition, while >15-min-delayed data is $0. We are choosing the cheap tier
   because it is the *correct* tier for the job, not because we couldn't afford the other one.
2. **Retail flow products are a UI, not a data source.** $50-150/mo buys the same consolidated
   prints plus a Discord bot and someone's opinion about which print mattered. Our T6 is a
   *pre-registered quantitative rule* — buying someone else's proprietary "flow ratio" would
   (a) be un-auditable, (b) not match our pre-registered definition, and (c) contaminate the
   experiment with an opaque black box. That last point alone is disqualifying under
   "Operation Hunting Blind" rules: **we cannot freeze someone else's proprietary algorithm.**
3. **The build is genuinely small for our scope.** r/algotrading (2025-04-10) says building your own
   options-flow database is "worth doing… similar to what you have to do in production trading."
   For T6 specifically we need: list the SPY 0DTE chain by strike → filter to deep-OTM (e.g. ≥3% OTM)
   → sum daily volume per bucket → divide by the trailing 20-day mean for the same bucket. That is
   one chain fetch and one aggregation per day, not a real-time system.
4. **The 20-day baseline is the real requirement, and it is a backfill problem.** We need ~20
   sessions of 0DTE SPY volume *before* T6 can gate anything. That is historical data, and
   historical data is the cheap kind: Tradier sandbox ($0) and Massive Starter's 2y history ($29)
   both cover it, and Databento's $125 free credits cover a backtest of the rule itself.
5. **Escalation ladder, pre-registered — buy only when a stated condition fires:**
   - **$0 (day 1):** Tradier sandbox or Alpaca indicative for the T6 series. Compute, log, and
     record the numbers even while T6 is *not* gating trades. This builds the 20-day baseline for
     free and gives us the baseline distribution before the gate is ever allowed to fire.
   - **$29 (when T6 is promoted from "logged" to "gating"):** Massive Options Starter — real
     consolidated trades, 2y history, daily OI, $0 in OPRA fees because everything is 15-min
     delayed. Condition to fire: the $0 tier's volumes look structurally wrong (e.g. deep-OTM
     bucket volume near zero), which would mean indicative/delayed feeds are missing 0DTE prints
     outright.
   - **$12-30 (alternative to the above):** MarketData.app Starter — 5y history, same 15-min delay.
     Preferred only if the trades/candles endpoint check (§4) comes back better than Massive's.
   - **$40 (only if an intraday T6 is ever earned):** ThetaData Value — the cheapest real-time
     options API in existence — **after** confirming with them in writing which OPRA non-display
     category a personal single-machine research script falls into.
   - **Never, for this experiment:** the $50-299 retail flow tier. Wrong product category, and it
     breaks the pre-registration discipline.
6. **Honesty disclosures to carry into the brief, unchanged from the companion doc plus this note:**
   T6 will be computed from **delayed** consolidated trades; the baseline is **self-computed**, not
   vendor-supplied; and there is community testimony (r/options, 2025-09-18) that deep-OTM 0DTE
   purchases are frequently wrong. That last point is not a data problem to solve with money — it
   is a reason to pre-register T6 conservatively and let the NO-SHOT ledger speak.

**Open items before ratification** (neither blocks writing the brief):
- One live MarketData.app call to confirm an options trades-or-volume endpoint exists at Starter tier.
- A browser visit to Cheddar's checkout to replace the $85/$99 third-party estimate with a verified
  number — this only matters if the recommendation above is ever revisited.
- A written answer from ThetaData on OPRA non-display category for a personal research script,
  before any $40 spend.

---

## Source list

**Vendor primary (fetched this session, 2026-10-02)**
- Unusual Whales — [Plans & Pricing](https://unusualwhales.com/pricing)
- ThetaData — [OPRA Fee Guide for Options Market Data](https://www.thetadata.net/articles/2026-05-29-opra-fee-guide-for-options-market-data) (2026-05-29) · [Pricing](https://www.thetadata.net/pricing) · [Options Data](https://www.thetadata.net/options-data)
- MarketData.app — [Data Plans](https://www.marketdata.app/docs/account/plans/) · [Starter](https://www.marketdata.app/docs/account/plans/starter/) · [Trader](https://www.marketdata.app/docs/account/plans/trader/) · [Prime](https://www.marketdata.app/docs/account/plans/prime/) · [Options API](https://www.marketdata.app/docs/api/options/) · [Options quotes/entitlements](https://www.marketdata.app/docs/api/options/quotes/) · [Options data](https://www.marketdata.app/data/options/)
- SpotGamma — [Subscription cost support page](https://support.spotgamma.com/hc/en-us/articles/1500002666102-What-is-the-cost-of-a-SpotGamma-Subscription)
- MenthorQ — [Premium Monthly](https://menthorq.com/membership/premium-monthly/) · [Pricing](https://menthorq.com/pricing/)
- FlowAlgo — [flowalgo.com](https://flowalgo.com/)
- OptionStrat — [membership](https://optionstrat.com/membership) · [home/OPRA claim](https://optionstrat.com/)
- Market Chameleon — [Options Trader subscription](https://marketchameleon.com/Subscription/Options) · [Total Access](https://marketchameleon.com/Subscription/TotalAccess)
- Databento — [Options](https://databento.com/options) · [OPRA.PILLAR](https://databento.com/datasets/OPRA.PILLAR) · [New OPRA pricing plans](https://databento.com/blog/introducing-new-opra-pricing-plans) (2025-06-04) · [Pricing](https://databento.com/pricing)
- Massive (ex-Polygon) — [Options](https://massive.com/options) · [Options market data API](https://massive.com/options)
- BlackBoxStocks — [Pricing](https://blackboxstocks.com/pricing/)
- Trade Ideas — [Pricing](https://www.trade-ideas.com/pricing/)

**Third-party pricing verification (secondary)**
- [profitbuilders.io — Cheddar Flow $99 Pro / $85 Standard](https://profitbuilders.io/vs/cheddar-flow) (2026-05-23)
- [optionstrading.org — Cheddar Flow Review](https://www.optionstrading.org/blog/cheddar-flow-review/) (2026-07-29)
- [SourceForge — Cheddar Flow reviews](https://sourceforge.net/software/product/Cheddar-Flow/)
- [purepowerpicks — Cheddar Flow](https://purepowerpicks.com/cheddar-flow-review/) · [Black Box](https://purepowerpicks.com/black-box-stocks-review/) (2026-08-22) · [OptionStrat](https://purepowerpicks.com/optionstrat-review/)
- [tradingtoolshub — FlowAlgo](https://tradingtoolshub.com/review/flowalgo/) (2026-03-07) · [BlackBoxStocks](https://tradingtoolshub.com/review/blackboxstocks/) (2026-03-28) · [Polygon.io](https://tradingtoolshub.com/review/polygon-io/) (2026-07-13)
- [bullishbears — Cheddar Flow](https://bullishbears.com/cheddar-flow-review/) (2026-04-22) · [Trade Ideas](https://bullishbears.com/trade-ideas-review/) (2026-04-21) · [BlackBoxStocks](https://bullishbears.com/black-box-stocks-review/) (2026-08-24) · [FlowAlgo](https://bullishbears.com/flowalgo-review/)
- [findtradingtools — MenthorQ checkout-verified](https://findtradingtools.com/tools/menthorq/) (verified 2026-09-08/09)
- [desktested — ThetaData review, $40/$80/$160 on the pricing page 2026-08-30](https://desktested.com/posts/thetadata-review/)
- [flashalpha — ThetaData pricing recheck 2026-09-17](https://flashalpha.com/articles/flashalpha-vs-thetadata-options-greeks-iv-api) · [SpotGamma](https://flashalpha.com/articles/spotgamma-review-2026-pricing-features-alternatives) (2026-09-08)
- [equitiesamerica — OptionStrat tiers](https://equitiesamerica.com/tools-directory/optionstrat-review) (2026-04-20)
- [daystoexpiry — OptionStrat $99.99/mo](https://www.daystoexpiry.com/blog/optionstrat)
- [London Strategic Edge — Polygon.io/Massive as authorized OPRA distributor, 17 exchanges](https://www.londonstrategicedge.com/directory/fundamental-data/polygon-io-massive/)
- [groupbuytrading — SpotAlpha via GBT from $19/mo](https://groupbuytrading.com/product/spotgamma/)
- [optionstrading.org — Trade Ideas tiers](https://www.optionstrading.org/reviews/trade-ideas/) (2026-05-22)
- [toolradar — Trade Ideas](https://toolradar.com/tools/trade-ideas) (2026-07-20)
- [tradercongress — Unusual Whales $60/mo](https://tradercongress.com/blog/unusual-whales-alternatives) (2026-03-07)
- [optionsmatrixpro — OptionStrat tier comparison](https://www.optionsmatrixpro.com/compare/optionstrat) (2026-08-31)

**Reddit (cited in §2)** — all linked inline in the sentiment table.
