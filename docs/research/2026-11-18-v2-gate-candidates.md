# v2 Gate Candidates — base-rate measurement (pre-declared)

rulebook `1.4.0-draft+071095af7402` · arm C · 5724 daily bars (2004-10-20 .. 2026-10-02) · warmup 201 bars

Candidate gate structures scored by **recombining the per-session statuses** the frozen `executor.checklist.evaluate` already produced — no rulebook or config edit. T4 re-thresholds (1.2 / 1.0 vs the frozen 1.5) are applied only inside this recomposition and mirrored from `checklist._t4` exactly; on daily bars the intraday run-rate branch is unmeasurable, so only the relative-volume branch can carry a PASS.

**Verification:** OK — 12 sampled sessions, 48 evaluations, 0 mismatches

## Sanity check: V1 must equal V0

**FAILED — reported loudly.** Dropping T1/T2/T5 from the AND changed the fire set. This is a genuine finding, not a harness bug: the October review's *"T1, T2, T5 are redundant — removing any of them changes nothing"* is a **leave-one-out** statement, and it does not extend to dropping all three together, which is what V1 does.

**Why the premise fails.** Each of T1/T2/T5 has zero *leave-one-out* sole-vetoes: on no session is exactly one of them the sole blocker while the other two still pass. But their vetoes **overlap** — on some sessions more than one of them fails at once. In a leave-one-out the other two dropped gates are still counted as "the other gates," so a session where two of them fail is not a sole-veto for either. In V1 all three are removed, so those overlapping-veto sessions are exposed. The delta below decomposes the V1\V0 sessions by which dropped gate(s) fail; multi-gate failure keys are the signature of the overlap.

Mismatch per (basis, direction):

- plan/call: V0 fired 2, V1 fired 4 — dropping T1/T2/T5 changed the fire set
- plan/put: V0 fired 3, V1 fired 41 — dropping T1/T2/T5 changed the fire set
- plan/either: V0 fired 5, V1 fired 45 — dropping T1/T2/T5 changed the fire set
- eod/call: V0 fired 2, V1 fired 4 — dropping T1/T2/T5 changed the fire set
- eod/put: V0 fired 3, V1 fired 41 — dropping T1/T2/T5 changed the fire set
- eod/either: V0 fired 5, V1 fired 45 — dropping T1/T2/T5 changed the fire set

`plan` basis — V1\V0 delta, by which dropped gate(s) fail:

| Direction | V1\V0 sessions | T1 only | T2 only | T5 only | T1+T2 | T1+T5 | T2+T5 | T1+T2+T5 |
|---|---|---|---|---|---|---|---|---|
| call | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |
| put | 38 | 12 | 0 | 2 | 6 | 15 | 0 | 3 |

`eod` basis — V1\V0 delta, by which dropped gate(s) fail:

| Direction | V1\V0 sessions | T1 only | T2 only | T5 only | T1+T2 | T1+T5 | T2+T5 | T1+T2+T5 |
|---|---|---|---|---|---|---|---|---|
| call | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |
| put | 38 | 5 | 0 | 2 | 6 | 22 | 0 | 3 |

**Reading.** Call-direction delta is tiny (T1/T2/T5's vetoes barely overlap), but put-direction is large and **T1-dominated**: T1 (close > 50EMA > 200EMA) is far more binding on puts than calls, so it vetoes many put sessions where T2b/T3/T4 pass. The practical consequence: V1 is not a neutral "remove the redundant gates" move — it loosens the put side far more than the call side, mostly by releasing T1.

## Cross-check (V1 vs harness `joint_rate_excluding`)


### Basis: `plan`

| Variant | Direction | Fires | Joint rate | Days/yr | Trades/60-session window | P(>=1 in window)¹ |
|---|---|---|---|---|---|---|
| V0 · call | 2 / 5522 | 0.0362% | 0.091 | 0.0217 | 2.1501% |
| V1 · call | 4 / 5522 | 0.0724% | 0.183 | 0.0435 | 4.2547% |
| V2 · call | 12 / 5522 | 0.2173% | 0.548 | 0.1304 | 12.2369% |
| V3 · call | 44 / 5522 | 0.7968% | 2.008 | 0.4781 | 38.1218% |
| V4 · call | 143 / 5522 | 2.5896% | 6.526 | 1.5538 | 79.2839% |
| V5 · call | 360 / 5522 | 6.5194% | 16.429 | 3.9116 | 98.2490% |
| V0 · put | 3 / 5522 | 0.0543% | 0.137 | 0.0326 | 3.2080% |
| V1 · put | 41 / 5522 | 0.7425% | 1.871 | 0.4455 | 36.0554% |
| V2 · put | 87 / 5522 | 1.5755% | 3.970 | 0.9453 | 61.4354% |
| V3 · put | 121 / 5522 | 2.1912% | 5.522 | 1.3147 | 73.5353% |
| V4 · put | 348 / 5522 | 6.3021% | 15.881 | 3.7812 | 97.9872% |
| V5 · put | 753 / 5522 | 13.6364% | 34.364 | 8.1818 | 99.9849% |
| V0 · either | 5 / 5522 | 0.0905% | 0.228 | 0.0543 | 5.2902% |
| V1 · either | 45 / 5522 | 0.8149% | 2.054 | 0.4890 | 38.7959% |
| V2 · either | 99 / 5522 | 1.7928% | 4.518 | 1.0757 | 66.2253% |
| V3 · either | 165 / 5522 | 2.9880% | 7.530 | 1.7928 | 83.8000% |
| V4 · either | 429 / 5522 | 7.7689% | 19.578 | 4.6614 | 99.2190% |
| V5 · either | 953 / 5522 | 17.2582% | 43.491 | 10.3549 | 99.9988% |

- V1/call: recomposition fired 4 vs `joint_rate_excluding` 4 — agree.
- V1/put: recomposition fired 41 vs `joint_rate_excluding` 41 — agree.

¹ independence approximation — see limitations.

### Basis: `eod`

| Variant | Direction | Fires | Joint rate | Days/yr | Trades/60-session window | P(>=1 in window)¹ |
|---|---|---|---|---|---|---|
| V0 · call | 2 / 5523 | 0.0362% | 0.091 | 0.0217 | 2.1497% |
| V1 · call | 4 / 5523 | 0.0724% | 0.183 | 0.0435 | 4.2539% |
| V2 · call | 12 / 5523 | 0.2173% | 0.548 | 0.1304 | 12.2349% |
| V3 · call | 44 / 5523 | 0.7967% | 2.008 | 0.4780 | 38.1164% |
| V4 · call | 143 / 5523 | 2.5892% | 6.525 | 1.5535 | 79.2779% |
| V5 · call | 360 / 5523 | 6.5182% | 16.426 | 3.9109 | 98.2476% |
| V0 · put | 3 / 5523 | 0.0543% | 0.137 | 0.0326 | 3.2074% |
| V1 · put | 41 / 5523 | 0.7424% | 1.871 | 0.4454 | 36.0502% |
| V2 · put | 87 / 5523 | 1.5752% | 3.970 | 0.9451 | 61.4287% |
| V3 · put | 121 / 5523 | 2.1908% | 5.521 | 1.3145 | 73.5289% |
| V4 · put | 348 / 5523 | 6.3009% | 15.878 | 3.7806 | 97.9857% |
| V5 · put | 753 / 5523 | 13.6339% | 34.357 | 8.1803 | 99.9848% |
| V0 · either | 5 / 5523 | 0.0905% | 0.228 | 0.0543 | 5.2893% |
| V1 · either | 45 / 5523 | 0.8148% | 2.053 | 0.4889 | 38.7905% |
| V2 · either | 99 / 5523 | 1.7925% | 4.517 | 1.0755 | 66.2186% |
| V3 · either | 165 / 5523 | 2.9875% | 7.529 | 1.7925 | 83.7946% |
| V4 · either | 429 / 5523 | 7.7675% | 19.574 | 4.6605 | 99.2183% |
| V5 · either | 953 / 5523 | 17.2551% | 43.483 | 10.3531 | 99.9988% |

- V1/call: recomposition fired 4 vs `joint_rate_excluding` 4 — agree.
- V1/put: recomposition fired 41 vs `joint_rate_excluding` 41 — agree.

¹ independence approximation — see limitations.

## How to read the candidates

- **V1** isolates the three *nominally* binding gates (T2b / T3 / T4) by dropping T1/T2/T5. It is **not** identical to V0 (see the sanity section): dropping the three together releases overlapping vetoes, dominated by T1 on the put side. Read V1 as "the checklist if the trend/RSI/IV gates were removed" — a materially looser put gate, not a no-op.
- **V2 / V3** loosen T4's relative-volume floor to 1.2 / 1.0. Because T4 is the dominant constraint, these move the joint rate the most of any candidate.
- **V4** relaxes the T2b/T3 confluence from AND to OR, which is a materially looser technical requirement; **V5** stacks the 1.2 T4 floor on top.

## Limitations (carried from `2026-10-03-gate-base-rates.md`)

1. T5's `iv_rank` is a Cboe VIX percentile-rank proxy, not SPY option IV; no free point-in-time historical SPY option IV exists. T5's true pass rate could differ by several percentage points in either direction.
2. The signal feed is Yahoo Finance daily bars, not the production Alpaca SIP feed. Yahoo reports consolidated volume; Alpaca's free tier is IEX-only. T4 is a volume gate, so its measured pass rate is likely an overestimate of what production will see — and the candidate T4 re-thresholds inherit that same volume uncertainty.
3. Daily bars cannot resolve anything intraday; the T2b fresh-cross guard is effectively inert at daily resolution (consecutive bars are exactly 24h apart).
4. T5's event-day leg is not replayed (the calendar covers 2026-Q4 only); it is carried as a separate ~11% (28/252, FOMC + CPI) session-count haircut, not folded into the measured rates.
5. Independence is assumed in the P(>=1 in window) figures; the gates are regime-dependent and real runs will cluster, so the figure is generous at this p.

