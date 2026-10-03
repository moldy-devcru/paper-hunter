# Full-cycle rehearsal — 2026-10-03

**Scope:** the whole daily cycle, offline, end to end, before the trading window opens in
November. **Verdict: NO-GO for arming the daily cron trio.** Every stage ran and every
mechanical path works; the blockers are data readiness and two unresolved mechanics
questions, not broken code.

Reproduce:

```bash
scripts/rehearse.sh                      # report to stdout, throwaway world
scripts/rehearse.sh --json /tmp/rep.json # machine-readable report
python -m executor.rehearsal --help
```

The harness is `executor/rehearsal.py` (15 stages, `tests/test_rehearsal.py`,
`scripts/rehearse.sh`). It runs against a **temp copy** of the journal, uses
`DryRunRouter` and `StaticWatchData` only, and installs a guard that makes
`socket.connect` raise. Three claims are *measured* rather than asserted, in the
integrity stage: the shipped rulebook's sha256 is unchanged across the run, the tree
under `/opt/paper-hunter` is unchanged in size and mtime, and zero network connection
attempts were made.

---

## 1. What ran

| # | Stage | What it exercised |
|---|---|---|
| 1 | `rules` | shipped rulebook: draft status, unfrozen gates, event-calendar veto kinds |
| 2 | `calibration` | T6 proposal from the **deployed** `flow_baseline` (copied read-only) |
| 3 | `simulated-freeze` | T6 filled into a **temp** rulebook so the shot branch can run |
| 4 | `fixture-world` | deterministic SPY bars, 0DTE + arm C chains, seeded IV store, journal |
| 5 | `iv-warmup` | deployed IV-store readiness audit + the fixture's warm store |
| 6 | `plan/noshot` | near-miss session: plan build + journal write |
| 7 | `plan/shot` | washout session: the entry-capable plan |
| 8 | `watch/quiet` | `run_once` ticks **and** the `run_loop` driver the intraday cron runs |
| 9 | `watch/stale` | trigger reached on a stale snapshot → entry refused |
| 10 | `watch/entry` | reclaim: full checklist re-verified live, entry routed (dry) |
| 11 | `exits+roll` | arm B profit take + hard close precedence; arm C roll ladder |
| 12 | `eod` | EMA streak, NO-SHOT ledger, EOD close decision — for both sessions |
| 13 | `soak` | `flow_baseline` + IV observation writes |
| 14 | `analysis` | shadow-roll open/mark, TA-vs-arm-C comparison, scorecard, histogram, weekly rollup |
| 15 | `integrity` | row counts, router state, rulebook sha, `/opt` tree, network attempts |

Both branches are exercised, and the tests pin that: a rehearsal that only ran the quiet
path would be worthless, so `tests/test_rehearsal.py` asserts the no-shot near miss, the
ledger row, the entry trade, the stale veto, both arm B exits and the arm C roll.

### The two fixture sessions

- **No-shot (2026-10-20).** A near miss: the reclaim already happened on the signal bar
  and every condition passes except **T4 (relative volume)** — 1 blocking failure, inside
  `noshot.DEFAULT_MARGINAL_LIMIT = 1`. Cells `B/call` and `C/call` are sightings. This is
  the case the ledger exists to measure; a rout (everything fails) would legitimately
  write nothing, so the near miss is the fixture that proves the path.
- **Shot (2026-10-19).** A washout signal bar (`-2.0%` close, 3% lower wick, 1.8x volume)
  followed intraday by a `+3.0%` reclaim at 1.8x volume. The pre-market plan is NO_TRADE
  on the washout close but carries the T1 level to watch; the watch loop re-verifies the
  whole checklist against the **live** series and the entry fires.

`# INTERPRETATION:` the live series for the shot session carries the session's own reclaim
bar rather than yesterday's bars. The checklist is re-run on whatever series the loop is
handed, so a loop fed the plan's own bars would re-verify a *yesterday* setup and could
never agree with a session that actually reclaimed.

---

## 2. T6 calibration: proposed N

From the deployed journal, copied read-only (64 rows, 63 backfill + 1 live, 2026-07-06 →
2026-10-02):

| | value |
|---|---|
| proposed **N** (pooled P90 of ratios) | **2.681** |
| call-side P90 | 1.157 |
| put-side P90 | 4.024 |
| sessions considered / qualifying | 64 / 38 |
| qualified side-samples | 76 |
| excluded side-samples | 52 (26 of 64 sessions affected) |
| ratios ≥ 5x baseline | 6 |
| largest ratio | call **61.21x** on 2026-09-16 |

**N is dimensionless and was computed as the ratified formula says**: `r_i` = trade-side
deep-OTM volume ÷ that session's trailing 20-session baseline mean, then linear-
interpolation P90 over the `r_i`. This matters because T6 compares
`today_volume >= N * baseline_mean` — an `N` taken from raw volume would be a count, not
a multiplier. The UI previously showed only the raw-volume P90; `executor/calibration.py`
now owns the ratio computation and the `/calibration` page shows both, with the raw line
clearly labelled a chart line.

### Recommendation: DO NOT FREEZE N

Reported as a proposal only; nothing was written to the rulebook. Reasons for the lead:

1. **52 of 76 side-samples excluded**, mostly short baselines. `flow_baseline` is
   append-only, so sessions written before the historical backfill carry an incomplete
   20-session baseline and cannot be scored.
2. **Six ratios ≥ 5x**, one at 61x, on the same store. That looks like chain-snapshot
   completeness rather than a market fact, and a P90 is exactly the statistic that moves
   when a handful of rows are wrong.
3. **Call/put asymmetry is large** (1.157 vs 4.024, 3.5x). `t6_flow.side` is
   `trade_direction`, so one multiplier is applied to whichever side is traded; the pooled
   P90 implicitly assumes the sides share a distribution. They plainly do not.

Deciding whether to use a per-side multiplier or a pooled one is mechanics, so it is left
to the lead rather than resolved here.

---

## 3. Bugs found

### Fixed (small, plumbing — no rulebook semantics changed)

1. **arm C could never select a contract** (`executor/watch_loop.py::_arm_c_contract`).
   The selector read `premium_pct_max`, but the plan writes
   `premium_pct_of_bankroll_max`, and **neither side carried the bankroll**. The cap
   evaluated to `$0` and `max_premium <= 0` was in the skip condition, so nothing
   complained: arm C's live entry path was silently dead. The plan now carries
   `bankroll_usd` and the selector reads the criteria dict's own keys for the premium
   cap, the DTE band and the delta floor — so the rulebook drives selection instead of
   literals repeated in two places (shipped values unchanged: 90–180 DTE, delta 0.80,
   50% of bankroll). Regression tests in `tests/test_watch_loop.py`.
2. **Arm C selection crashed** before it got that far: `cell.watch.arm_c_criteria` does
   not exist (only `to_dict()` spells it that way). AttributeError on the first arm C
   entry attempt of every session. Same tests cover it.
3. **IV rank and the flow gate were dropped between the CLI and the loop.**
   `cmd_watch` never passed `iv_rank`/`flow_gate`, and `run_loop` had no parameters for
   them, so every tick re-verified with `iv_rank=None` and `flow_gate=None` — T5 and T6
   could never resolve even in principle. Both are now plumbed end to end
   (`executor/main.py`, `executor/watch_loop.py`), and `tests/test_flow_gate.py` pins the
   forwarding.
4. **The calibration reader computed the wrong statistic** — raw-volume P90 for a
   quantity the rulebook defines as a ratio. Fixed in `executor/calibration.py`; the UI
   shows the raw line as a chart line and the proposed `N` separately.
5. **The T6 calibration reader/formula mismatch is now one implementation.**
   `executor/calibration.py` is used by the executor, the UI and this rehearsal.

### Documented, not fixed (design-level — operator/lead questions)

6. **Arm C has no roll executor.** `PositionManager.evaluate_exits(..., replacement=None)`
   refuses to roll, and the watch loop never supplies a replacement leg, so a position
   past its roll trigger produces a VETO-shaped note instead of a roll. The rehearsal
   exercises the ladder **directly** (with a replacement supplied) and shows it emits
   ROLL; the live path cannot. Choosing the replacement contract intraday is a trading
   mechanic — **operator question.**
7. **A green plan cannot be entered.** `compute_triggers` only emits levels for blocking
   conditions that have not passed, so a cell that passes every condition carries **no**
   actionable trigger, and the watch loop only considers cells with an armed trigger. A
   fully green pre-market plan is therefore a plan with nothing to watch, and the
   plan→entry interaction has never been exercised in production shape (the loop's own
   tests reach the entry path only with hand-built cells). Whether a green cell should arm
   the loop at all — on the entry window alone, or against a fixed trigger — is
   **mechanics: operator question.**
8. **`cmd_eod` omits `taken` and `decision_ids`.** Without `taken`, a session that both
   saw the setup *and* traded still writes a NO-SHOT row worded as a skip; the rehearsal
   demonstrates it (2 rows without `taken`, 1 correct row with it). Without
   `decision_ids`, every row lands with `counterfactual_entry_ref = NULL`, severing the
   link between a counterfactual and the decision that produced it. Fixing the call site
   is mechanical but changes journal contents, so it is left for the lead's call rather
   than changed under a rehearsal.
9. **`cmd_eod` is not runnable offline** — it constructs a live `AlpacaClient`
   unconditionally, with no `--offline` mode. The EOD stage therefore replays its steps
   against fixtures instead of invoking the CLI. Worth an `--offline` flag at some point.

---

## 4. Deployment readiness (read-only audit of the deployed data)

| item | state |
|---|---|
| shipped rulebook | **draft**, three unfrozen T6 gates (`multiplier`, deep-OTM calls, deep-OTM puts) — arm B is inert by design |
| deployed IV store | **28 rows, 28 tenor keys, all from 2026-10-02**; `IvRankStore.MIN_OBSERVATIONS = 60` → deepest tenor has **1 of 60** |
| T5 live | **undefined** for every cell → arm B T5 `PENDING` → no entry, ever, until the store warms |
| T6 live | `calibration_pending: true` → arm B T6 `PENDING` |
| T6 mechanics | T6 needs an **EOD-confirmed** flow gate but gates an **intraday** arm B entry. `executor/flow_gate.py` carries the newest prior confirmed row forward with an auditable `GateRead` (source date, side, ratio, multiplier), but whether carry-forward satisfies T6 intraday is **unratified — operator question.** |
| T5 tenor | plan logic reads the expiry-keyed `tenor_key`; reading the rolling `dte_tenor_key` instead is **still an open operator decision.** Not resolved here. |
| warm-up math | at one observation per session per tenor, the first tenor to reach 60 needs **60 sessions** — a data-accumulation problem no code change shortens |

---

## 5. GO / NO-GO checklist for arming the cron trio

| # | gate | status |
|---|---|---|
| 1 | no-shot path (plan → watch → EOD ledger) exercised offline | **GO** |
| 2 | shot path (plan → reclaim → entry → exit → roll) exercised offline | **GO** |
| 3 | watch-loop driver (`run_loop`) exercised with injected clock | **GO** |
| 4 | stale-snapshot veto exercised, exits unaffected | **GO** |
| 5 | EOD streak/close, soak writes, shadow-roll analysis, weekly rollup exercised | **GO** |
| 6 | no live orders, no network, no `/opt` writes, rulebook unmodified | **GO** (measured) |
| 7 | full pytest + ruff clean | **GO** (883 passed) |
| 8 | T6 `N` frozen by the lead | **NO-GO** — proposed 2.681, exclusions/asymmetry/outliers unreviewed |
| 9 | T5 IV rank resolvable in production | **NO-GO** — 1 of 60 observations on every deployed tenor |
| 10 | T6 intraday policy (prior-session carry-forward) ratified | **NO-GO** — contradiction unresolved |
| 11 | T5 tenor choice (expiry vs rolling DTE) ratified | **NO-GO** — open operator decision |
| 12 | arm C roll executor path exists | **NO-GO** — no replacement-leg selection in the loop |
| 13 | entry path reachable for a green plan | **NO-GO** — trigger-path finding above needs a mechanics decision |
| 14 | `cmd_eod` records `taken` and `decision_ids` | **NO-GO** — journal contents wrong for traded sessions |

**Recommendation:** keep all three crons disarmed. Items 8–11 are operator/lead
decisions; 12–14 are mechanics questions that must be answered before an entry can be
routed at all. Items 9's warm-up is calendar-bound: if the window is meant to open in
November, the IV store needs to be accumulating now, and the earlier that starts the
closer item 9 lands.