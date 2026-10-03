# T5 IV backfill — build log and run outcome (run B, 2026-10-03)

Branch `feature/frozen-rule-rulings`. Base: run A's tip `50e2c0f`.

## What was built

| commit | what |
|---|---|
| `b2b7799` | live depth probe of `/v1beta1/options/bars` (from run A) |
| `a55deef` | `executor/black_scholes.py` — European pricer + IV solver, stdlib only |
| `81f918d` | R9 provenance: `source="bars_bs_inversion"`, `origin="live"\|"backfill"` |
| `c2da92b` | `executor/iv_backfill.py` + `scripts/backfill_iv_rank.py` + 23 tests |
| `f2614da` | throttle pacing + bounded 403 cooldown, + 5 tests |
| `e2b0b68` | ladder sized to the gate (50 chunks → 15), empty-ladder refusal |

The module is `executor/iv_backfill.py`; the CLI is `scripts/backfill_iv_rank.py`
(`--dry-run`, `--days`, `--arms`, `--range-pct`, `--strike-step`, `--report`).

Design decisions that are load-bearing, all recorded as `# INTERPRETATION:` markers in the
code and regenerated into `docs/ratification.md` §(a):

* **Contract reconstruction is an inference, written down as one.** No point-in-time chain
  exists, so for each session the pipeline picks the in-band expiry nearest the band's
  lower edge and the enumerated strike nearest spot *among those that have a bar*. Both
  appear in the run report.
* **Both rights are recorded.** The gate filters by direction at read time, so recording
  one side would leave half its reads in permanent warmup for nothing.
* **Two keys per observation** — the ruled moneyness rolling key that T5 reads, plus the
  expiry-keyed `$5` audit key, so the approximation stays measurable against the exact
  thing.
* **Risk-free 0.0425, dividend yield 0.013**, both fixed and documented. The solver refuses
  zero DTE, non-finite or non-positive prices, no-arbitrage violations, insufficient
  volume, and collapsed vega rather than returning a number.

## Data feasibility (measured, not assumed)

From `docs/reviews/2026-10-03-iv-backfill-depth-probe.md`:

* Historical floor is **February 2024**; earlier symbols return `200` with an **empty**
  `bars` map, so the pipeline refuses a pre-floor window rather than recording those
  sessions as "traded nothing".
* Bars carry **OHLCV only** (`c,h,l,n,o,t,v,vw`). **No `impliedVolatility`** — which is
  exactly why a Black-Scholes inversion is required rather than a field read.
* OCC strikes are **8 digits**; 9 digits is a hard `400`.

## The blocking finding: the options-bars route is throttled, and lies about it

`403 OPRA agreement is not signed` arrives **in waves**. Roughly 10–15 requests succeed,
then every request 403s for ~2 minutes, then the burst allowance returns. The identical
request 403s twice, succeeds minutes later with nothing changed on our side, and 403s
again. The documented Basic budget is 200 calls/min; nothing about the observed behaviour
resembles that.

Consequently three runs were attempted and **none completed the fetch**:

| attempt | scope | chunks | outcome |
|---|---|---|---|
| 1 | full trailing year | 17 | aborted at ~11 min, store still empty |
| 2 | 120 days | 6 | aborted at ~8 min, store still empty |
| 3 | 100 days, ±0.5% ladder | 4 | aborted at ~7 min, store still empty |

**Zero observations were written.** `data/ivrank.db` is empty; the backfill is unrun.

Mitigation that *is* in the code: requests are paced to a minimum interval, a 403 cools
down 180 s (doubling) rather than the 5 s a 429 wants, retries are bounded at three, and
**both retry and cooldown counts are reported** so a run that leaned on the throttle is
visibly a worse run. Sizing the ladder to the gate cut the request count 3.4× on its own.

### Operational note worth flagging

Two self-inflicted factors made this worse and are recorded so they are not repeated:
(I killed the wrapper PID rather than the Python child, so **two orphaned runs kept
competing** for the same throttle while a third was launched; and the 403 cooldown was
initially 30 s, shorter than the measured ~2 min recovery). Both were found and corrected;
neither was an upstream fault.

## Can T5 arm C warm at all? — answered by arithmetic, not by hope

Since observation was impossible, the question was answered with the **shipped** key
builders (`scratch/iv_backfill_warmup_arithmetic.py`, no API calls):

```
--- frozen 1y lookback: 2025-10-03 .. 2026-10-03
    sessions=261  distinct keys=2
      mte91-call-mny+0.00       261  WARM
      mte91-put-mny+0.00        261  WARM
```

**Yes — arm C warms with a wide margin: 261 observations per key against
`MIN_OBSERVATIONS = 60`.**

The reason only two keys exist is the load-bearing finding: `select_expiry` picks the
**shortest-dated** expiry inside the band, so DTE pins near the band's lower edge (~91)
instead of sweeping 90–180. Every session therefore lands in **one** DTE bucket and adds
one observation to it. That is why a single key clears the floor, and why widening the
ladder buys nothing.

**Arm B cannot be backfilled, by construction.** Its frozen DTE band is `0`; Black-Scholes
vega is identically zero at expiry; a 0-DTE daily print does not determine a volatility and
the solver refuses rather than inventing one. Arm B stays in warmup and is filled only by
the live EOD poll. No code change alters this — it is a property of the instrument.

## Consequence for `docs/ratification.md` (e) and (f): NOT rewritten

Step 5 of the brief says to rewrite (e)/(f) **only if verification shows T5 warm**, and to
report the gap instead otherwise. Verification did not happen — the store is empty — so
**neither section was touched.** The arithmetic above says the rewrite is *probably*
warranted once the backfill lands, but "probably, from arithmetic" is exactly the kind of
claim this experiment exists to refuse. (e)'s structural-dead premise and (f)'s E1
prediction statuses stay as drafted, unsigned.

The one thing that *is* verified is the IV-warmup fact (e) rests on: **arm C's IV gate can
warm, and does so comfortably, once the backfill runs.** (e) therefore keeps its
no-mid-flight-loosening pre-commitment regardless — that survives any data outcome.

## Regression found and fixed

R9's migration broke three rehearsal tests. Cause: `_migrate_columns` issued a
`CREATE INDEX` that is a genuine write, where the pre-R9 `CREATE TABLE IF NOT EXISTS` was a
no-op on an existing table — so R9 turned "we could not add an index" into "the fixture
cannot be built", because the rehearsal and UI open the **deployed** store under
`/opt/paper-hunter`, owned by another service user. Schema creation and migration are now
best-effort, the skipped step is recorded on `store.migration_note` rather than swallowed,
and a regression test builds a deliberately legacy read-only store to pin it.

Full suite: **1125 passed**, ruff clean.