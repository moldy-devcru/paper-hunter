# Live depth probe — Alpaca options bars on this paper plan

**Date:** 2026-10-03 · **Branch:** `feature/frozen-rule-rulings` · **Credential:** live
paper creds from `~/.config/paper-hunter/soak.env` (values never printed, never written here)

Supersedes the "documented-but-unverified" data-depth section of
`docs/reviews/2026-10-03-iv-backfill-feasibility.md` §3, which was read from dated
reference notes because no credential was reachable at the time. **Everything below is a
live response from this plan**, not a citation.

## Verdict: the feed is deep enough. Backfill is feasible.

| Probe | Request | Live result |
|---|---|---|
| A1 | `SPY240621C/P00550000`, 2024-06 | 200, 2 symbols, 28 bars, 2024-06-03..2024-06-21 |
| A2 | `SPY240920C/P00555000`, 2024-09 | 200, 2 symbols, 28 bars, 2024-09-03..2024-09-20 |
| A3 | `SPY241220C/P00600000`, 2024-12 | 200, 2 symbols, 30 bars, 2024-12-02..2024-12-20 |
| A4 | `SPY250321C/P00590000`, 2025-03 | 200, 2 symbols, 30 bars, 2025-03-03..2025-03-21 |
| A5 | `SPY231215C00440000`, 2023-12 | **200, 0 symbols, 0 bars** — empty, not an error |
| A6 | `SPY230616C00430000`, 2023-06 | **200, 0 symbols, 0 bars** — empty, not an error |
| B | `SPY260918C/P00700000`, 2026-06-15..09-18 | 200, 67 bars per symbol, full life |
| D | `SPY260918C00700000`, 2024-02-01..2026-09-30 | 200, **311 bars, 2025-06-17..2026-09-18** |

**Depth floor: February 2024 confirmed live.** Pre-2024 contracts return `200` with an
*empty* `bars` map — absence, not an error, which is what `OptionBarSeries`'s docstring
already says is the documented "no data" shape. 2024-02 onward is populated for every
expiry probed, including far OTM strikes.

## Bar fields: OHLCV only, no IV on the wire

Raw envelope keys: `['bars', 'next_page_token']`. Raw bar keys:

```
['c', 'h', 'l', 'n', 'o', 't', 'v', 'vw']
sample: {"c": 62.99, "h": 66.1, "l": 62.59, "n": 19, "o": 63.93, "t": "2026-09-01T04:00:00Z", "v": 72, "vw": 64.200694}
```

`"impliedVolatility" in json.dumps(raw)` → **False**. So the feasibility doc's §1 claim
holds on live data: **IV must be inverted from the close through Black-Scholes**. There is
also no `trades`/`quotes` split on this route — one OHLCV bar per contract per day.

`vw` (volume-weighted average price) is present and is a *better* inversion input than
`c` when they differ materially, because a daily `c` on a thin contract can be a single
odd-lot print. Recorded here as an available choice; the backfill uses `c` so the stored
number is reproducible from the one field the client documents, and the delta is stated
where it matters.

## Liquidity is adequate for inversion near the money

The contracts probed carry real volume and sane OHLC: `SPY260918C00700000` on
2026-06-15 had `v=44, n=20, vw=69.21` against `c=67.66`; the 700 put had `v=1525,
n=292`. Those are the near-the-money, 90-DTE-band contracts the gate actually reads. The
one-cent-close-error sensitivity measured in the feasibility doc §3b (≈0.05 vol points at
delta 0.82) therefore applies as stated: the inversion is well-conditioned here.

## No entitlement wall — one non-reproducible 403

One early request returned
`403 OPRA agreement is not signed`. It did **not** reproduce: ~20 subsequent option-bars
requests (single-symbol, batched, historical, near and far expiry) all returned `200`.
Recorded once as observed, not chased — no retry storms. Control checks confirm the
credential is live: `get_option_chain("SPY")` returned **10,000 contracts across 26
expiries**, and stock bars served normally.

Two shape notes learned the hard way, both cheap to avoid:

* OCC strikes are **8 digits** (`SPY260918C00700000`). A 9-digit strike is `400 invalid
  symbol`; the route validates strictly against `^[A-Z]{1,5}\d{6,7}[CP]\d{8}$`.
* A symbol with no data in the window returns `200` with an absent key. Absence is data,
  not an error — the pipeline must treat it as "this contract did not trade".

## What this changes about the feasibility doc

The doc's **arithmetic verdict stands** and was not re-litigated here: under the *frozen*
`$5`-absolute key no key reaches 60 inside the frozen 1y lookback (22 best case). What
this probe establishes is the premise the doc could only cite: **the data to build a
*moneyness-keyed* series (R8, `MONEYNESS_BUCKET_PCT = 2.0`) exists, is deep enough
(Feb-2024 floor, 210 sessions inside the 1y lookback), and is priced well enough to invert.**
The doc's recommendation §6.2 — "scope it as seed the corrected series for v2" — is
therefore buildable, and this run builds it.

## ADDENDUM (f2614da, same day): that 403 is not a one-off, and it is not an error

> ### ⚠️ ERRATUM — this addendum's central claim is WRONG. Read it as history, not as a finding.
>
> **The "throttle in waves" reading below was not measured correctly and is not true.** The
> verdict that supersedes it is
> [`2026-10-03-opra-403-investigation.md`](2026-10-03-opra-403-investigation.md), and the
> correction is one line of code: **this was a 15-minute RECENCY gate on `end`, wearing the
> error text `OPRA agreement is not signed`.** It was never a burst allowance.
>
> What went wrong in the reading below is visible in its own table and was missed at the
> time: every successful `200` in that table used a window ending months ago
> (2026-06 / 2026-09), and every `403` was a *pipeline* call — i.e. one sending
> `end = <today>T23:59:59Z`. The two groups were not the same request. "The identical
> request 403s and then succeeds" was the illusion; the `end` values differed.
>
> Three measurements from the later investigation refute the throttle outright:
>
> 1. **The rate-limit budget never moved.** Across a 15-call burst of the backfill's own
>    chunk 0 in 2 seconds, `X-Ratelimit-Remaining` went 199 → 193 and never dipped, and all
>    15 returned `200`. A burst allowance being exhausted shows the budget draining to
>    zero; there was no burst allowance to exhaust.
> 2. **The 403 did not consume rate budget.** An entitlement check does not spend it; a
>    throttle does. The two OPRA refusals carried `X-Ratelimit-Remaining: 197` and `199`.
> 3. **The decisive A/B.** Two calls, same second, same 100 symbols, same credential,
>    `end` the only variable: `end=<today>T23:59:59Z` → **403**; `end=<now−20min>` →
>    **200** with all 100 symbols populated. A throttle cannot do that.
>
> **The client code built from this addendum was wrong and has been corrected.** `f2614da`
> put 403 in `RETRYABLE_STATUS` with a 180-second doubling cooldown, on the strength of a
> throttle that does not exist. That is why two runs each burned ~9 minutes of sleep
> ladder to arrive at a refusal that was never going to change. 403 is now terminal — it
> fails in one call — and the `end` is clamped by `OPTIONS_END_BACKOFF_MINUTES` at the
> source, which is the fix the whole addendum was groping toward.
>
> Two things in the addendum DO survive and are still true: the honest SIP 403 on equities
> (below), and the principle that the error text is misleading.
>
> What follows is preserved verbatim as the record of what was believed and why. Do not
> cite it as a finding.

The first real backfill run hit this and the behaviour turned out to be worth writing down,
because the error text is misleading and the naive reading of it is wrong.

**The 403 arrives in WAVES.** Roughly 10-15 requests succeed, then every request on the
route 403s for about two minutes, then the burst allowance returns. Observed sequence:

| when | request | result |
|---|---|---|
| t0 | 8-symbol batch | `403` |
| t0+~20m | 13 requests incl. 100-symbol batches | all `200` |
| t0+~25m | 100 symbols / 410-day window | `403` |
| t0+~25m | 90-day window, 100 symbols | `403` |
| t0+~26m | 20 symbols / 410-day window | `403` |
| t0+~27m | `get_option_chain("SPY")` | `200`, 10,000 contracts |
| t0+~27m | **1 symbol / 3.5-month window** | `200`, 67 bars |
| t0+~30m | **the exact 100-symbol request that 403'd above** | `200`, 50 symbols with data |
| t0+~32m | pipeline dry-run, 4 retries w/ 2-16s backoff | all 5 attempts `403` |
| t0+~34m | 3 more shapes incl. the one that just passed | all `403` |
| t0+~36m | same request again, after ~2 min idle | `200` |

**The identical request 403s and then succeeds with nothing changed on our side.** So this
is a *throttle*, not a wall, and "the OPRA agreement is not signed" is simply the wrong
error text for it. A client that treats the first 403 as terminal would have abandoned a
backfill that was in fact buildable, and a client that treats it as "retry forever" would
have escalated a ten-per-burst allowance into a self-inflicted ban. The build therefore
paces requests to a minimum interval and cools down for 30s (doubling) on a 403, bounded
at four retries — and **counts and reports both retries and cooldowns**, so a run that
leaned on forty cooldowns is visibly a worse run than one that needed none.

The documented Basic budget is 200 calls/min. **Nothing about the observed behaviour of
this route on this plan resembles that number.** Treat the burst allowance as the real
budget, and do not assume a rate-limit header is available to read back.

One genuinely *honest* 403 does exist and is different: equities on SIP answer
`403 subscription does not permit querying recent SIP data` when the query window ends less
than 15 minutes ago. That one is fixed by the same 20-minute backoff the live soak already
applies — it is a real entitlement boundary, not a throttle.

## Reproduction

`scratch/iv_backfill_probe.py` (first pass, malformed OCC — kept for the error text),
`scratch/iv_backfill_probe2.py` (entitlement control + per-case status),
`scratch/iv_backfill_probe3.py` (depth walk, field shape, batch size),
`scratch/iv_backfill_probe4.py` (raw envelope keys, stale/nonexistent symbols, long
history). All research scratch; no production code touched by this step.