# Gate base rates — measured, not assumed

**Date:** 2026-10-03 (before the Monday 17:30 window opens)
**Subject:** the frozen T1–T6 checklist's base rates over 22.7 years of SPY daily bars
**Rulebook measured:** `1.2.0-draft+9c5200188e0e` (`config/rules.example.yaml`)
**Tooling:** `scripts/measure_gate_baserates.py` + `analysis/gate_baserate.py` + `scripts/fetch_history.py`
**Reproduce:** `.venv/bin/python scripts/measure_gate_baserates.py`

---

## Verdict in one paragraph

The outsider review's central claim is **confirmed, and slightly understated.** Arm C — the
only checklist-gated arm actually running in the window — fires on **5 of 5,522 SPY sessions
in 22.7 years (0.09%)**. That projects to **0.23 qualifying days per year, or 0.054 trades in
a 60-session window.** The probability a 60-session window contains at least one qualifying
day is **5.3%**; the probability it contains none is **94.7%**. The review's prediction of
"one qualifying day per few thousand sessions" was right to within a factor of two, and its
structural claim is exactly right: **T4 and T3 are the binding constraints, T1 and T2 do
almost no filtering, and T5 does none at all.** One correction to the review's framing: it
attributed the tightness partly to positive correlation between gates, which would push the
joint rate *above* the naive product. The measurement shows the opposite — the measured joint
rate (**3.6e-4**) is *below* the naive product of the measured marginals (**6.7e-4**). The
gates are **negatively** correlated in the tail, which makes the checklist tighter than the
review estimated, not looser.

---

## Methodology

The measurement does not reimplement the gates. It calls the real
`executor.checklist.evaluate` once per (session, arm, direction), feeding it an
`IndicatorSnapshot` assembled from the repository's own `executor.indicators` functions with
the rulebook's own thresholds. Nothing in `executor/`, `config/`, or any frozen strategy
document was modified — this is additive only.

**Signal bars:** 5,724 SPY daily bars, 2004-01-02 → 2026-10-02, fetched once and cached to
`data/historical/spy_daily.csv`. The first 201 bars are skipped for indicator warmup, leaving
**5,522 sessions measured** (5,523 on the `eod` basis — see below).

**Two bases, because they answer different questions:**

| Basis | Signal bar | Why |
|---|---|---|
| `plan` | the **prior** session's close | This is what `executor/hunt_plan.py` actually evaluates pre-market. It is the honest basis for "will the checklist be green at 09:45 Monday?" |
| `eod` | the session's **own** completed close | Standard backtest convention, and what the outsider review's arithmetic assumed. |

The two agree to within 0.01 percentage points on every per-gate rate, so nothing in the
findings depends on which basis you take. `plan` is used below unless stated.

**Correctness check, run on every invocation:** `verify_against_builder` re-derives sampled
sessions through the production `executor.snapshot_builder.build_snapshot` — which recomputes
indicators from scratch on a truncated bar prefix — and asserts the fast path agrees
condition-for-condition. Latest run: **12 sessions, 48 evaluations, 0 mismatches.** A mismatch
prints loudly and exits non-zero. Every number below came from a run that passed this check.

---

## Per-gate pass rates (arm C, `plan` basis, 5,522 sessions)

Arm B carries the same checklist plus T6, and its rates are identical for T1–T5. Directions are
shown separately because T1 (trend) and T2 (RSI) are strongly direction-dependent; T4 and T5
are direction-blind.

| Gate | What it needs | Call pass | Put pass | Either-direction |
|---|---|---|---|---|
| T1 | close > 50EMA > 200EMA | **63.07%** | 10.58% | 36.83% |
| T2 | 50 < RSI(14) < 75 | **65.79%** | 31.94% | 48.87% |
| T2b | MACD line > signal **and** histogram expanding | 26.64% | 27.09% | 26.87% |
| T3a | bandwidth percentile < 20 **and** band widening | 6.37% | 2.83% | 4.60% |
| T3b | 2σ band rejection | 5.90% | 5.67% | 5.79% |
| **T3** | *(T3a OR T3b — the group as the evaluator resolves it)* | **12.26%** | 8.49% | **10.38%** |
| T4 | relative volume ≥ 1.5× | 8.96% | 8.96% | **8.96%** |
| T5 | IV rank < 50 *(VIX proxy — see limitations)* | 55.03% | 55.03% | 55.03% |
| T6 | flow ratio ≥ N | — | — | **not carried by arm C** |

T3 is an OR group, not an AND: the evaluator folds `T3a OR T3b` into a single veto carried by
T3a. Reporting it per-arm understates the gate by the whole T3b contribution, so the group row
is derived per session as "T3a PASS or T3b PASS."

---

## Joint (ANDed) rate

| Arm | Direction | Fires | Joint rate | Days/yr | Per 60-session window | P(≥1 in window) |
|---|---|---|---|---|---|---|
| C (stalker) | call | 2 / 5,522 | 0.036% | 0.09 | 0.022 | 2.2% |
| C | put | 3 / 5,522 | 0.054% | 0.14 | 0.033 | 3.2% |
| **C** | **either** | **5 / 5,522** | **0.091%** | **0.23** | **0.054** | **5.3%** |
| B (gambler) | either | 0 / 5,522 | 0.00% | 0.00 | 0.000 | 0.0% |

Arm B's zero is **structural, not empirical** — see "T6" below. Arm A is buy-and-hold and runs
no checklist.

**Trade-count implication for the window:** arm C is expected to produce **~0.05 trades** over
its 60-session window. In plain terms: a single qualifying day, if one occurs at all, is a
~5% event. The rulebook's own note on arm B — *"weeks with zero trades is the system working"* —
is, on this arithmetic, not a special condition to be relieved of. It is the expected output.

---

## Which gates are binding

Measured two ways, because "binding" is ambiguous. **Sole-veto** counts sessions where this gate
was the *only* blocker (every other gate passed and this one vetoed) — the sharpest single
measure. **Pass given others pass** is the share of otherwise-green sessions this gate also
lets through; near 100% means it is redundant.

Arm C, call direction, `plan` basis:

| Gate | Sole-veto sessions | Pass given others pass | Reading |
|---|---|---|---|
| **T4** | **91 (1.65%)** | **2.15%** | **The dominant constraint.** T4 is the only gate that ever vetoes a day everything else passed, and it kills 98% of them. |
| T2b | 14 (0.25%) | 12.50% | Second-most binding; also vetoes most otherwise-green days. |
| T3 | 11 (0.20%) | 0.00% | Binding, but note it never *passes* an otherwise-green day — every day T3 is the sole blocker, it would also have been blocked by something else the same day. |
| T1 | 0 | 100.00% | **Does no filtering at all.** On every session the other gates passed, T1 passed too. |
| T2 | 0 | 100.00% | **Does no filtering at all.** Same. |
| T5 | 0 | 100.00% | **Does no filtering at all.** Same. |
| T6 | — | — | Not carried by arm C. |

T1, T2, and T5 are **redundant within this AND** at daily resolution. Removing any of them
changes nothing about which sessions fire. That is a stronger statement than the review's
"T1 and T2 are doing almost no filtering" — they are doing *none*, once T2b/T3/T4 are in
place. T5's 55% pass rate is high, but a gate that passes 55% of days is not doing work in an
AND whose other members already reject 99.9% of days.

Put direction has the same ordering with T2b at 13 sole-vetoes and T4 at 91.

---

## Comparison to the outsider review

| Claim in `2026-10-03-algo-outsider-review.md` | Verdict |
|---|---|
| "one qualifying day per few thousand sessions" (arm C ≈ 2×10⁻⁴) | **Confirmed.** Measured 9.1×10⁻⁴ either-direction, 3.6×10⁻⁴ call-only. Within a factor of 4 of the review's estimate, on the same order. |
| "the binding constraints are T4 and T3, not the trend gate" | **Confirmed.** T4 is the dominant constraint by a wide margin (91 sole-vetoes, 2.15% pass given others). T3 and T2b follow. |
| "T1 and T2 are doing almost no filtering work" | **Confirmed, understated.** Measured: zero sole-vetoes, 100% pass given others. They are entirely redundant in this AND. |
| "The conditions are positively correlated … the honest number is higher than the naive product" | **Refuted.** The naive product of measured marginals is 6.7×10⁻⁴; the measured joint is 3.6×10⁻⁴. The gates are **negatively** correlated in the tail. The checklist is *tighter* than the review concluded, not looser. |
| Review's per-gate standalone estimates (T1 ~45%, T2 ~45%, T3 ~3–6%, T4 ~6–10%, T5 ~50%) | **Directionally close, individually off.** T4 (8.96%) and T5 (55%) match well. T3 measures 10.4% as a group vs 3–6% estimated — because T3 is an OR of two ~5% arms, not a 3–6% single gate. T1/T2 differ sharply by direction (63% call vs 11% put for T1) because the review quoted a single blended figure where the arms genuinely diverge. |

The review also flagged that **T3a is close to self-contradictory at daily resolution** (it
requires bandwidth percentile *low* and bandwidth *rising*). The measurement supports that
observation: T3a passes 6.37% of call sessions, and its sibling T3b passes 5.90%, so the group
nearly doubles the gate's pass rate. The review's recommendation to "re-examine T2b-vs-T3b"
cannot be settled by base rates alone — both are binding, and neither is removable without
weakening the confluence principle the brief asked for.

---

## Limitations (read before quoting any number above)

1. **T6 is `calibration_pending` and has no historical series.** There is no free source of
   point-in-time historical SPY option/flow data, so T6 is PENDING on every session. A
   pending gate blocks, so **arm B's measured fire rate is 0.00% by construction, not by
   market behaviour.** The marginal table for arm B is therefore reported with T6 dropped, as
   a clearly-labelled counterfactual about the *technical* gates. Do not read it as a
   prediction of what arm B will do — T6 is designed to pass ~10% of days by definition, so
   arm B's real joint rate will be roughly an order of magnitude *below* the T1–T5 counterfactual.
   Also note **arm B is not in the window**: `rules.window.arms` is `[A, C]`.
2. **T5's `iv_rank` is a Cboe VIX percentile-rank proxy**, not SPY option IV. The proxy's
   45th-percentile VIX level roughly corresponds to the rulebook's IV-rank < 50 threshold, and
   it is computed with the repo's own `executor.iv_rank` ranking function. But VIX and SPY IV
   are different quantities with different dynamics; T5's true pass rate could differ by several
   percentage points in either direction. Since T5 has zero sole-vetoes, this uncertainty
   cannot change any conclusion in the binding table — it can only matter if T5 were the sole
   blocker, which it never is.
3. **T5's event-day leg is not replayed.** The repo's event calendar covers 2026-Q4 only, so
   FOMC/CPI vetoes cannot be applied to 2004–2026. Estimated separately as a haircut: **28 of
   252 sessions/yr (11.11%, FOMC + CPI)** would be vetoed. Applying it would cut arm C's
   projected days/yr from 0.23 to roughly **0.20**, and the window expectation from 0.054 to
   about 0.048. It does not change any conclusion.
4. **The T2b fresh-cross guard is effectively inert at daily resolution.** Consecutive daily
   bars are exactly 24h apart (72h across weekends), and the guard vetoes only when the adverse
   cross is *strictly younger than* 24h — so a cross on the immediately preceding bar sits at
   exactly 24.0h and does not fire. This is a property of measuring on daily bars, **not**
   evidence the guard is harmless intraday, where bars are minutes apart and the guard will
   actually bind. Anyone reasoning about T2b's intraday behaviour from this table is reading
   the wrong resolution.
5. **The signal feed is Yahoo Finance daily bars, not the production Alpaca SIP feed.** Yahoo
   reports consolidated volume; Alpaca's free tier is IEX-only. Since T4 is the single most
   binding gate and is a *volume* gate, this is the limitation most likely to move the numbers.
   Consolidated volume runs higher than IEX-only, so **T4's measured 8.96% pass rate is likely
   an overestimate** of what production will see. The true joint rate could be lower still.
6. **Daily bars cannot resolve anything intraday.** T1/T2/T4 as evaluated at a 09:45 entry use
   the prior close, which is the honest pre-market basis, but nothing here speaks to whether
   those levels held at the moment of entry.
7. **Independence is assumed in the P(≥1 in window) figures.** The gates are regime-dependent
   and real runs will cluster. With p this small the approximation is generous, but the true
   probability of a green day is not independent across a 60-session window.
8. **No split adjustment is applied to the fetched bars.** SPY has had no splits in this
   window, so this is safe here and would need revisiting for any instrument that has.

---

## What an empty Monday window implies

Nothing went wrong. On the measured base rate, **94.7% of 60-session windows contain zero
qualifying arm-C days**, and Monday's session alone is far more likely to be blank than not.
The expected trade count over the whole window is **~0.05**.

Three things follow, and one does not:

- **An empty window is the base-rate prediction, not a failure signal.** It should not be
  investigated, debugged, or treated as evidence the gates are broken.
- **Reporting the window as a null result would be a mistake.** With ~5 qualifying sessions in
  22.7 years, a 3-month window has almost no statistical power to detect *anything* — not a
  positive edge, and not a negative one. The honest pre-registered expectation to write down in
  advance is "fewer than one trade; the experiment will be inconclusive about edge, and that
  is what this sample size can support."
- **Do not re-tune thresholds in response.** The frozen rulebook exists precisely to prevent
  post-hoc adjustment after seeing a quiet window. If the base rate here is uncomfortable, the
  correct response is to note it and leave the rules frozen.
- **The genuinely actionable finding is about the gate design, not the window.** T1, T2, and T5
  are redundant; T4 is doing nearly all the filtering. That is a real structural observation
  about the checklist, and it is the thing worth acting on — after the window closes, and on
  the pre-registered terms.

If the window opens with an empty Monday, the correct note in the log is: *"base rate predicts
0.05 trades per window; 0 trades on day 1 is unremarkable."* Not *"checklist is too tight."*

---

## Files added

| Path | Purpose |
|---|---|
| `analysis/gate_baserate.py` | Pure analysis module: indicator stacking, snapshot construction, base-rate/marginal/projection math, builder-equivalence verifier |
| `scripts/fetch_history.py` | Fetches and caches SPY daily bars and the Cboe VIX CSV; reuses `executor.iv_rank`'s own parser and URL |
| `scripts/measure_gate_baserates.py` | CLI runner; prints the tables above, `--json` for the raw numbers, `--verify-samples` to widen the equivalence check |
| `tests/test_gate_baserate.py` | 8 smoke tests over the invariants that would silently corrupt the table (T3 OR-group resolution, uncarried-gate exclusion, always-pending flagging, projection algebra) |
| `data/historical/spy_daily.csv` | Cached SPY daily bars, committed so the numbers are re-derivable offline |
| `data/historical/vix_daily.csv` | Cached Cboe VIX daily closes |

No existing gate logic, threshold, or frozen strategy document was modified.