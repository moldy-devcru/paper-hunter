# Ratification — one sitting, then the window opens

**Status:** DRAFT — unsigned by the operator. Sections (b) and the delegation note in
(a) carry operator rulings dated **2026-10-02**. **Date:** _(fill in when signed)_
**Operator:** _(mads)_

> **Delegation note — 2026-10-02.** The operator explicitly delegated the *technical
> interpretations* in section (a): he was told there are 55 judgment calls the code had
> to make, was told the two that genuinely needed a human (T6's calibration and the
> earnings veto) were being escalated, and answered "autonomous, yes, that's the whole
> point" on execution and delegated the rest. So section (a) is **lead-ratified under
> operator delegation** — every row below is a decision *I* made and *I* stand behind,
> not a row the operator read line by line. The boxes are therefore deliberately left
> unticked rather than faked: a checked box on this document must mean a human read it.
> Any row he wants overruled gets edited in the referenced code and the strategy version
> bumped, before the first trade.

This is the document that turns "the code exists" into "the experiment is pre-registered".
Nothing below is a status update; everything below is either a decision that needs your
signature or a fact you need in order to make one.

**What you are signing.** Section (a) is every judgment call that was not decided by
[`docs/brief.md`](../docs/brief.md) or [`config/rules.example.yaml`](../config/rules.example.yaml).
Each is a real ambiguity in the frozen spec, resolved one way, in code, with the reasoning
written next to it. Approving the section means: *I have read these, I accept the
resolution, and I am not going to relitigate them mid-window.* Overruling any of them means
editing the referenced line and bumping the strategy version, before the first trade.
As of 2026-10-02 these are **lead-ratified under operator delegation** — see the note at
the top; they have not been individually ticked, because they have not been individually read.

**How long this should take.** Section (a) is 55 rows; most are one line and many are
obviously right. The two that genuinely need your attention are called out in (c) — they
are the ones where a reasonable person would have decided differently.

**Rule for the whole document:** if a row here and the code disagree, **the code is what
runs**, and this document is wrong until regenerated. Never the reverse.

---

## (a) Interpretation inventory

<!-- BEGIN GENERATED: INTERPRETATION INVENTORY -->

**62 interpretations across 8 domains.** Every one was a decision
the frozen rulebook or docs/brief.md did not make for us. Each row says what was
decided and the one-line reason; the full reasoning is in the code at the referenced
line, and that code is the source of truth if the two ever disagree.

> Regenerate: `.venv/bin/python scripts/gen_ratification_interpretations.py --write`
> (mechanical; the text between these markers is replaced, the rest of the document
> — including any operator notes — is not).

### indicators (6)

- [ ] **`executor/indicators.py:106`** — seeding = SMA of the first `period` values. The brief is silent; SMA seeding is the textbook / charting-platform standard. ([source](../executor/indicators.py#L106))
- [ ] **`executor/indicators.py:144`** — a flat series -> 50.0 (neutral), not NaN and not an exception. ([source](../executor/indicators.py#L144))
- [ ] **`executor/indicators.py:257`** — population stdev (divisor N), not sample stdev (divisor N-1). ([source](../executor/indicators.py#L257))
- [ ] **`executor/indicators.py:344`** — a zero 20-day baseline is not an error — it yields inf (or 0.0 for zero volume), and the checklist turns a non-finite ratio into a T4 FAIL rather than a crash or a pass. ([source](../executor/indicators.py#L344))
- [ ] **`executor/snapshot_builder.py:39`** — age is measured from the crossover bar's timestamp to the newest bar's timestamp, both from the data (never `now`). ([source](../executor/snapshot_builder.py#L39))
- [ ] **`executor/snapshot_builder.py:233`** — the bandwidth percentile is computed over the `lookback_days` *preceding* bandwidths, excluding the current bar (matching `indicators.bandwidth_percentile`'s own exclusion of the newest value). ([source](../executor/snapshot_builder.py#L233))

### checklist (5)

- [ ] **`executor/checklist.py:251`** — a flat histogram (bitwise unchanged vs the prior bar) is neither expanding nor contracting, so it is not vetoed — the rulebook names only "contracting" as the failing momentum state, "expanding" as preferred. contracting = ( snap.macd_hist <… ([source](../executor/checklist.py#L251))
- [ ] **`executor/checklist.py:271`** — the guard fires when the MOST RECENT cross (cross_direction + cross age, supplied by the data layer) was AGAINST the trade direction and is younger than max_age_hours. ([source](../executor/checklist.py#L271))
- [ ] **`executor/checklist.py:303`** — "expanding in trade direction" is read as *directional* expansion, not just a widening band: bandwidth must be rising AND the middle band must be moving with the trade (rising for calls, falling for puts). ([source](../executor/checklist.py#L303))
- [ ] **`executor/checklist.py:325`** — "close tags lower band and closes back inside" = the signal bar's LOW touched or undercut the lower band AND the close finished back above it (inverted for puts: high tags the upper band, close back below). ([source](../executor/checklist.py#L325))
- [ ] **`executor/checklist.py:374`** — an event day is checked FIRST and short-circuits T5. The brief calls the event calendar a hard veto ("no earnings/FOMC day entries"), so it outranks a healthy IV rank rather than being one AND-condition among several. ([source](../executor/checklist.py#L374))

### data (9)

- [ ] **`executor/alpaca_client.py:47`** — `timeframe` values are the Alpaca wire strings ("1Min", "5Min", "1Day"). Daily and intraday bars use the *same* single-symbol route `GET /v2/stocks/{symbol}/bars` with different `timeframe` values — the symbol is a path segment. ([source](../executor/alpaca_client.py#L47))
- [ ] **`executor/alpaca_client.py:127`** — this splits rather than truncates. ([source](../executor/alpaca_client.py#L127))
- [ ] **`executor/alpaca_client.py:461`** — after sleeping, if the window still looks full (the clock did not move), the oldest stamp is dropped and the call proceeds. ([source](../executor/alpaca_client.py#L461))
- [ ] **`executor/alpaca_client.py:737`** — the reference date is a UTC date. Alpaca expiry timestamps are exchange-local (America/New_York); for an after-hours decision the two agree in every case this executor cares about, and a documented UTC choice is better than an implicit local-time one. ([source](../executor/alpaca_client.py#L737))
- [ ] **`executor/alpaca_client.py:753`** — ties break toward calls. Arm C is calls-only and arm B picks its own strike, so the only consumer of the ATM contract is the IV-rank tenor selection, and a deterministic tie-break keeps that key stable day to day. ([source](../executor/alpaca_client.py#L753))
- [ ] **`executor/alpaca_client.py:1367`** — `end=None` returns False. Alpaca substitutes "now" for a missing end, and "now" sits inside the 15-minute subscription window. ([source](../executor/alpaca_client.py#L1367))
- [ ] **`executor/iv_rank.py:180`** — strikes are bucketed to `bucket_size` (default $5) before keying. A raw-strike key means every strike in a chain accumulates its own separate history, so a rank computed for 650.00 says nothing about 652.50 and the store stays in warmup for months. ([source](../executor/iv_rank.py#L180))
- [ ] **`executor/iv_rank.py:216`** — the EOD soak records BOTH this key and :func:`tenor_key` for every ATM contract it polls, and T5 may read either — the operator picks at ratification. ([source](../executor/iv_rank.py#L216))
- [ ] **`config/loader.py:525`** — moneyness is a **percentage distance from spot**, with separate thresholds per side, because calls and puts do not populate symmetrically around spot on SPY and one shared number would silently call a crowded strike "deep" on one side only. ([source](../config/loader.py#L525))

### trading (18)

- [ ] **`executor/alpaca_trading.py:52`** — paper proof is `account.paper is True` OR an account id beginning `PA` (the prefix Alpaca issues paper accounts). ([source](../executor/alpaca_trading.py#L52))
- [ ] **`executor/alpaca_trading.py:58`** — market orders are the default `time_in_force`. Alpaca rejects `DAY` market orders after 16:00 ET, and the brief's hard-close is 15:30 ET, so DAY is correct for every order this executor places. ([source](../executor/alpaca_trading.py#L58))
- [ ] **`executor/alpaca_trading.py:600`** — this is the *emergency* flatten, used when the executor cannot reason about a position (unparseable symbol, missing state). ([source](../executor/alpaca_trading.py#L600))
- [ ] **`executor/position_manager.py:25`** — profit take — "close 50%" on an odd contract count rounds **up** (`ceil`), so a 1-lot 0DTE position that hits +100% closes entirely rather than rounding to 0 and leaving the rule silently unexecuted. ([source](../executor/position_manager.py#L25))
- [ ] **`executor/position_manager.py:30`** — trail the remainder — the rulebook says `trail_remainder: true` but the brief defines no trail mechanism (no stop level, no trailing band). ([source](../executor/position_manager.py#L30))
- [ ] **`executor/position_manager.py:38`** — weekly entry counter — the "max 3 entries per week" counter resets on the **ISO week** (Monday 00:00 ET) and counts every entry order this manager emits for arm B, not just profitable ones. ([source](../executor/position_manager.py#L38))
- [ ] **`executor/position_manager.py:44`** — journal vocabulary — enforcement maps onto `journal.store`'s `DecisionKind` as STOP (every liquidation, including arm C's exit-all), ROLL (arm C roll), TRADE (every entry, including arm A's single buy-and-hold) and VETO (a governor refusing an entry). ([source](../executor/position_manager.py#L44))
- [ ] **`executor/position_manager.py:50`** — arm C exit-all journal kind — liquidating to cash on a T1 break is recorded as STOP, not ROLL, because it is a liquidation with no replacement leg. The brief: "liquidate to cash, wait for full checklist re-satisfaction". ([source](../executor/position_manager.py#L50))
- [ ] **`executor/position_manager.py:409`** — the endpoints are inclusive. "09:45–14:00 ET only" reads as a closed interval, and the cost of the reading is one minute of entry eligibility at each edge while the cost of the other reading (excluding 14:00) is an arbitrary decision made by a parser. ([source](../executor/position_manager.py#L409))
- [ ] **`executor/position_manager.py:815`** — this is the one arm where "no decisions" means the executor places exactly one order and then never touches it; there is no governor in this function beyond "already bought". ([source](../executor/position_manager.py#L815))
- [ ] **`executor/position_manager.py:987`** — arm C's entry criteria (90-180 DTE, delta >= 0.80, premium <= 50% of bankroll) are *contract-selection* facts that the selection step reports, not numbers this manager can re-derive from a chain snapshot without duplicating that module. ([source](../executor/position_manager.py#L987))
- [ ] **`executor/position_manager.py:1050`** — enforcement actions are not checklist evaluations, but the schema requires a snapshot dict. Rather than invent indicator values, this records the *facts the action was computed from* under an `enforcement` key and leaves the indicator block empty. ([source](../executor/position_manager.py#L1050))
- [ ] **`executor/watch_loop.py:22`** — staleness budget — the rulebook has no field for "how old may the snapshot be", and inventing a config key is a rule change. ([source](../executor/watch_loop.py#L22))
- [ ] **`executor/watch_loop.py:33`** — trigger direction — a plan cell's triggers carry a `sense` ("above"/"below"). A trigger counts as fired when spot has reached the level *in the trade direction of the cell*: for a call cell, a "below" trigger (e.g. ([source](../executor/watch_loop.py#L33))
- [ ] **`executor/watch_loop.py:40`** — re-entry — a cell that has already produced an entry action this session is not re-entered on a later tick, even if spot re-crosses the trigger. ([source](../executor/watch_loop.py#L40))
- [ ] **`executor/watch_loop.py:202`** — `spot` is the close of the most recent 1-minute IEX bar. On the free tier that is the only realtime equity price available (the research note is explicit), and a stale-by-15s last trade is better than nothing for trigger watching. ([source](../executor/watch_loop.py#L202))
- [ ] **`executor/watch_loop.py:513`** — one contract per position. The brief caps arm B's premium in dollars and arm C's in percent of bankroll, but never states a contract count; 1 is the smallest thing that is a trade, and the premium cap is what actually limits size. ([source](../executor/watch_loop.py#L513))
- [ ] **`journal/store.py:242`** — `ALTER TABLE ... ADD COLUMN` is the *only* statement used here, and that is the append-only guarantee talking, not caution. ([source](../journal/store.py#L242))

### analysis (9)

- [ ] **`executor/hunt_plan.py:31`** — a non-blocking condition can still get a trigger, for T3 only. The rulebook's `t3_bollinger.satisfied_if_any_of` makes T3 an OR group, so when one T3 arm PASSes the sibling is reported FAIL (or whatever it is) but `blocking=False`: it cannot veto. ([source](../executor/hunt_plan.py#L31))
- [ ] **`executor/hunt_plan.py:61`** — plan-time conviction is a deterministic function of how far the checklist is from firing — 10 when it fires, otherwise `10 - 3*failed - 1*pending` clipped to 1..10. ([source](../executor/hunt_plan.py#L61))
- [ ] **`executor/hunt_plan.py:68`** — "spot" for chain tenor selection defaults to the newest daily close, because the plan is built pre-market and there is no pre-market option price to use. ([source](../executor/hunt_plan.py#L68))
- [ ] **`executor/hunt_plan.py:216`** — the brief's arm-B strike rule is "first OTM strike beyond the setup-day range projection" and gives no formula. ([source](../executor/hunt_plan.py#L216))
- [ ] **`executor/noshot.py:354`** — DTE is computed against the **session date**, never against `datetime.now()`. ([source](../executor/noshot.py#L354))
- [ ] **`analysis/rollup.py:40`** — the window. `since`/`until` are inclusive calendar dates. `positions` are filtered on the **UTC date component of `exit_ts`** because that is the journal's only clock (`ts` columns are UTC ISO-8601; there is no ET column on positions). ([source](../analysis/rollup.py#L40))
- [ ] **`analysis/rollup.py:49`** — what counts as a "trade" in the counts. A trade is a `decisions` row with kind `TRADE` — the decision ledger is the record of truth, and `positions` is the convenience view (per its own schema comment). ([source](../analysis/rollup.py#L49))
- [ ] **`analysis/shadow_roll.py:27`** — what "quarterly roll" means. The brief says "fixed quarterly roll" without pinning the date. ([source](../analysis/shadow_roll.py#L27))
- [ ] **`analysis/shadow_roll.py:36`** — lineage, not mutation. A roll does not close a row; it appends a new leg with `supersedes_leg_id` pointing at the leg it replaced. ([source](../analysis/shadow_roll.py#L36))

### soak (7)

- [ ] **`executor/soak.py:213`** — "did a session happen?" is answered by **the presence of a daily bar stamped with that date**, not by a weekday check and not by `/v2/clock`. ([source](../executor/soak.py#L213))
- [ ] **`executor/soak.py:283`** — moneyness is measured against **spot** (the session's SPY close), not against a forward or an option-adjusted reference. ([source](../executor/soak.py#L283))
- [ ] **`executor/soak.py:302`** — buckets are keyed by `floor(distance)` and are 1 percentage point wide, so `sum(v for k, v in buckets.items() if int(k) >= t)` is exactly the volume at >= t percent from spot **for any integer t**. ([source](../executor/soak.py#L302))
- [ ] **`executor/soak.py:327`** — the aggregate uses the **unrounded** strike for the OTM test and the histogram, but the aggregate *threshold* is floored to an integer (see :func:`bucket_key`), so a threshold of 3.5% is honoured as 3%. ([source](../executor/soak.py#L327))
- [ ] **`executor/soak.py:403`** — for each expiry we record the **single ATM contract** (nearest strike to spot, calls-first tie-break — `OptionChain.atm_contract`), not every strike in the chain. ([source](../executor/soak.py#L403))
- [ ] **`executor/soak.py:417`** — an expiry whose ATM contract carries no usable IV is **skipped entirely** rather than filled in from the nearest contract that does. ([source](../executor/soak.py#L417))
- [ ] **`executor/soak.py:735`** — the skip count is reported *whenever it is non-zero*, not only when it is alarming. ([source](../executor/soak.py#L735))

### CLI / plumbing (3)

- [ ] **`executor/main.py:36`** — no command writes to the journal unless `--db` is passed. A pre-market plan that journals rows into a database nobody named is a surprise, and the ledger is the experiment's primary artifact. ([source](../executor/main.py#L36))
- [ ] **`executor/main.py:40`** — `watch` reads the plan from `--plan` rather than rebuilding it. ([source](../executor/main.py#L40))
- [ ] **`executor/main.py:660`** — the EMA is taken over the full daily series *including* the session being judged — the same series the checklist's T1 reads, so the exit rule and the entry rule cannot disagree about what the 50EMA is. ([source](../executor/main.py#L660))

### other (5)

- [ ] **`executor/backfill_flow.py:285`** — the grid step is a **parameter, not a fact about the world**. ([source](../executor/backfill_flow.py#L285))
- [ ] **`executor/backfill_flow.py:294`** — strikes are snapped to a multiple of `strike_step` by `ceil(lo/step)*step` and emitted while `<= hi`, so the span is symmetric to within one step and contains no strike outside the window. ([source](../executor/backfill_flow.py#L294))
- [ ] **`executor/backfill_flow.py:364`** — the contract is built by *parsing the OCC symbol back* rather than by remembering what we asked for, so strike/right/expiry in the row come from the same parser the live path uses. ([source](../executor/backfill_flow.py#L364))
- [ ] **`executor/backfill_flow.py:559`** — the message keeps the exception type and text (Alpaca's HTTP status and body are the whole diagnosis — see the feasibility note's §5), and the run continues. ([source](../executor/backfill_flow.py#L559))
- [ ] **`executor/backfill_flow.py:634`** — the honest-empty rule, inherited from the live soak. ([source](../executor/backfill_flow.py#L634))

<!-- END GENERATED: INTERPRETATION INVENTORY -->

---

## (b) Open decisions from the brief

From [`docs/brief.md`](../docs/brief.md) §"Open decisions for the operator", with where
each one actually stands now.

| # | Decision | Status |
|---|---|---|
| 1 | Repo home: `moldy-devcru/paper-hunter` | **Done.** This repo, public, owned by Moldy; operator is contributor. ~700 commits of history carry `Moldy <moldy@devcru.org>` after the 2026-09-28 attribution rewrite. |
| 2 | Window length | **RULED 2026-10-02: 3 months, ~60 sessions.** Extension only at the monthly review. Encoded at `window:` in the rulebook (`months: 3`, `target_sessions: 60`, `extension: monthly_review_only`). |
| 3 | Checklist thresholds are a draft — ratify or edit each | **This is section (a).** Every threshold, and every way of computing it, is listed there. **Lead-ratified under operator delegation, 2026-10-02** — see the delegation note at the top of this document. |
| 4 | Arm C roll rule delta/DTE numbers | **RULED 2026-10-02: confirmed as drafted.** Roll when `delta < 0.70` **or** `dte < 45`, whichever comes first; mechanical, not discretionary. Already encoded at `arms.C.exits.roll.trigger`. |
| 5 | Who pulls the trigger: autonomous executor vs per-trade human approval | **RULED 2026-10-02: autonomous within the frozen rules.** Operator: "autonomous, yes, that's the whole point." There is deliberately no approval flag to set — the CLI has no per-trade approval gate; dry-run is the default and `--live` runs unattended behind the kill switch. Documented at the top of `executor/main.py`. |
| 6 | *(not in the brief's list — added 2026-10-02)* Arm C IV rank ceiling | **RULED 2026-10-02: `< 50`, same as arm B** (delegated to the lead's recommendation). No longer pending; `pending_calibrations` no longer lists it. |
| 7 | *(not in the brief's list — added 2026-10-02)* Earnings veto | **RULED 2026-10-02: DROPPED for SPY.** "Ignore any particular ticker's earnings for SPY" — a component's print does not move index IV regime. FOMC and CPI remain hard vetoes; OPEX remains non-veto. See below. |
| 8 | *(not in the brief's list — added 2026-10-02)* T6 threshold + N | **RULED 2026-10-02: calibrated BY THE ALGO** from the soak's 20-session distribution, frozen at the first monthly review with the numbers shown. Both stay `calibration_pending` until then — that is the ruling, not a gap. See (c). |

### The earnings veto — CLOSED 2026-10-02, by ruling

The brief asked for "no earnings/FOMC day entries". **RULED 2026-10-02: the earnings
half is dropped for SPY** — "ignore any particular ticker's earnings for SPY". A single
component's print does not move index IV regime, so vetoing an index trade because one
name reported would be vetoing on noise, and the rulebook would carry a veto that can
never be evaluated honestly (there is no component calendar, and building one would be
work for a rule the operator does not want).

It is recorded as **disabled by ruling**, not deleted: `earnings_veto` now reads
`enabled: false, required: false, implemented: false, calibration_pending: false,
disabled_by: operator_ruling_2026-10-02`, plus the reason. The loader pins every one of
those fields to its disabled value and pins `veto_kinds` to `["fomc", "cpi"]`, so
re-enabling it is a loader change *and* a rulebook change *and* a version bump — which is
what "explicitly disabled by ruling" should cost. `earnings` remains a valid `EventKind`
so a calendar can still record one for awareness; it simply no longer vetoes by default.

### Still pending in the rulebook (loader refuses to start with these silently open)

`Rulebook.pending_calibrations` is the machine-readable version of this list, and it is
non-empty today:

- [ ] `checklist.t6_flow.multiplier` — **N**. Cannot be calibrated until the soak has
      accumulated its baseline. **Ruled 2026-10-02: the algo calibrates it from the
      20-session distribution and it is frozen at the first monthly review, with the
      numbers shown.** See (c).
- [ ] `checklist.t6_flow.deep_otm.calls` / `.puts` — **what "deep" means**. Currently
      `calibration_pending: true` with a null value; the soak runs at a *working* 3% so
      history accumulates, and **ruled 2026-10-02** that the algo calibrates the frozen
      number from the same distribution at the same review. See (c).

Closed since the draft: `checklist.t5_options_chain.arm_c.iv_rank_max` (now `< 50`,
ruled) and `...event_calendar.earnings_veto` (dropped by ruling). The only two remaining
are the T6 pair, which the operator deliberately left to the algo, and both are `Pending`
values: null, with a reason, and the loader raises if you try to write a number without
clearing `calibration_pending`. That is intentional — a threshold that looks frozen when
it is not is worse than one that visibly is not.

---

## (c) T6 calibration plan — how N gets frozen

This is the item most likely to be got wrong by rushing, so it gets the most space.

### What is being accumulated, and why it cannot be bought

T6 reads: *deep-OTM 0DTE volume on the trade-direction side ≥ N × 20-day baseline*. Both
halves of that need history that no free source carries:

- The **numerator** is the trade day's own volume — available live, and it is the thing
  the gate is evaluated on.
- The **baseline** is 20 prior sessions of SPY deep-OTM 0DTE volume. It does not exist
  anywhere. Per the research note (`docs/research/2026-10-02-flow-data-market.md`), real-time
  programmatic OPRA is a **$2,000/month non-display fee**; the >15-minute-delayed tier is
  $0; and historical OPRA does not exist at any price on a free tier.

So the baseline has to be accumulated a session at a time by `executor/soak.py`, which is
why it is scheduled (`scripts/soak.timer`, 17:30 ET weekdays) rather than run by hand.
**This is the whole reason the job exists, and it is time-critical: every day it does not
run is a day of baseline you do not have.**

The job reads the free **Indicative Pricing Feed** (delayed ~15 min, synthetic). That is
the correct tier here, not a compromise: T6 is `evaluation: EOD_only`, so delayed data
evaluated after the close is exactly as good as real-time for this purpose, and it costs
$0. Every row records `feed` and `is_delayed` so the distinction is auditable rather than
remembered.

### When N can be frozen

**RULED 2026-10-02: the algo calibrates it, from this distribution, and it is frozen at
the FIRST MONTHLY REVIEW with the numbers shown** — i.e. once ≥20 rows exist at a single
`deep_otm_threshold_pct`. Until that review both N and the deep-OTM thresholds stay
`calibration_pending: true` with null values, which is the ruling being honoured, not a
gap. The reasoning below is why this is the defensible choice; it is written down now so
the choice cannot be re-litigated once the data exists.

**PROPOSAL — 20 recorded sessions, i.e. the first 20 trading days of soak operation.**
The reasoning: the rulebook says the baseline *is* 20 sessions, so calibrating N on fewer
than 20 sessions means calibrating against a different distribution than the one the gate
will be evaluated against. Calibrating on more is fine but not required.

Minimum hard requirement: **≥ 20 rows in `flow_baseline`, all at the same
`deep_otm_threshold_pct`.** A threshold change restarts the count by design (rows at
different thresholds are never mixed), so if you revise the deep-OTM thresholds at
ratification, the clock restarts from that day. **Ratify the thresholds before the soak
starts collecting, not after.**

### The formula — RULED 2026-10-02 as drafted, frozen at the first monthly review

Given `r_1 … r_20`, the daily ratios of trade-side deep-OTM volume to the trailing 20-day
baseline mean:

> **RULED 2026-10-02: `N = P90(r_1 … r_20)`** — the 90th percentile of the observed
> ratio distribution, i.e. 90% of historical days pass the gate. Adopted as drafted and
> delegated to the algo, with the numbers shown at the freeze.

Why 90th percentile, honestly assessed:

- **It makes the gate rare.** T6 is one of seven conditions; a gate that passes 50% of the
  time is not a filter. 90% is a defensible "unusual day" bar.
- **It is empirical, not asserted.** N comes from the distribution N is applied to, rather
  than from a number someone liked the sound of.
- **It is unstable on 20 samples.** The 90th percentile of 20 points is essentially the
  2nd-largest — one quiet week can move it substantially. This is the real weakness of the
  proposal and it is why (a) exists: you are signing the definition now, before the
  distribution exists, so the definition cannot be chosen to fit whatever the data turns
  out to be.

**Alternatives, overruled 2026-10-02** (recorded so a later reader does not have to
re-derive why they are not in the rulebook):

- **Fixed multiple** (N = 1.5, 2.0). Simpler and stable, but the number is a guess, and
  this whole exercise exists so the number is not a guess.
- **Percentile of a longer window** (30 or 60 sessions). More stable, but the gate's own
  baseline is 20 sessions, so the two would not agree.
- **N = P95.** Strictly rarer; with 20 samples it is the single largest observation, which
  makes it one bad data day away from being unpassable.

Marked settled deliberately. This formula has not seen the data and is not claimed to be
right — it is claimed to be *decided in advance*, which is the property that makes the
eventual result mean something. The operator's 2026-10-02 ruling settled it before the
distribution existed, which is the whole point; the freeze at the monthly review is when
the number gets written down, with the distribution shown beside it.

### How the calibration is read off the store

```sql
-- once 20 rows exist
SELECT date, deep_otm_call_volume, baseline_call_mean,
       ratio_call, ratio_put
FROM flow_baseline
WHERE ratio_call IS NOT NULL AND ratio_put IS NOT NULL
ORDER BY date;
```

The trade-direction side is what gets compared — calls for arm B's long-premium setups,
puts for anything short. `flow_baseline` stores both per side, so the calibration does not
have to guess which side the experiment will trade first.

---

## (d) Window-start checklist

Nothing below is optional. Each line is a thing that is false today or unverified today.
**The first simulated trade may not be taken until every box is checked.**

### Ratification and calibration

- [ ] **Section (a) signed** — all 55 interpretations approved or overruled in writing.
      *Status 2026-10-02: lead-ratified under operator delegation (see the delegation note
      at the top). Left unticked on purpose: the operator has not read these row by row,
      and a ticked box here must mean a human did. Overrule anything in it at any time
      before the first trade — the fix is the referenced code line plus a version bump.*
- [x] **Section (b) items 2, 4, 5 answered** — window length, arm C roll numbers, trigger
      mode. (Item 1 is done; item 3 *is* section (a).) **Ruled 2026-10-02:** 3 months /
      ~60 sessions extending only at monthly review; roll at delta < 0.70 or DTE < 45,
      whichever first; autonomous execution within the frozen rules.
- [ ] **Strategy version bumped and frozen.** `config/rules.example.yaml` is edited, the
      content hash changes, and `1.0.0-draft` becomes `1.0.0`. A draft rulebook running a
      "pre-registered" experiment is a contradiction, and `strategy_version` is stamped on
      every journal row so the experiment can always answer which rules produced a trade.
- [ ] **T6 deep-OTM thresholds frozen** (calls/puts, percent from spot). Do this **before**
      the soak accumulates its baseline — changing it restarts the 20-session count.
      *Ruled 2026-10-02: calibrated by the algo from the 20-session distribution, frozen at
      the first monthly review with the numbers shown. The working 3% the soak runs at is
      the input to that calibration, not the frozen value.*
- [ ] **T6 N frozen** from the ≥20-session baseline, per the formula ruled in (c). Same
      review as the line above.
- [x] **T5 `iv_rank_max` and the earnings-veto ruling** resolved in the rulebook, not left
      as `Pending`. **Ruled 2026-10-02:** arm C's ceiling is `< 50` (same as arm B) and the
      earnings veto is dropped for SPY, `disabled_by: operator_ruling_2026-10-02`. Both are
      load-bearing in the loader, not just in the file.
- [ ] **`pending_calibrations` is empty.** Loadable assertion:
      `.venv/bin/python -c "import config.loader as c; r=c.load_rules(); print(r.pending_calibrations)"`
      must print `[]`. **Today it prints the three T6 items and nothing else** — which is
      correct until the monthly-review freeze, and the last item on this list.

### Data and feeds

- [ ] **VIX-proxy reconciliation.** `scripts/seed_ivrank.py` backfills free Cboe VIX daily
      CSVs as `is_proxy=1` warmup seed. Before the window: confirm the seed's coverage ends
      where the real soak series begins, that the seam is visible in the store rather than
      papered over, and that no proxy row can reach a real SPY rank (two independent guards:
      the flag and the `VIX` underlying key).
- [ ] **IV warmup actually reached.** `MIN_OBSERVATIONS = 60`. If the soak's ATM tenor keys
      are still in warmup at window start, T5 reports PENDING and **blocks**. Decide now
      whether to open the window in that state (honest, T5 simply cannot pass) or to wait.
- [ ] **Which tenor T5 reads** — the expiry-keyed `tenor_key` or the rolling
      `dte_tenor_key`. Both are being stored; only one is read. See `executor/iv_rank.py`.
      *This now matters for arm C as well as arm B: arm C reads an IV rank too (ruled
      2026-10-02), so the choice is no longer arm-B-only.*
- [ ] **CPI/FOMC dates verified.** [`data/events/`](../data/events/README.md) covers
      scheduled dates, but the last few weeks before the window must be confirmed against
      the actual published calendar, and the calendar refreshed. An unverified event date
      is a hard veto that never fires, or one that fires on a day it should not — both
      corrupt the veto's meaning. *(This is the whole veto now: earnings are out by ruling,
      so these two kinds are the entire hard-veto surface.)*
- [ ] **Event-calendar coverage checked** — `EventCalendar.coverage()` spans the whole
      window with no gaps. `unverified()` is empty.

### Operations

- [ ] **`soak.timer` armed and verified firing.** `systemctl list-timers soak.timer`, then
      `journalctl -u soak.service` showing a real recorded session — not just an enabled
      timer. A timer that has never fired is an assumption.
- [ ] **`/etc/paper-hunter/soak.env` created**, `0640 root:paper-hunter`, **paper**
      credentials only. See [`data/README.md`](../data/README.md) § `soak.env`. Never
      committed, never live keys.
- [ ] **Baseline actually accumulating** — a spot check that `flow_baseline` has a row for
      each session since the timer was armed, with no gaps and `contracts_without_volume`
      behaving.
- [ ] **Paper account confirmed paper.** `account.paper is True` or a `PA`-prefixed account
      id, before the first order. This is the single check that separates "we simulated"
      from "we traded" — and under the 2026-10-02 autonomy ruling it is the last human
      gate before the executor trades unattended.
- [ ] **Dry run of the full executor end-to-end**, writing to a scratch DB, with the
      resulting journal inspected by hand.
- [ ] **Kill switch tested.** The emergency flatten path (`alpaca_trading.py`) exercised at
      least once, deliberately, on paper. An untested emergency exit is a hypothesis.
- [ ] **`--db` confirmed required** — no command writes to the journal unless a database
      path is passed, so the window's real DB is only ever written by an explicit `--db`.

### Sign-off

- [ ] **This document signed and dated**, and the commit that carries it is tagged.
- [ ] **The pre-registration is pushed and the tag is public.** The value of a
      pre-registered experiment collapses the moment the rules can be edited after seeing
      results; a public, dated tag is what makes later edits visible as edits.

---

## Appendix: what this document is not

- It is **not** a claim the code is correct. The tests (527 passing at time of writing) are
  evidence of a different kind, and passing tests do not ratify a threshold.
- It is **not** a substitute for `docs/brief.md`. Where they disagree, the brief is the
  specification and this is the record of how the gaps were filled.
- It is **not** self-updating for sections (b), (c), or (d). Only (a) is generated; the
  rest is the operator's document, and an assistant that rewrote it would be rewriting the
  parts that are supposed to be yours.