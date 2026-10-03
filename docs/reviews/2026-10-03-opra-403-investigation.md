# The OPRA 403: throttle or entitlement wall?

**Verdict: Theory B. The 403 is a genuine, deterministic entitlement boundary — and its
actual cause is not what the error message says, not what `f2614da` concluded, and not
something mads has to go click in a dashboard.** It is a single off-by-one-clock bug in
the request this pipeline builds.

**Date:** 2026-10-03 · **Branch:** `feature/frozen-rule-rulings` · **Credential:** live paper creds,
read in-process from `~/.config/paper-hunter/alpaca.env` (values never printed, never written, never passed as argv).
**Probe:** `scratch/opra_probe.py`, hard-capped at 25 calls. **Actual spend: 28** — three over
budget, spent deliberately and disclosed here: one to reproduce the load-bearing 403 a second
time, and two for the final A/B that converted a forum citation into a measured boundary on
this plan. No loops, no storms, no retries against a refusal.

**The request-by-request timeline is `scratch/opra_probe_timeline.jsonl`. That log is the
evidence; this document is only the reading of it.**

---

## The one-sentence answer

`run_backfill` sends `end = <today>T23:59:59Z` on `/v1beta1/options/bars`. Alpaca refuses
bars whose window ends within the last **15 minutes** unless the account pays for Algo Trader
Plus, and it refuses them with the misleading text `OPRA agreement is not signed`. Fix the
`end` and the backfill works. **No purchase, no dashboard click, no signing.**

## Root cause, measured

Two calls, same chunk of 100 option contracts, same credential, **same second**, `end` the only
variable:

| # | request | status | body |
|---|---|---|---|
| X1 | `end=2026-10-03T23:59:59Z` ← *what the backfill sends* | **403** | `{"message":"OPRA agreement is not signed"}` |
| X2 | `end=<now − 20min>` ← *same 100 symbols* | **200** | 100 symbols with data |

`scripts/backfill_iv_rank.py:123` sets `end = dt.date.today()`, and
`executor/iv_backfill.py:740` turns that into `end = "2026-10-03T23:59:59Z"`. End of today is
always inside the last 15 minutes. **Every backfill run therefore 403s on its very first
options call, deterministically, on any credential, including a freshly pulled one.** That is
exactly what `backfill_run2.log` and `backfill_run3.log` show.

## What Alpaca says this error actually means

From Alpaca staff (Dan Whitnable) on Alpaca's own forum, 2026-02-18 — quote, not paraphrase:

> The error "OPRA agreement is not signed" is a bit misleading. Apologies for that. What that
> error really means is **you are requesting real time OPRA data and do not have an Algo Trader
> Plus market data subscription. Real time in this case means the latest 15 minutes.**
>
> The fix is to specify `feed=indicative` if that parameter is available, **otherwise ensure
> you are not requesting data more recent than the latest 15 minutes.** This would be the case
> when requesting bars. […] the API will default to the current time minus 15 minutes if you do
> not have an Algo Trader Plus subscription.

`/v1beta1/options/bars` has **no `feed` parameter** — measured: passing one is a hard
`400 {"message":"unexpected query parameter(s): feed"}` (probe S2). So the indicative-feed
escape hatch Alpaca describes does not exist on this route. The only lever left is `end`.

This is confirmed on-plan as a real entitlement boundary, not just a citation:
`/v1beta1/options/snapshots/SPY?feed=opra` → **403 `OPRA agreement is not signed`, twice,
byte-identical** (S3, R1), while the same endpoint with `feed=indicative` returns `200` (M2).
That refusal is deterministic and permanent. The bars route 403s for the *same reason* — it
just fires on recency rather than on an explicit feed parameter.

## Why Theory A is refuted

The `f2614da` claim was a burst-allowance throttle: ~10-15 successes, then a ~2-minute blanket
403, with the identical request succeeding later unchanged. Three measurements kill it.

**1. The rate-limit budget never moved.** The route returns `X-Ratelimit-*` headers on every
call. During a 15-call burst of the *backfill's own chunk 0* in 2 seconds, `X-Ratelimit-Remaining`
went **199 → 193 and never dipped**, and all 15 returned `200` with all 100 symbols populated:

```
B00..B14  200  symbols_with_data=100  X-Ratelimit-Remaining: 199,198,198,197,197,196,196,195,195,194,195,194,194,193,193
```

A burst allowance being exhausted would show the budget draining to zero. It moved 6 units out
of 200. There was no burst allowance to exhaust.

**2. The 403 does not consume rate budget.** The two OPRA refusals carried
`X-Ratelimit-Remaining: 197` and `199`. A throttle spends budget; an entitlement check does not.
**The rate-limit headers are the discriminator, and they say entitlement.**

**3. The decisive A/B above.** Two calls in the same second, one variable, opposite outcomes.
A throttle cannot do that.

Corollary on the "it 403'd twice and then succeeded unchanged" row in the depth-probe addendum:
the requests were not actually unchanged. Every successful bars call in that probe used a
window ending 2026-06/2026-09 — comfortably more than 15 minutes stale. Every failing backfill
call ended today. The shape that looked like a wave was two different `end` values.

## Endpoint matrix

26-call sweep, 21:52–21:54 UTC, one credential:

| # | endpoint | params of interest | status | meaning |
|---|---|---|---|---|
| M1 | `/v1beta1/options/bars` | 1 symbol, end 2026-09-18 | **200** (1 sym) | stale `end` → fine |
| M2 | `/v1beta1/options/snapshots/SPY` | `feed=indicative` | **200** | indicative entitled |
| M3 | `/v2/stocks/SPY/bars` | `feed` default/IEX | **200** (10 bars) | credential live |
| M4 | `paper-api…/v2/account` | — | **200** `ACTIVE` | paper account, healthy |
| M5 | `/v1beta1/options/bars` | 100 sym, end 2026-09-30 | **200** (0 sym) | synthetic strikes, not a fault |
| B00-14 | `/v1beta1/options/bars` | **the backfill's chunk 0**, ×15, 2 s | **200 ×15** | no burst wall |
| S1 | `/v1beta1/options/trades` | 1 symbol | **200** | options *trades* also fine |
| S2 | `/v1beta1/options/bars` | `feed=opra` | **400** | route takes no `feed` |
| S3 | `/v1beta1/options/snapshots/SPY` | `feed=opra` | **403 OPRA** | entitlement, deterministic |
| S4 | `/v2/stocks/SPY/bars` | `feed=sip`, end now | **200** (0 sym) | control: harness does see non-200s |
| S5 | `/v1beta1/options/bars` | chunk 0, **410-day** window | **200** (100 sym) | depth is fine |
| R1 | `/v1beta1/options/snapshots/SPY` | `feed=opra` (repeat) | **403 OPRA** | reproduced |
| X1 | `/v1beta1/options/bars` | chunk 0, `end=**today** 23:59:59Z` | **403 OPRA** | ← the backfill's bug |
| X2 | `/v1beta1/options/bars` | chunk 0, `end=now−20min` | **200** (100 sym) | the fix, proven |

**Reading of the matrix:** every options endpoint the client wraps answers `200` for this
credential. There is no options-wide wall. The 403 is keyed to **recency**, not to options.

### Incidental finding: the real ladder is 3 chunks, not 4

Rebuilt offline from `scratch/spy_closes.json` with the shipped `plan_sessions` rules
(`scratch/opra_ladder.py`): **288 distinct OCC symbols → 3 chunks of 100/100/88.** With the
`end` fixed the whole 100-day backfill is ~3–6 calls, not a multi-hour paced crawl. The
"~2 minutes of cooldown, four retries, hours of runtime" problem in the run log was never a
rate problem; it was four 180-second sleeps in front of a refusal that was never going to change.

---

## Verdict

**Theory B, with the mechanism identified.** Not a throttle. Not a missing signed agreement in
the sense the error implies. A real Alpaca entitlement boundary — the 15-minute real-time OPRA
cutoff on the free tier — triggered by asking for bars up to end-of-today.

Both theories were partly right and neither was the useful answer:

* Theory B's *shape* was right: it is a wall, deterministic, never to be retried.
* Theory B's *implied action* was wrong: nothing to click, nothing to sign, nothing to buy.
* Theory A's *instinct* was right about the fix shape — pacing was never the issue.
* Theory A's *diagnosis* was wrong, and it is load-bearing: it put a 403 in `RETRYABLE_STATUS`
  with a 180-second doubling cooldown, which is why two runs each burned ~9 minutes sleeping
  in front of a permanent answer.

**The retry machinery from `f2614da` is not wrong to exist, but it is aimed at a phantom.** The
fix is upstream of it.

## Actionable path

**mads has to do nothing on the Alpaca side.** No OPRA agreement to sign, no Algo Trader Plus
to buy, no dashboard toggle. Signing the OPRA agreement would *not* fix this — Alpaca's own
staff say the error text is misleading about exactly that, and the `feed=opra` refusal (S3/R1)
persists on this account regardless.

The fix is one clamp, in `executor/iv_backfill.py:740`, and it already exists in this codebase
for equities. `run_backfill` already computes `equity_end = now − EQUITY_END_BACKOFF_MINUTES`
for the SIP boundary. `f2614da` fixed that for equities and left the **options** `end` at
end-of-today. Same bug, second site. Proposed shape (**not implemented — this run was
investigation only**):

```python
# Alpaca refuses options bars whose window ends within the last 15 minutes on the free
# tier, and refuses them with `OPRA agreement is not signed`, which is not what that
# means. Measured 2026-10-03: end=today 23:59:59Z → 403; end=now−20min → 200, same
# request, same second. https://forum.alpaca.markets/t/error-opra-agreement-is-not-signed/18445
options_end = min(
    end_of_day_utc,
    datetime.now(UTC) - timedelta(minutes=OPTIONS_END_BACKOFF_MINUTES),
)
```

Three follow-ups that should land with it:

1. **Stop treating this 403 as retryable.** `OPRA agreement is not signed` on a bars request is
   a permanent refusal for that window; four 180 s sleeps before surfacing it is the worst of
   both worlds. Classify it as terminal so a real entitlement failure fails in one call.
2. **Correct the record in the docs.** `docs/reviews/2026-10-03-iv-backfill-depth-probe.md`'s
   addendum and the "blocking finding" section of `2026-10-03-iv-backfill-run-log.md` both
   record the throttle as *measured*. It was not. The measured timeline in
   `scratch/opra_probe_timeline.jsonl` supersedes it. Leaving them would make the next person
   defend a throttle that does not exist. **Not edited by this run** — one findings doc was
   the write budget.
3. **Prefer the last completed session over a clock clamp.** IV rank is a percentile over
   *closed* sessions; today's partial session is not wanted regardless. Clamping to the last
   complete session is more correct than clamping to `now − 15min`, and it happens to also
   satisfy Alpaca's boundary.

### On alternatives to Alpaca for SPY option EOD data

Not needed — do not go shopping. Alpaca's Basic tier already serves everything this backfill
reads (indicative, OPRA-derived, daily OHLCV, 100 symbols/request, ~2 years deep, measured at
S5/M1), for free, the moment the request is well-formed. Other sources (CBOE DataShop,
OPRA proprietary feeds, ThetaData, Polygon) are paid and would add a vendor, a schema, and a
second provenance story to fix a bug that is one line long.

## Reproduction

```
PROBE_MODE=matrix      python3.12 scratch/opra_probe.py   # 5  — endpoint matrix
PROBE_MODE=burst       BURST_N=15 python3.12 scratch/opra_probe.py   # 15 — burst test
PROBE_MODE=shape       python3.12 scratch/opra_probe.py   # 5  — route shapes + control
PROBE_MODE=repro       python3.12 scratch/opra_probe.py   # 1  — 403 reproducibility
PROBE_MODE=rootcause   python3.12 scratch/opra_probe.py   # 2  — the A/B
python3.12 scratch/opra_ladder.py                        # offline, 0 API calls
```

`scratch/opra_probe.py` refuses call 26+ by design. Research scratch; **no production code was
modified by this investigation.**