# Full-cycle rehearsal — 2026-10-03

**Scope:** the whole daily cycle, offline, end to end, before the trading window opens in
November. **Verdict: NO-GO for arming the daily cron trio.** Every stage ran and every
mechanical path works; the blockers are data readiness, not broken code.

> **Updated 2026-10-03 (addendum three).** The two design-level findings that made this a
> NO-GO on *mechanics* — arm C's missing roll executor and the un-enterable green plan —
> were operator questions, and are now **ruled (R1, R2) and fixed**. The remaining NO-GO is
> **data readiness only**: T5's IV-rank warm-up and T6's `N` both need sessions of soak
> history that have not accumulated yet, and R4's keying change restarts the IV warm-up
> from zero (see the addendum). Addendum two closed findings 8 and 9.

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
| 12 | `eod` | the **real** `executor.main.main(["eod", "--offline", ...])` for both sessions: EMA streak, NO-SHOT ledger (traded cell excluded, every counterfactual linked), EOD close decision |
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

> Findings **6 and 7 are now closed** by rulings R1 and R2 — see addendum three below.
> They are left here as originally written, because the record of *why they were open* is
> part of the evidence that they are now closed.

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

### Addendum — items 8 and 9 are now fixed (`839e73d`, 2026-10-03)

Both gaps above were closed after the rehearsal was reviewed. The findings are left
above as written; this is what changed.

**Item 8 — `taken` and `decision_ids` are recorded.** `cmd_eod` now passes both to
`build_noshots`, and reads both out of the journal rather than guessing:

* `taken` comes from the session's own `TRADE` decisions (ET session date, arm, and the
  direction parsed from the contract's OCC right, so arm B's call cell and put cell are
  told apart by what was actually bought). The EOD close decision now carries the derived
  `taken` in its `checklist_state` and names it in its reasoning, so the ledger's claim is
  visible on the decision row too.
* `decision_ids` comes from the **pre-market** plan row for each cell — an intraday
  watch-loop `NO_TRADE` row for the same `(arm, direction)` must not attribute a
  counterfactual to a re-verification. When a plan was rewritten before the open, the
  newest pre-open row wins, because that is the decision the day's execution ran against.
* A `TRADE` row whose symbol is not a parsable OCC contract is reported on stderr (the
  cell is then treated as *not* taken, which is the conservative direction for the
  counterfactual but is a data fault and says so). A sighting whose plan row is missing is
  written — dropping a real sighting over a bookkeeping gap is the worse failure for a
  ledger — with a loud warning that its `counterfactual_entry_ref` is NULL.

Journal semantics are unchanged: the ledger stays append-only, and no schema moved. The
rehearsal now asserts both properties directly (a traded cell gets no row; every row
carries its link).

**Item 9 — `cmd_eod` is runnable offline.** `eod --offline --offline-fixture <file.json>`
swaps the client's transport for `MockTransport` over documented route payloads
(`/v2/stocks/SPY/bars` and `/v1beta1/options/snapshots/SPY`). It is a transport swap, not a
second code path: the same parsing, the same doc-verified query-parameter contract, and a
404 for an unrouted request rather than a silent empty series. No credential is read, no
order is routed, and `--offline` with `--live` is refused.

The EOD stage no longer replays the command's steps. It writes the day's bars and chain to
a fixture file, journals the shot session's `TRADE` row where production's
`SqliteJournalSink` writes it, and then calls
`executor.main.main([..., "eod", "--offline", "--offline-fixture", ...])` for **both**
sessions — so the pass under test is the shipped one, and a fixture that drifts from the
API's shape fails the rehearsal instead of passing it. The old "cmd_eod, unmodified"
demonstration is gone because the bug it demonstrated is gone; the stage's findings now
report the two properties as confirmed.

The net effect on a rehearsal run: the shot session (which traded B/call) now writes
**0** rows and says `traded ['B/call']`; the no-shot session writes 2 rows, both linked
(`decision_ref=2` and `decision_ref=4` — its own plan rows).

### Addendum — findings 6 and 7 closed by rulings R1 and R2 (2026-10-03)

Findings 6 and 7 above were **operator questions** and have now been ruled. Both are
fixed, and the rehearsal exercises the fixed path rather than describing it.

**Finding 6 → R1 (arm C roll replacement).** The rehearsal no longer supplies a
replacement by hand. `position_manager.arm_c_roll_replacement` selects it from the tick's
own chain (which `AlpacaWatchData` already fetches for the contract quote, so this costs
no extra network call) by the ruling: **earliest expiry in the 90–180 DTE band, strike
nearest delta 0.80, inside arm C's existing premium cap.**

The fixture chain is built so the correct answer is knowable and the three refusals are
tests of the rule rather than of the fixture: an expiry below the band at a perfect 0.80
delta, an expiry above the band at 0.80, two in-band strikes flanking 0.80, and a **put at
exactly the target delta** that must still be rejected because arm C is calls-only. The
stage now reports:

```
arm C roll selection (R1): 20270127 100 DTE, strike 475, delta 0.81, premium $700.00 of $5,000.00
arm C roll refusals (R1): no_qualifying_expiry=True, no_readable_greeks=True, over_the_premium_cap=True
```

and **fails** if the selection is absent, lands outside the band, is far from the 0.80
target, or if any refusal case stops refusing.

Two things this fixed that were not in the finding:

* **The buy leg is marked on the ASK**, not the bid. `watch_loop.contract_price` marks a
  long being *sold* (bid → mid → ask); reusing it for a buy would understate what the roll
  spends and let a cap-breaching roll through the cap check. The two directions now have
  separate functions on purpose.
* **The post-roll position now adopts the new contract's Greeks.** Previously the rolled
  position inherited the *old* contract's delta, DTE, expiry and strike — so the tick
  after a roll re-read the pre-roll Greeks, the trigger fired again immediately, and the
  position would have rolled on every pass until the chain ran dry. A roll that re-triggers
  its own trigger is not a roll. The Greeks are what the trigger reads, so they are part of
  the state a roll must update. Provenance for both sides lands in `position.meta["roll"]`.

**Finding 7 → R2 (green-plan arming).** Ruled: a green plan arms the loop on the **entry
window alone**. `_cell_plan_green` reads the plan's own pre-market `checklist.fire` rather
than re-evaluating it, and a green cell arms exactly **one** intraday re-verification per
cell per session, inside the entry window.

> **The journal-volume constraint behind the "one"**, recorded because it was a real
> design pressure and not an arbitrary cap: `checklist_failure_histogram()` counts
> `NO_TRADE` decisions, and the re-verification fires the entry governors again, which
> return `NO_TRADE` VETOs. Re-verifying on *every* watch tick would write a failing row per
> tick and swamp the histogram with rows that are not failures but "already checked". The
> per-session budget bounds it; if the histogram ever shows re-verification noise, the fix
> is to tag those rows distinctly, not to raise the budget.

**R3** (`carry_forward`, operator-ratified 2026-10-03 00:43 EDT), **R4**
(`tenor_key_mode: rolling_dte`, 7-day buckets) and **R5** (November opens arms **A + C**,
arm B inert until T5 warms) are encoded and covered in `docs/ratification.md` §(b). Two of
them changed what the rehearsal *measures*:

* **R4 is now asserted, not narrated.** The `iv-warmup` stage recomputes the tenor key for
  the next 14 days and **fails if it churns daily** — reporting `over 15 days the same
  contract yields 1 distinct key(s)`. This is the load-bearing property: under the old
  raw-integer-DTE key every session minted a new tenor, so each series was length 1 and
  could never reach `MIN_OBSERVATIONS` regardless of soak length. The fixture now calls
  `hunt_plan._tenor_key_for` directly rather than reimplementing it, because the first cut
  of that helper omitted the executor's `max(dte, 0)` clamp and blew up on the fixture
  chain's 0DTE contract — a rehearsal that keys differently from the executor would warm a
  store the executor cannot read.
* **R4's cost to live data is now reported.** The deployed store holds **28 rows across 28
  tenor keys** — one observation per key, the exact signature of the old keying. Under
  `rolling_dte` those rows sit under keys the executor will never query again, so T5's
  warm-up restarts from zero. The stage flags this as an **operator decision, not decided
  here**: accept the restart (~60 sessions), re-key the old rows (which recovers nothing
  real, since one observation per key was never a distribution), or backfill from the
  provider. My recommendation is to accept it — re-keying would manufacture a warm-looking
  series out of singletons, which is precisely the kind of green that is not earned.

**R1's remaining honest gap:** the roll requires the *live* chain, so if the chain read
fails the position holds and the tick's note says so. That is the intended behaviour (the
ruling says never close instead of rolling), but it means an arm C position past its roll
trigger can sit unsatisfied for as long as the chain is unreadable. The note is the
mitigation; the operator should expect to see it.

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
| 7 | full pytest + ruff clean | **GO** (932 passed) |
| 8 | T6 `N` frozen by the lead | **NO-GO** — proposed 2.681, exclusions/asymmetry/outliers unreviewed |
| 9 | T5 IV rank resolvable in production | **NO-GO** — 1 of 60 observations on every deployed tenor, and R4's keying change restarts the warm-up from zero |
| 10 | T6 intraday policy ratified | **GO** — R3, operator-ratified 2026-10-03 00:43 EDT, `carry_forward` |
| 11 | T5 tenor choice ratified | **GO** — R4, `rolling_dte` at 7-day buckets |
| 12 | arm C roll executor path exists | **GO** — R1; selection from the live chain, refusals and stuck-roll reporting rehearsed |
| 13 | entry path reachable for a green plan | **GO** — R2; one intraday re-verification per cell per session inside the entry window |
| 14 | `cmd_eod` records `taken` and `decision_ids` | **GO** — addendum two |

**Recommendation:** keep all three crons disarmed, but for one reason now rather than
five. Every **mechanics** gate (12, 13, 14) and every **open decision** (10, 11) is
closed. What is left is purely **data readiness**: item 8's `N` needs enough baseline
sessions to be worth freezing, and item 9's warm-up is calendar-bound — and under R4 it
starts from zero rather than from the 28 singleton rows the old keying left behind.

The clock is therefore the only thing that matters for item 9. If the window is meant to
open in November, the soak has to be accumulating under the new keying **now**, because
60 sessions at one observation per session per tenor is the whole cost of it. The
pre-R4 rows are not a partial head start; they are unreachable history, and re-keying
them would only make the store look warmer than it is (see addendum three).