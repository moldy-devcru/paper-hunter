# Data-source feasibility: options + SPY market data

Research note — 2026-10-02. Answers the "Data requirements (verify before brief is final)" open
question in [`../brief.md`](../brief.md): which data source covers (a) live options chains with
Greeks + IV for arms B/C, (b) ~1y IV-rank history for T5, (c) SPY daily + intraday bars for the
trigger loop — at $0 or near-$0.

**Verdict up front:** Alpaca on the free/Basic tier can cover the whole experiment *except*
real-time consolidated data, and its free options feed is **indicative, not real OPRA** — fine for
a paper experiment, with two specific caveats flagged below. The one genuine gap is T6
(deep-OTM 0DTE flow volume), which no free source gives you honestly.

---

## 1. Alpaca (paper trading, free / Basic tier)

### Tier matrix (source: [About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api),
doc updated 2026-07-16)

The Trading API has exactly two data plans. Basic is the default for **both paper and live** accounts.

| | Basic (free) | Algo Trader Plus |
| :-- | :-- | :-- |
| Equities real-time coverage | **IEX only** | All US exchanges (SIP) |
| Options real-time coverage | **Indicative Pricing Feed** | **OPRA** |
| Websocket subs | 30 symbols / 200 option quotes | Unlimited / 1000 option quotes |
| Historical data limitation | latest 15 minutes | none |
| Historical API calls | 200/min | 10,000/min |
| Price | Free | $99/month |

### What "Indicative Pricing Feed" actually means
([Historical Option Data](https://docs.alpaca.markets/us/docs/historical-option-data), verbatim):

> Indicative Pricing Feed is a free derivative of the original OPRA feed: the quotes are not actual
> OPRA quotes, they're just indicative derivatives. The trades are also derivatives and they're
> delayed by 15 minutes.

So on the free tier: synthetic quotes, trades delayed 15 min, no OPRA BBO. Same page notes
**historical options data only exists since February 2024**.

### Q1 answers

- **Full chains with Greeks + IV for SPY?** Yes, via `GET /v2beta1/options/snapshots/SPY`
  ("option chain" endpoint — [reference](https://docs.alpaca.markets/us/reference/optionchain)). It
  returns latest trade + latest quote + greeks for **every contract symbol of the underlying**.
  Per-contract snapshot fields ([reference](https://docs.alpaca.markets/us/reference/optionsnapshots)):
  `impliedVolatility` ("calculated using the Black-Scholes model"), `greeks{delta,gamma,theta,vega,rho}`,
  `dailyBar`, `prevDailyBar`, `minuteBar`, `latestQuote`, `latestTrade`. Chain params: `feed`
  (`opra`|`indicative`, defaults to `indicative` when unsubscribed), `limit` (default 100, max 1000),
  `updated_since`, `next_page_token`. A full SPY chain is a multi-page fetch — budget pages, not one call.
- **Intraday quotes?** Yes but thin: `GET /v2beta1/options/quotes/{symbols}` multi-quote latest, or the
  stream `wss://stream.data.alpaca.markets/v1beta1/indicative` ([Real-time Option Data](https://docs.alpaca.markets/us/docs/real-time-option-data)).
  Two gotchas: the option stream is **msgpack only** (error 412 otherwise), and `*` subscription is
  rejected for option quotes (error 413) — you must name symbols. Free tier caps you at 200 option
  quote subscriptions, which is fine for a single SPY expiry ladder.
- **Historical IV for IV rank?** **No point-in-time IV history.** Alpaca serves IV only as a *current*
  snapshot field, and historical option data starts Feb 2024. IV rank therefore must be **self-built**:
  poll the chain once per EOD, persist `{date, expiry, strike, iv}` to SQLite, and compute the ~1y
  percentile rank locally. `prevDailyBar` in each snapshot helps; from Feb 2024 forward the build-up
  is mechanical. Budget ~1 month of accumulation before the T5 history is statistically meaningful,
  and seed it with VIX (§4) in the meantime.
- **Paper vs live data differences?** Market **data** is identical — same entitlement, same feeds
  ([paper trading](https://docs.alpaca.markets/us/docs/paper-trading) uses the same keys/feed
  structure). What differs is *execution simulation*: no market impact, no latency slippage, no queue
  position for resting limit orders, no regulatory fees, no dividends. For 0DTE premium buys that is
  an optimistic bias worth recording on the ledger. Options trading is enabled by default in the paper
  environment ([Options Trading](https://docs.alpaca.markets/us/docs/options-trading)), up to Level 3.

### Q3: SPY OHLCV daily + 1m/5m on the free tier

- Historical equity data goes back to **2016** on both tiers.
- **Key and counter-intuitive fact** ([Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)):
  "All the latest endpoints (including the snapshot endpoint), require a subscription to be used with
  the SIP feed. **For historical queries, the `end` parameter must be at least 15 minutes old to query
  SIP data without a subscription.**" So the free tier *does* give consolidated SIP history older than
  15 minutes — i.e. daily OHLCV backfill and any intraday bar whose window has closed >15 min ago.
  **Verify empirically in implementation**; treat as documented-but-untested until a live call confirms.
- What you cannot get free: current SIP snapshot/latest-trade (error `42210000`
  "subscription does not permit querying recent SIP data"). The default feed falls back to IEX, which
  is a real exchange's prints but a small slice of consolidated volume.
- **T4 gotcha (relative volume ≥ 1.5x 20d avg):** IEX-only volume is a small fraction of consolidated.
  The ratio survives *only* if numerator and 20-day baseline are computed from the same feed. Freeze
  `feed=iex` for the whole relative-volume series (or `feed=sip` for everything historical) and never
  mix — otherwise T4 compares apples to oranges and the checklist silently lies.
- Rate limit 200 historical calls/min on Basic is not a constraint for a trigger loop polling every
  few seconds.

---

## 2. Free / cheap alternatives

### Tradier ([Market Data](https://docs.tradier.com/docs/market-data), [rate limiting](https://docs.tradier.com/docs/rate-limiting))

| Type | Brokerage API (needs a brokerage account) | Sandbox API |
| :-- | :-- | :-- |
| Equities | Realtime | Delayed (15 min) |
| Options | Realtime | Delayed (15 min) |
| Indices | Realtime (derived: NDX/RUT/COMP) | Not available |
| **Greeks** | **Hourly** | **Not available** |

- Rate limits: **120 rpm production, 60 rpm sandbox**. Delayed data is built from the same
  consolidated feed, delayed the standard 15 minutes.
- Greeks/IV on the chains endpoint are "included courtesy of the ORATS APIs"
  ([chains reference](https://docs.tradier.com/reference/brokerage-api-markets-get-options-chains)).
- **Documented contradiction, must be tested:** the chains reference advertises Greek + IV in the
  response, while the market-data matrix says Greeks are *Not available* on the Sandbox API. Open
  question for implementation: does the sandbox chain return live-computed Greeks or nothing? Do not
  design around it until answered with a real sandbox key.
- Also note "Hourly" Greeks cadence on the paid API — too coarse for a 0DTE trigger loop that needs
  delta ≥ 0.80 checks intraday. You'd recompute Greeks yourself anyway.
- Cost: brokerage account is free to open; sandbox data free. **$0.**

### Massive / Polygon.io (rebranded — polygon.io now redirects to massive.com)

Per [massive.com/options](https://massive.com/options) and [snapshot docs](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot):

| Plan | Price | Options coverage |
| :-- | :-- | :-- |
| Options Basic | free | **options "Not included"** — free tier is stocks-only |
| Options Starter | $29/mo | all US option tickers, unlimited calls, 2y history, minute aggregates, Greeks + IV, daily OI, websockets, snapshot — **15-min delayed** |
| Options Developer | $79/mo | as above, delayed |
| Options Advanced | $199/mo | **real-time** |

- Chain endpoint returns delta/gamma/theta/vega, IV, quotes, trades, open interest — a strict
  superset of Alpaca's indicative chain, consolidated (100% market coverage).
- **No historical point-in-time Greeks/IV** — Polygon staff confirmed on r/algotrading (2025-06):
  "we currently don't offer historical options greeks or IV data at a specific point in time." Same
  self-build requirement as Alpaca.
- **$29/mo is the near-$0 escape hatch** if indicative-feed quotes prove too fake, or if 2y history +
  daily OI (useful for the T6 flow gate) is worth paying for.

### Cboe

- **VIX daily history is free and verified**: `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`
  → fetched today, 9,286 rows, `1990-01-02` → `2026-10-01`. This is the right seed/benchmark for the
  T5 IV-rank series while the SPY chain history accumulates.
- **OPRA delayed data has no per-user display/non-display fees** (>15 min delayed), though
  redistribution still requires vendor registration ([ThetaData OPRA fee guide](https://www.thetadata.net/articles/2026-05-29-opra-fee-guide-for-options-market-data),
  [MarketData.app](https://www.marketdata.app/education/options/opra-fees/)). Cboe publishes free delayed
  quotes on its website; there is **no free programmatic Cboe options-chain API** worth planning around.
  Cboe DataShop (tick-by-tick OPRA) is paid institutional pricing.

### Nasdaq Data Link

- No retail-grade options chain API at $0. Nasdaq's OPRA product ("Nasdaq Smart Options") is
  cost-efficient *for firms*, not individuals ([nasdaq.com/solutions/data/nasdaq-data-link/api](https://www.nasdaq.com/solutions/data/nasdaq-data-link/api)).
- Verdict: **rule out** for this project.

### Also considered
- **yfinance / Yahoo** — free chain + IV, unofficial and flaky, no dependable Greeks history. Journal-only
  cross-check at most; never in the executor path.
- **ORATS direct** — the upstream Greeks/IV supplier behind Tradier; has its own paid data API.

---

## 3. Coverage of the brief's specific needs

| Brief need | Alpaca Basic (free) | Tradier sandbox (free) | Massive Starter ($29) |
| :-- | :-- | :-- | :-- |
| Full SPY chain | ✅ (multi-page) | ✅ per-expiry | ✅ |
| Greeks per contract | ✅ BS-computed | ⚠️ docs contradict; possibly absent on sandbox | ✅ |
| IV per contract | ✅ BS-computed | ⚠️ same ambiguity | ✅ |
| Quote quality | ⚠️ **indicative/synthetic**, trades +15 min | ✅ consolidated, +15 min | ✅ consolidated, +15 min |
| Intraday option quotes | ✅ ws (200 syms, msgpack) | ✅ +15 min | ✅ +15 min |
| ~1y IV-rank history | ❌ build it yourself (Feb 2024 data floor) | ❌ build it yourself | ❌ build it yourself (no PIT Greeks) |
| Daily OI / flow | ❌ | ❌ | ✅ daily OI |
| T6 deep-OTM 0DTE volume | ⚠️ indicative trades only | ⚠️ 15-min delayed | ⚠️ 15-min delayed |
| SPY daily OHLCV | ✅ since 2016 | ✅ | ✅ |
| SPY 1m/5m intraday | ✅ historical >15min old; IEX-only live | ✅ +15 min | ✅ +15 min |
| Cost | $0 | $0 | $29/mo |

---

## 4. Recommendation

**Ship on Alpaca paper + Basic ($0), with Cboe VIX CSV as the IV-rank seed. Keep Tradier sandbox as
the free fallback. Do not spend money until a documented need appears.**

1. **Primary: Alpaca paper keys, Basic plan.**
   - SPY bars: historical SIP where `end` is >15 min old; `feed=iex` frozen for the live/relative-volume
     series so T4 compares like with like. Confirm the free-tier delayed-SIP behavior with one live
     call before relying on it — that is the single biggest implementation unknown here.
   - Chains: `/v2beta1/options/snapshots/SPY`, paged, one EOD poll per day persisted to SQLite as the
     IV-rank history builder. Start the builder before day 1; it only gets better.
   - Execution: paper options already enabled, Level 3, no key/config work.
2. **IV rank (T5): two-source.** Seed with the free Cboe VIX daily CSV for the pre-window period, then
   switch to the self-built SPY ATM-chain IV series as it accumulates. Freeze the T5 definition in the
   brief (which expiry, ATM-tenor selection, lookback window) *before* first trade — IV rank is a
   fudge-prone statistic and the number must be pre-registered like everything else.
3. **Fallback: Tradier sandbox ($0)** if Alpaca's indicative quotes turn out to be unusable (e.g.
   bid/ask that don't behave like real markets). Resolve the Greeks contradiction with an actual
   sandbox call first. Tradeoff: sandbox has no indices and no reliable Greeks.
4. **Escalation path, only if earned: Massive Starter at $29/mo.** Buy it if (a) indicative quotes
   distort paper fills enough to corrupt the arm-B result, or (b) daily OI becomes load-bearing for
   the T6 flow gate. Both are "the experiment's conclusions are being distorted" conditions, not
   "I want nicer data" conditions.
5. **Known gap, disclose in the brief, do not paper over:** **T6** (deep-OTM 0DTE volume ≥ Nx baseline)
   has no free honest source. Indicative trades and 15-min-delayed real trades both understate
   0DTE flow, and 15-min delay is disqualifying inside a 09:45–14:00 ET trigger window. Realistic
   options: compute the 20-day baseline from the same delayed feed (self-consistent but stale), or
   narrow T6 to an EOD-evaluated gate rather than intraday. **Recommend the latter — it matches the
   hunting philosophy anyway (flow as a pre-market filter, not a trigger) and it is honest about what
   free data can support.** Flag as an operator decision at ratification.

### Freeze-worthy takeaways for the brief
- Alpaca Basic ≠ real options data. "Free" here means indicative quotes + 15-min-delayed trades.
- No free source gives point-in-time historical Greeks/IV. IV rank must be built by us, starting now.
- Free-tier SPY realtime is IEX-only; feed consistency is a correctness requirement, not a preference.

---

## Source list

- Alpaca — [About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api) ·
  [Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) ·
  [Historical Option Data](https://docs.alpaca.markets/us/docs/historical-option-data) ·
  [Real-time Option Data](https://docs.alpaca.markets/us/docs/real-time-option-data) ·
  [Option chain](https://docs.alpaca.markets/us/reference/optionchain) ·
  [Snapshots](https://docs.alpaca.markets/us/reference/optionsnapshots) ·
  [Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading) ·
  [Options Trading](https://docs.alpaca.markets/us/docs/options-trading)
- Tradier — [Market Data](https://docs.tradier.com/docs/market-data) ·
  [Get Options Chains](https://docs.tradier.com/reference/brokerage-api-markets-get-options-chains) ·
  [Rate Limiting](https://docs.tradier.com/docs/rate-limiting) · [FAQ](https://docs.tradier.com/docs/faq)
- Massive (ex-Polygon.io) — [Options product](https://massive.com/options) ·
  [Option Chain Snapshot](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot) ·
  [Unified Snapshot](https://massive.com/docs/rest/options/snapshots/unified-snapshot) ·
  [Options REST overview](https://massive.com/docs/rest/options/overview)
- Cboe — [VIX daily history CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv)
- OPRA fee context — [ThetaData OPRA fee guide](https://www.thetadata.net/articles/2026-05-29-opra-fee-guide-for-options-market-data) ·
  [MarketData.app OPRA fees](https://www.marketdata.app/education/options/opra-fees/)
- Nasdaq — [Nasdaq Data Link APIs](https://www.nasdaq.com/solutions/data/nasdaq-data-link/api)

All vendor pages fetched 2026-10-02. Vendors change tiers; re-verify pricing at implementation time.