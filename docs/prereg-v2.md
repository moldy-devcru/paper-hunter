# Pre-registration v2 — RATIFIED 2026-11-18

**Status:** RATIFIED. Operator mads approved the draft as written on 2026-11-18
23:26 ET via Discord #paper-hunter (message `1572830369256833066`: "OK. I
approve."). Drafted 2026-11-18 by the lead (Moldy) under mads's ruling of
2026-11-18 23:16 ET: *"I'll go with your gut. V5 it is."* — selecting variant V5
from the measured candidates in
[`docs/research/2026-11-18-v2-gate-candidates.md`](research/2026-11-18-v2-gate-candidates.md)
(commit `964a941`, branch `v2-gate-candidates`). Every number below is a
pre-results measurement or expectation; nothing was edited after approval.

**Lineage.** v1 (`docs/ratification.md`) stays the record of the as-is window:
its (e)/(f) signatures are still pending, and its soak/data collection continues
regardless of v2 (the mechanism layer is window-agnostic). On signature, v2
**supersedes v1's trading scope**: one window opens, arms per §2 below, arm A
scored once. Operator note: if you want v1's A-position to open before v2 is
signed, say so at signature; the default is one window, v2's.

---

## 1. Gate specification (V5, as measured)

Blocking checklist for arms B and C becomes:

> **T4 AND (T2b OR T3-group)**

- **T4** `relative_volume_min: 1.2` (v1: 1.5). `intraday_run_rate_min: 1.5`
  unchanged. Lookback 20d unchanged.
- **T2b** unchanged (MACD line vs signal, contracting-histogram veto,
  fresh-cross guard 24h with the T3 whipsaw exception — the guard stays and
  WILL bind intraday, unlike in the daily-bar measurement).
- **T3-group** unchanged (`squeeze_release OR band_rejection`).
- **T1, T2, T5: removed from the blocking AND.** Still computed and journaled
  as observations on every plan. T5's IV series keeps accumulating (it is
  mechanism evidence, and a future cycle may re-admit it).

**Declared consequence — counter-trend entries are now permitted.** v1's T1
carried `counter_trend.allowed: false`. The measured V5 put-side frequency
(+38 fires vs V0 over 22.7 years) is predominantly counter-trend puts released
by dropping T1. v2 therefore explicitly retires with-trend-only. This is a
philosophy change, not a side effect, and it is written here so it cannot be
discovered later in a journal.

**Arm B activation gate retired.** `window.arm_b_gate` existed only because
T5 was blocking and could not warm for 0-DTE. With T5 non-blocking, arm B
opens at window start. T6 remains arm B's extra blocking gate (§3).

## 2. Arms and window

- Arms **A + B + C**, $10k paper each, same window.
- Window: 3 months / ~60 sessions, extension by monthly review only.
- Start: first trading day after signature AND the §6 ops checklist is green.
- Strategy version: `2.0.0-draft` while unsigned; `2.0.0` frozen at signature,
  content hash stamped on every journal row, pre-registration pushed as a
  public dated tag.

## 3. T6 freeze — re-specification, declared pre-results

v1 ruled N = P90 of the first 20 soak sessions. That proposal's instability
materialized: first-20 P90 = **16.9× call / 2.47× put** (July spikiness), and
the call number passes only 4 of 94 recorded days vs the ~10% design intent.

**v2 re-specifies the calibration window to ALL recorded sessions at a single
deep-OTM threshold** (94 as of 2026-11-18, all at the working 3%), and freezes:

- **N_call = 5.59×**, **N_put = 3.07×** (P90, linear interpolation, all 94).
- **deep_otm_threshold_pct = 3.0** frozen (calls and puts).
- Observed pass rates on the 94-session record: call 11/94 (11.7%), put 17/94
  (18.1%). Consistent with the ~10% design intent.

This is a new-cycle design change made before any v2 results exist, which is
the only legitimate moment for it. The v1 formula is not silently edited; it
is superseded, with the reason recorded.

## 4. Measured expectations (pre-results)

From the candidate report, plan basis, either direction, 5,522 sessions:

- **Arm C: ~10.3 trades per 60-session window** (V5 fires on 17.3% of
  sessions; P(≥1 in window) ≈ 100% under the independence approximation).
- **Arm B: ~1–2 trades per window** (C's rate × T6's ~12–18% pass). The
  gambler is observable but still thinly sampled; NO-SHOT counterfactuals
  carry the rest of the flow evidence.
- **Arm A: always invested**, the bar B and C must clear.

Caveats that travel with every number: Yahoo consolidated volume overstates
what the IEX-only production feed will show (live frequency runs LOWER, T4 is
a volume gate); daily bars cannot resolve intraday behavior; event-veto
haircut ~11% not folded in; gate fires cluster by regime, so the independence
approximation is generous.

## 5. Predictions (re-scored for v2, criteria unchanged where carried)

1. **P1 — arm A finishes the window positive vs cash.** Scored as in v1;
   ~95% confidence of an answer existing. Unchanged.
2. **P2 — arm B is structurally negative-EV as an asset class.** Now
   PARTIALLY evaluable: 1–2 expected trades yield directional evidence, not
   a powered test. Recorded as directional; the criterion is unchanged and
   the definitive test stays deferred until B has ≥20 trades.
3. **P3 — arm C tracks arm A with leverage-amplified variance.** First real
   evidence possible (~10 expected trades). Exploratory at this sample size;
   a variance claim needs the rollup's per-trade marks, and one lucky window
   is not confirmation.
4. **P4 — trade frequency under hunting discipline.** v2 redefines the
   discipline bar: pre-registered expectation ~3–4 C trades/month.
   **Falsifier: >12 C trades/month (weekly trading) means the rules were not
   frozen tight enough.** Unlike v1, this test has teeth: the expected
   outcome is non-zero, so a quiet window is now informative too.

## 6. Pre-commitments (carried from v1 §(e), in force at signature)

- An empty or quiet window is **INCONCLUSIVE** — not a bug, not evidence for
  loosening. No gate or threshold changes mid-window. Extension and
  re-calibration are monthly-review decisions made on schedule, not on
  results.
- Gate frequency is not edge. V5's thresholds were chosen for observability
  from a pre-declared candidate set, measured before any results, and frozen
  here. The looseness is the experiment, not a defense of it.
- v1's monk-like-discipline framing is retired by operator choice; v2 tests a
  looser confluence strategy and says so.
- Ops before first trade (v1 §(d) carried): paper account confirmed paper,
  kill switch exercised once on paper, CPI/FOMC calendar verified and
  coverage checked, full dry run into a scratch DB with the journal inspected,
  loader prints `pending_calibrations == []` under the v2 config.

**Operator signature:** mads — ratified via Discord #paper-hunter, 2026-11-18
23:26 ET, message `1572830369256833066` ("OK. I approve."). A dated, attributable
operator approval is the record; no wet signature required.  **Date:** 2026-11-18

**Lead (Moldy):** drafted 2026-11-18, stands behind every row above.

### Items covered by the ratification (all approved 2026-11-18 23:26 ET)

1. Counter-trend entries permitted (§1) — the philosophy change.
2. T6 re-specification + freeze numbers (§3).
3. One window, v2 scope, supersedes v1's trading scope (§ lineage note).
4. Predictions wording (§5), especially P4's new falsifier.
