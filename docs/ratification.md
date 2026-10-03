# Ratification — one sitting, then the window opens

**Status:** DRAFT — unsigned by the operator. Sections (b) and the delegation note in
(a) carry operator rulings dated **2026-10-02**; section (e) and ruling **E1** were
drafted 2026-10-03 after the frozen-rule rulings debate, **revised the same day** once the
T5 backfill landed (which withdrew (e)'s structural reason and changed one of E1's
prediction statuses), and are **awaiting operator signature**. **Date:** _(fill in when signed)_ **Operator:** _(mads)_

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

**78 interpretations across 8 domains.** Every one was a decision
the frozen rulebook or docs/brief.md did not make for us. Each row says what was
decided and the one-line reason; the full reasoning is in the code at the referenced
line, and that code is the source of truth if the two ever disagree.

> Regenerate: `.venv/bin/python scripts/gen_ratification_interpretations.py --write`
> (mechanical; the text between these markers is replaced, the rest of the document
> — including any operator notes — is not).

### indicators (8)

- [ ] **`executor/indicators.py:106`** — seeding = SMA of the first `period` values. The brief is silent; SMA seeding is the textbook / charting-platform standard. ([source](../executor/indicators.py#L106))
- [ ] **`executor/indicators.py:144`** — a flat series -> 50.0 (neutral), not NaN and not an exception. ([source](../executor/indicators.py#L144))
- [ ] **`executor/indicators.py:257`** — population stdev (divisor N), not sample stdev (divisor N-1). ([source](../executor/indicators.py#L257))
- [ ] **`executor/indicators.py:344`** — a zero 20-day baseline is not an error — it yields inf (or 0.0 for zero volume), and the checklist turns a non-finite ratio into a T4 FAIL rather than a crash or a pass. ([source](../executor/indicators.py#L344))
- [ ] **`executor/snapshot_builder.py:39`** — age is measured from the crossover bar's timestamp to the newest bar's timestamp, both from the data (never `now`). ([source](../executor/snapshot_builder.py#L39))
- [ ] **`executor/snapshot_builder.py:235`** — the bandwidth percentile is computed over the `lookback_days` *preceding* bandwidths, excluding the current bar (matching `indicators.bandwidth_percentile`'s own exclusion of the newest value). ([source](../executor/snapshot_builder.py#L235))
- [ ] **`executor/snapshot_builder.py:384`** — the clock is the newest bar's, not `now()`. ([source](../executor/snapshot_builder.py#L384))
- [ ] **`executor/snapshot_builder.py:389`** — every insufficient input yields `None` rather than a number computed on what happens to be available. A shrunk baseline, a dropped session, and a zero window all still produce a ratio; none of them produce the one the frozen rule names. ([source](../executor/snapshot_builder.py#L389))

### checklist (7)

- [ ] **`executor/checklist.py:257`** — a flat histogram (bitwise unchanged vs the prior bar) is neither expanding nor contracting, so it is not vetoed — the rulebook names only "contracting" as the failing momentum state, "expanding" as preferred. contracting = ( snap.macd_hist <… ([source](../executor/checklist.py#L257))
- [ ] **`executor/checklist.py:277`** — the guard fires when the MOST RECENT cross (cross_direction + cross age, supplied by the data layer) was AGAINST the trade direction and is younger than max_age_hours. ([source](../executor/checklist.py#L277))
- [ ] **`executor/checklist.py:309`** — "expanding in trade direction" is read as *directional* expansion, not just a widening band: bandwidth must be rising AND the middle band must be moving with the trade (rising for calls, falling for puts). ([source](../executor/checklist.py#L309))
- [ ] **`executor/checklist.py:331`** — "close tags lower band and closes back inside" = the signal bar's LOW touched or undercut the lower band AND the close finished back above it (inverted for puts: high tags the upper band, close back below). ([source](../executor/checklist.py#L331))
- [ ] **`executor/checklist.py:369`** — how the two branches combine. The brief joins them with "or", so either branch alone satisfies T4; they are not both required. ([source](../executor/checklist.py#L369))
- [ ] **`executor/checklist.py:377`** — a non-finite value on either branch is a FAIL, not an exception and not a pass: the ratio is what it is, the window behind it was unusable, and volume did not confirm. ([source](../executor/checklist.py#L377))
- [ ] **`executor/checklist.py:426`** — an event day is checked FIRST and short-circuits T5. The brief calls the event calendar a hard veto ("no earnings/FOMC day entries"), so it outranks a healthy IV rank rather than being one AND-condition among several. ([source](../executor/checklist.py#L426))

### data (10)

- [ ] **`executor/alpaca_client.py:47`** — `timeframe` values are the Alpaca wire strings ("1Min", "5Min", "1Day"). Daily and intraday bars use the *same* single-symbol route `GET /v2/stocks/{symbol}/bars` with different `timeframe` values — the symbol is a path segment. ([source](../executor/alpaca_client.py#L47))
- [ ] **`executor/alpaca_client.py:127`** — this splits rather than truncates. ([source](../executor/alpaca_client.py#L127))
- [ ] **`executor/alpaca_client.py:461`** — after sleeping, if the window still looks full (the clock did not move), the oldest stamp is dropped and the call proceeds. ([source](../executor/alpaca_client.py#L461))
- [ ] **`executor/alpaca_client.py:737`** — the reference date is a UTC date. Alpaca expiry timestamps are exchange-local (America/New_York); for an after-hours decision the two agree in every case this executor cares about, and a documented UTC choice is better than an implicit local-time one. ([source](../executor/alpaca_client.py#L737))
- [ ] **`executor/alpaca_client.py:753`** — ties break toward calls. Arm C is calls-only and arm B picks its own strike, so the only consumer of the ATM contract is the IV-rank tenor selection, and a deterministic tie-break keeps that key stable day to day. ([source](../executor/alpaca_client.py#L753))
- [ ] **`executor/alpaca_client.py:1367`** — `end=None` returns False. Alpaca substitutes "now" for a missing end, and "now" sits inside the 15-minute subscription window. ([source](../executor/alpaca_client.py#L1367))
- [ ] **`executor/iv_rank.py:243`** — strikes are bucketed to `bucket_size` (default $5) before keying. A raw-strike key means every strike in a chain accumulates its own separate history, so a rank computed for 650.00 says nothing about 652.50 and the store stays in warmup for months. ([source](../executor/iv_rank.py#L243))
- [ ] **`executor/iv_rank.py:301`** — the EOD soak records BOTH this key and :func:`tenor_key` for every ATM contract it polls, and T5 may read either — the operator picks at ratification. ([source](../executor/iv_rank.py#L301))
- [ ] **`executor/iv_rank.py:399`** — the key is NAMESPACED (`mte`/`mny`) rather than reusing the `dte`/dollar prefix, and this is load-bearing rather than cosmetic. ([source](../executor/iv_rank.py#L399))
- [ ] **`config/loader.py:577`** — moneyness is a **percentage distance from spot**, with separate thresholds per side, because calls and puts do not populate symmetrically around spot on SPY and one shared number would silently call a crowded strike "deep" on one side only. ([source](../config/loader.py#L577))

### trading (19)

- [ ] **`executor/alpaca_trading.py:52`** — paper proof is `account.paper is True` OR an account id beginning `PA` (the prefix Alpaca issues paper accounts). ([source](../executor/alpaca_trading.py#L52))
- [ ] **`executor/alpaca_trading.py:58`** — market orders are the default `time_in_force`. Alpaca rejects `DAY` market orders after 16:00 ET, and the brief's hard-close is 15:30 ET, so DAY is correct for every order this executor places. ([source](../executor/alpaca_trading.py#L58))
- [ ] **`executor/alpaca_trading.py:600`** — this is the *emergency* flatten, used when the executor cannot reason about a position (unparseable symbol, missing state). ([source](../executor/alpaca_trading.py#L600))
- [ ] **`executor/position_manager.py:25`** — profit take — "close 50%" on an odd contract count rounds **up** (`ceil`), so a 1-lot 0DTE position that hits +100% closes entirely rather than rounding to 0 and leaving the rule silently unexecuted. ([source](../executor/position_manager.py#L25))
- [ ] **`executor/position_manager.py:30`** — trail the remainder — the rulebook says `trail_remainder: true` but the brief defines no trail mechanism (no stop level, no trailing band). ([source](../executor/position_manager.py#L30))
- [ ] **`executor/position_manager.py:38`** — weekly entry counter — the "max 3 entries per week" counter resets on the **ISO week** (Monday 00:00 ET) and counts every entry order this manager emits for arm B, not just profitable ones. ([source](../executor/position_manager.py#L38))
- [ ] **`executor/position_manager.py:44`** — journal vocabulary — enforcement maps onto `journal.store`'s `DecisionKind` as STOP (every liquidation, including arm C's exit-all), ROLL (arm C roll), TRADE (every entry, including arm A's single buy-and-hold) and VETO (a governor refusing an entry). ([source](../executor/position_manager.py#L44))
- [ ] **`executor/position_manager.py:50`** — arm C exit-all journal kind — liquidating to cash on a T1 break is recorded as STOP, not ROLL, because it is a liquidation with no replacement leg. The brief: "liquidate to cash, wait for full checklist re-satisfaction". ([source](../executor/position_manager.py#L50))
- [ ] **`executor/position_manager.py:432`** — the endpoints are inclusive. "09:45–14:00 ET only" reads as a closed interval, and the cost of the reading is one minute of entry eligibility at each edge while the cost of the other reading (excluding 14:00) is an arbitrary decision made by a parser. ([source](../executor/position_manager.py#L432))
- [ ] **`executor/position_manager.py:1009`** — this is the one arm where "no decisions" means the executor places exactly one order and then never touches it; there is no governor in this function beyond "already bought". ([source](../executor/position_manager.py#L1009))
- [ ] **`executor/position_manager.py:1205`** — the DECLARED entry window bounds NEW entries only, never R1's roll. ([source](../executor/position_manager.py#L1205))
- [ ] **`executor/position_manager.py:1214`** — arm C's entry criteria (90-180 DTE, delta >= 0.80, premium <= 50% of bankroll) are *contract-selection* facts that the selection step reports, not numbers this manager can re-derive from a chain snapshot without duplicating that module. ([source](../executor/position_manager.py#L1214))
- [ ] **`executor/position_manager.py:1289`** — enforcement actions are not checklist evaluations, but the schema requires a snapshot dict. Rather than invent indicator values, this records the *facts the action was computed from* under an `enforcement` key and leaves the indicator block empty. ([source](../executor/position_manager.py#L1289))
- [ ] **`executor/watch_loop.py:22`** — staleness budget — the rulebook has no field for "how old may the snapshot be", and inventing a config key is a rule change. ([source](../executor/watch_loop.py#L22))
- [ ] **`executor/watch_loop.py:33`** — trigger direction — a plan cell's triggers carry a `sense` ("above"/"below"). A trigger counts as fired when spot has reached the level *in the trade direction of the cell*: for a call cell, a "below" trigger (e.g. ([source](../executor/watch_loop.py#L33))
- [ ] **`executor/watch_loop.py:40`** — re-entry — a cell that has already produced an entry action this session is not re-entered on a later tick, even if spot re-crosses the trigger. ([source](../executor/watch_loop.py#L40))
- [ ] **`executor/watch_loop.py:212`** — `spot` is the close of the most recent 1-minute IEX bar. On the free tier that is the only realtime equity price available (the research note is explicit), and a stale-by-15s last trade is better than nothing for trigger watching. ([source](../executor/watch_loop.py#L212))
- [ ] **`executor/watch_loop.py:594`** — one contract per position. The brief caps arm B's premium in dollars and arm C's in percent of bankroll, but never states a contract count; 1 is the smallest thing that is a trade, and the premium cap is what actually limits size. ([source](../executor/watch_loop.py#L594))
- [ ] **`journal/store.py:242`** — `ALTER TABLE ... ADD COLUMN` is the *only* statement used here, and that is the append-only guarantee talking, not caution. ([source](../journal/store.py#L242))

### analysis (9)

- [ ] **`executor/hunt_plan.py:31`** — a non-blocking condition can still get a trigger, for T3 only. The rulebook's `t3_bollinger.satisfied_if_any_of` makes T3 an OR group, so when one T3 arm PASSes the sibling is reported FAIL (or whatever it is) but `blocking=False`: it cannot veto. ([source](../executor/hunt_plan.py#L31))
- [ ] **`executor/hunt_plan.py:61`** — plan-time conviction is a deterministic function of how far the checklist is from firing — 10 when it fires, otherwise `10 - 3*failed - 1*pending` clipped to 1..10. ([source](../executor/hunt_plan.py#L61))
- [ ] **`executor/hunt_plan.py:68`** — "spot" for chain tenor selection defaults to the newest daily close, because the plan is built pre-market and there is no pre-market option price to use. ([source](../executor/hunt_plan.py#L68))
- [ ] **`executor/hunt_plan.py:223`** — the brief's arm-B strike rule is "first OTM strike beyond the setup-day range projection" and gives no formula. ([source](../executor/hunt_plan.py#L223))
- [ ] **`executor/noshot.py:354`** — DTE is computed against the **session date**, never against `datetime.now()`. ([source](../executor/noshot.py#L354))
- [ ] **`analysis/rollup.py:40`** — the window. `since`/`until` are inclusive calendar dates. `positions` are filtered on the **UTC date component of `exit_ts`** because that is the journal's only clock (`ts` columns are UTC ISO-8601; there is no ET column on positions). ([source](../analysis/rollup.py#L40))
- [ ] **`analysis/rollup.py:49`** — what counts as a "trade" in the counts. A trade is a `decisions` row with kind `TRADE` — the decision ledger is the record of truth, and `positions` is the convenience view (per its own schema comment). ([source](../analysis/rollup.py#L49))
- [ ] **`analysis/shadow_roll.py:27`** — what "quarterly roll" means. The brief says "fixed quarterly roll" without pinning the date. ([source](../analysis/shadow_roll.py#L27))
- [ ] **`analysis/shadow_roll.py:36`** — lineage, not mutation. A roll does not close a row; it appends a new leg with `supersedes_leg_id` pointing at the leg it replaced. ([source](../analysis/shadow_roll.py#L36))

### soak (11)

- [ ] **`executor/soak.py:238`** — "did a session happen?" is answered by **the presence of a daily bar stamped with that date**, not by a weekday check and not by `/v2/clock`. ([source](../executor/soak.py#L238))
- [ ] **`executor/soak.py:308`** — moneyness is measured against **spot** (the session's SPY close), not against a forward or an option-adjusted reference. ([source](../executor/soak.py#L308))
- [ ] **`executor/soak.py:327`** — buckets are keyed by `floor(distance)` and are 1 percentage point wide, so `sum(v for k, v in buckets.items() if int(k) >= t)` is exactly the volume at >= t percent from spot **for any integer t**. ([source](../executor/soak.py#L327))
- [ ] **`executor/soak.py:352`** — the aggregate uses the **unrounded** strike for the OTM test and the histogram, but the aggregate *threshold* is floored to an integer (see :func:`bucket_key`), so a threshold of 3.5% is honoured as 3%. ([source](../executor/soak.py#L352))
- [ ] **`executor/soak.py:448`** — `right` is deliberately NOT filtered. One EOD sweep serves both arms and the gate filters by direction at read time, so recording both sides is what lets a call-side rank warm without a second pass tomorrow. ([source](../executor/soak.py#L448))
- [ ] **`executor/soak.py:536`** — this is a MEASUREMENT fix, not a rule change. ([source](../executor/soak.py#L536))
- [ ] **`executor/soak.py:542`** — a band with no contract, or whose nearest contract carries no usable IV, is SKIPPED and counted, never filled from a neighbouring expiry. ([source](../executor/soak.py#L542))
- [ ] **`executor/soak.py:599`** — for each expiry we record the **single ATM contract** (nearest strike to spot, calls-first tie-break — `OptionChain.atm_contract`), not every strike in the chain. ([source](../executor/soak.py#L599))
- [ ] **`executor/soak.py:614`** — an expiry whose ATM contract carries no usable IV is **skipped entirely** rather than filled in from the nearest contract that does. ([source](../executor/soak.py#L614))
- [ ] **`executor/soak.py:966`** — the skip count is reported *whenever it is non-zero*, not only when it is alarming. ([source](../executor/soak.py#L966))
- [ ] **`executor/soak.py:972`** — band skips are reported separately and labelled as the warm-up clock rather than folded into the expiry count. "3 expiries skipped" means three holes in a series that otherwise accumulates;. ([source](../executor/soak.py#L972))

### CLI / plumbing (7)

- [ ] **`executor/main.py:36`** — no command writes to the journal unless `--db` is passed. A pre-market plan that journals rows into a database nobody named is a surprise, and the ledger is the experiment's primary artifact. ([source](../executor/main.py#L36))
- [ ] **`executor/main.py:40`** — `watch` reads the plan from `--plan` rather than rebuilding it. ([source](../executor/main.py#L40))
- [ ] **`executor/main.py:750`** — a sighting whose plan row is missing from the journal is still written — it is real evidence that the setup was sighted — but it is reported loudly, because an unattributed counterfactual cannot be traced back to the decision that produced it. ([source](../executor/main.py#L750))
- [ ] **`executor/main.py:808`** — `--offline` is a *transport* swap, not a second code path. ([source](../executor/main.py#L808))
- [ ] **`executor/main.py:887`** — the direction comes from the contract's OCC right, so arm B's put cell and call cell are told apart by what was actually bought. ([source](../executor/main.py#L887))
- [ ] **`executor/main.py:915`** — the pre-market plan is what runs before the session open, so a decision journaled at or after 09:30 ET is intraday machinery and is skipped. ([source](../executor/main.py#L915))
- [ ] **`executor/main.py:948`** — the EMA is taken over the full daily series *including* the session being judged — the same series the checklist's T1 reads, so the exit rule and the entry rule cannot disagree about what the 50EMA is. ([source](../executor/main.py#L948))

### other (7)

- [ ] **`executor/backfill_flow.py:285`** — the grid step is a **parameter, not a fact about the world**. ([source](../executor/backfill_flow.py#L285))
- [ ] **`executor/backfill_flow.py:294`** — strikes are snapped to a multiple of `strike_step` by `ceil(lo/step)*step` and emitted while `<= hi`, so the span is symmetric to within one step and contains no strike outside the window. ([source](../executor/backfill_flow.py#L294))
- [ ] **`executor/backfill_flow.py:364`** — the contract is built by *parsing the OCC symbol back* rather than by remembering what we asked for, so strike/right/expiry in the row come from the same parser the live path uses. ([source](../executor/backfill_flow.py#L364))
- [ ] **`executor/backfill_flow.py:559`** — the message keeps the exception type and text (Alpaca's HTTP status and body are the whole diagnosis — see the feasibility note's §5), and the run continues. ([source](../executor/backfill_flow.py#L559))
- [ ] **`executor/backfill_flow.py:634`** — the honest-empty rule, inherited from the live soak. ([source](../executor/backfill_flow.py#L634))
- [ ] **`executor/iv_backfill.py:350`** — this is a **reconstruction of a listing calendar, not a record of one.** Alpaca serves no point-in-time chain, so the calendar cannot be read back for a past session. ([source](../executor/iv_backfill.py#L350))
- [ ] **`executor/iv_backfill.py:406`** — $1-wide is SPY's listed near-ATM spacing, and the width is sized to the GATE rather than to caution -- see DEFAULT_LADDER_RANGE_PCT for why that is 0.75% and not 3%. ([source](../executor/iv_backfill.py#L406))

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
| 9 | *(added 2026-10-03)* **R1** — what a rolled arm-C position rolls INTO | **RULED 2026-10-03 (operator).** The next expiry in the 90–180 DTE band, strike nearest delta 0.80, within arm C's existing premium cap. A roll with no qualifying contract returns *nothing* and is **reported** — never a naked close. See below. |
| 10 | *(added 2026-10-03)* **R2** — when the watch loop may act on a green plan | **RULED 2026-10-03 (operator).** A green plan arms the loop on the **entry window alone**; the pre-market checklist is the only gate and it is not re-evaluated intraday. See below. |
| 11 | *(added 2026-10-03)* **R3** — T6's intraday flow policy | **RULED 2026-10-03 00:43 EDT (operator-ratified, NOT delegated).** `carry_forward`. See below. |
| 12 | *(added 2026-10-03)* **R4** — T5 tenor keying | **RULED 2026-10-03 (operator).** Rolling-DTE keyed, bucketed at 7 days. See below. |
| 13 | *(added 2026-10-03)* **R5** — which arms open in November | **RULED 2026-10-03 (operator).** Arms **A + C** at window open; **B is inert** until T5 has enough observations to warm. See below. |
| 14 | *(added 2026-10-03)* **R6** — arm C's entry window | **RULED 2026-10-03 01:38 EDT (operator-ratified, accepting the lead's recommendation).** Arm C declares `entry.window_et: 09:45–15:30 ET`, inclusive. It bounds NEW entries only — R1's roll is not window-gated. See below. |
| 15 | *(added 2026-10-03)* **R7** — the 28 deployed IV rows under the old raw-DTE keys | **RULED 2026-10-03 01:38 EDT (operator-ratified, accepting the lead's recommendation):** orphaned by design. Warm-up restarts from zero under the rolling-DTE keying. No re-keying, no migration, no deletion. See below. |
| 16 | *(added 2026-10-03)* **R8** — T5's tenor-key strike axis | **RULED 2026-10-03 ~14:44 EDT (operator, ratifying both items together):** the absolute `$5` strike bucket is replaced by a **moneyness-relative** bucket at 2% of spot. Strategy version `1.3.0-draft` → `1.4.0-draft`. See below. |
| 17 | *(added 2026-10-03)* **R9** — do BACKFILLED historical observations count toward T5's 60? | **RULED 2026-10-03 ~14:44 EDT (operator, ratifying both items together):** **yes.** Frozen T5 "observations" is unqualified, so a backfilled historical observation that occupies the same key counts. See below. |

### R3 — T6's intraday policy (operator-ratified 2026-10-03 00:43 EDT)

The one ruling here the operator ratified **personally** rather than through the lead's
recommendation: mads, on seeing the recommendation, said *"your recs are good with me"*.
That is recorded as operator-ratified and not delegated, because it was.

T6's gate data is EOD-confirmed while the thing it gates — an arm B entry — is
intraday. **Default: `carry_forward`.** The newest confirmed row from a session *before*
this one is this session's gate input.

`next_day_only` remains a live switch: the gate resolves only from the immediately
preceding session, and a gap (holiday, failed soak) leaves T6 PENDING rather than
carrying an older regime forward. Flipping the default is a one-line YAML change plus a
strategy-version bump.

**The day-stale caveat stays on the record**, because it is the cost of the ruling and
not a detail: the carried ratio is one session stale, so T6 is a *prior-session regime*
filter, not a same-session flow read. Nothing about the carried row describes today's
flow. `executor/flow_gate.py` records `sessions_stale` on every read, so the staleness is
**measured in the journal** rather than asserted here.

**Revisit at the first monthly review**, once the baseline has enough sessions to say
whether carry-forward is behaving — i.e. whether the call side's P90 (1.157) and the put
side's (4.024) describe a distribution stable enough for one multiplier applied to
whichever side is traded.

### R1, R2, R4, R5 — encoded 2026-10-03, brief summary

**R1 (roll replacement).** `executor/position_manager.arm_c_roll_replacement` picks the
*earliest* qualifying expiry in the 90–180 DTE band, then the strike whose delta is
nearest 0.80 (ties broken by the lower strike), rejecting any candidate below the entry's
own `delta_min`, priced on the **ask** (a buy costs the ask; the bid-side exit mark would
understate the spend and let a cap-breaching roll through). No qualifying contract →
`None`, and `PositionManager.evaluate_exits` emits a **note** naming the fired trigger and
saying the position is `HELD, not rolled`. Before this, that case and a healthy position
both returned an empty action tuple — a stuck roll was invisible. The roll action carries
the full selection (`expiry`, `dte`, `strike`, `delta`, `target_delta`, `premium_usd`,
`cap_usd`) in `gov_checks["selection"]`, and the post-roll position adopts the *new*
contract's Greeks — otherwise the next tick re-reads the old delta and re-triggers the
roll that just fired.

**R2 (green-plan arming).** `_cell_plan_green` reads the plan's own pre-market checklist
(`checklist.fire`) rather than re-evaluating it: the point is "the plan was green at
08:30", a fact about then, not a second opinion about now. A green cell arms exactly **one**
intraday re-verification per cell per session, inside the entry window.

> **Journal-volume constraint, documented rather than solved by fiat.** The re-verification
> fires the entry governors again, which return `NO_TRADE` `VETO`s on the tick — and
> `checklist_failure_histogram()` counts `NO_TRADE` decisions. Re-verifying on *every*
> watch tick would therefore write a failing row per tick and swamp the histogram with
> rows that are not failures but "already checked". The one-per-session budget bounds it;
> if the histogram starts showing re-verification noise, the fix is to tag those rows
> distinctly rather than to raise the budget. **This throttle is an interpretation, not a
> ruling** — R2 said "arm on the entry window alone" and said nothing about frequency. It
> is a one-line change in `watch_loop._green_cell_armed` if the operator wants it lifted.

> **CLOSED 2026-10-03 01:38 EDT — R6, arm C declares an entry window.** Arm C now
> carries `entry.window_et: 09:45–15:30 ET`, inclusive on both ends, and the field is
> **required** by the loader: a rulebook missing it is a load error, not a silent
> fallback. The whole-session placeholder (`09:30`–`16:00`) is gone, and a missing
> window is now read as **CLOSED** rather than open — the old fallback was a policy
> nobody had voted for. See "R6 and R7 — encoded 2026-10-03" below.

**R4 (rolling-DTE tenor).** `iv_rank.dte_tenor_key()` floors DTE to a 7-day bucket
(`DTE_BUCKET_DAYS`, configurable at `checklist.t5_options_chain.dte_bucket_days`) and
emits keys like `dte98-call-450.00`. This is load-bearing, not cosmetic: the old key used
raw integer DTE, so **every session minted a brand-new key** and the rolling IV series
could never accumulate `MIN_OBSERVATIONS`. `tenor_key_mode` in the rulebook selects
between the expiry-keyed (`tenor_key`) and rolling-DTE (`dte_tenor_key`) modes; the hunt
plan and the watch loop's `_watch_iv_rank` both read it and pass the same bucket width, so
plan-time and intraday cannot disagree about which cell a contract lives in.

**R5 (November arms).** `window.arms: [A, C]` — arms active at window open on
`2026-11-02`. `Rulebook.inert_arms()` reports **B** as deferred: it is *not* silently
dropped, and the watch loop skips deferred arms by name instead of trading them blind.
Arm B opens automatically once T5 reaches `MIN_OBSERVATIONS` — **no new ruling needed**,
because arm C's IV rank now uses the same rolling-DTE key as arm B, so T5 warms from the
same series that arm B needs.

### R6 and R7 — encoded 2026-10-03 01:38 EDT (operator-ratified)

Both arrived together, on the same message: mads, shown the two items the full-cycle
rehearsal left open, said *"good with your recs"*. Recorded as **operator-ratified,
accepting the lead's recommendation** — the recommendation is mine, the ruling is his,
and the difference is the only thing that makes this section worth having.

**R6 — arm C's entry window is DECLARED: `09:45–15:30 ET`, inclusive.**
`arms.C.entry.window_et` in the rulebook; `WindowET` is a required field on
`ArmCEntry`, so a rulebook without one does not load. Arm C gets a real boundary for
R2's "the entry window alone", and the boundary is checked in the two places it can
bite:

- `position_manager.arm_entry_window_open(arm, now, rules)` — one helper, used by the
  arm B governor, the arm C governor, **and** `watch_loop._green_cell_armed`, so R2's
  arming path and the entry governors cannot disagree about where the edge is.
- `PositionManager._arm_c_entry` returns a named `entry_window` **VETO** with the
  declared window and the clock in its checks. This is the path R2's arming does *not*
  cover: a cell with a live price trigger reaches the manager whatever the clock says,
  so without this governor a declared window would bind only one of the two ways into
  an entry.

The hunt plan carries `entry_window_et` in arm C's `arm_c_criteria` alongside arm B's,
so a journalled cell explains its own timing at the monthly review.

**The window bounds entries, and only entries.** R1's roll is not gated by it. A
position already past its roll trigger (delta < 0.70 or DTE < 45) is managed whenever
the loop runs, including after 15:30, because a leg that cannot be rolled until the next
morning is a different risk from one that is never rolled at all. The exits ladder is
evaluated before any window question, and two tests pin that boundary: the roll fires at
09:30, 11:00 and 15:45 alike, while a *new* arm C entry at 09:35 or 15:45 is vetoed.
This reading is recorded as an `# INTERPRETATION:` in `position_manager._arm_c_entry` —
the operator ruled the window, the exemption for rolls is the one judgment call inside it.

Strategy version `1.1.0-draft` → `1.2.0-draft`: a rulebook change that binds an arm.
Status stays **DRAFT** and T6's multiplier stays `calibration_pending` — nothing about
the arms being settled changed here.

**R7 — the 28 deployed IV rows under the old raw-DTE keys are orphaned by design.**
They were written before the rolling-DTE ruling (R4). The store keeps them; nothing is
re-keyed, migrated, or deleted. The dead keys simply stop being read: under
`tenor_key_mode: dte_tenor_key` the T5 series for the bucket keys starts from zero and
warms again from the next soak session, and the old raw-DTE rows are never read by
anything.

The cost, stated rather than buried: **arm B's T5 warm-up restarts from zero.** T5
reports PENDING until the bucket keys reach `MIN_OBSERVATIONS = 60`, which under R5
means arm B stays inert at window open and arms A + C only. That was already the
post-R4 state — this ruling does not make the warm-up longer, it declines to buy 28 rows
of a series that cannot accumulate under a key that changes every session. Re-keying them
would mean a migration script touching a live store for rows that could never be trusted
as one continuous series anyway.

### R8 and R9 — the T5 key schema, RULED 2026-10-03 ~14:44 EDT (operator)

**Both arrived together, on the same message, and are recorded together because the
feasibility study said they only make sense together.** mads, having read
`docs/reviews/2026-10-03-iv-backfill-feasibility.md` and the round-2 consensus in
`docs/reviews/2026-10-03-rulings-debate.md`, ruled on both items at once: the tenor key
moves to moneyness-relative buckets, and frozen "observations" includes backfilled
history. The recommendation was the lead's; the ruling is the operator's.

**R8 — the rolling tenor key's strike axis becomes MONEYNESS-RELATIVE, at 2% of spot.**
`MONEYNESS_BUCKET_PCT` in `executor/iv_rank.py`; keys are namespaced
`mte<dte>-<side>-mny<bucket>` (`iv_rank.moneyness_tenor_key`).

The arithmetic that made this a ruling rather than a preference: under the absolute `$5`
axis, **no key reaches `MIN_OBSERVATIONS = 60` at any depth of history** — 22
observations best-case inside the frozen 1-year lookback, 25 across the entire
718-session backfill (feasibility doc §2b). The strike axis rotates with spot, so an ATM
contract's series changes every few percent and never accumulates. At 2% moneyness the
same study measures **210 observations** inside the same frozen lookback — the only
variant of the five tried that clears the floor with room. This is the same failure R4
fixed on the DTE axis, still live on the strike axis.

Two properties of the implementation are load-bearing and are pinned by tests:

- **The DTE axis is untouched.** R4's 7-day bucketing stands; only the strike axis moved.
- **The keys are namespaced, not merely renamed.** Nothing in the store records which
  bucketing produced a `tenor_key`, so a schema that reused the old spelling could
  silently mix a "roughly ATM" series with a "$650 strike" series. Under `mte`/`mny`,
  old rows are simply never looked up by a new reader — the R7 disposition obtained by
  construction, with no migration script and no deletion.

**What this costs, stated rather than buried: T5's warm-up restarts from zero, again.**
That is the second time the same 60-observation floor has been restarted, and it is the
price of a schema that can ever reach it. Every row written under the R4 dollar axis is
orphaned by this ruling. Nothing is re-keyed.

**What did NOT move:** `MIN_OBSERVATIONS = 60`, `iv_rank_lookback: "1y"`,
`iv_rank_max = 50` for both arms, `dte_bucket_days: 7`, and every other threshold in the
rulebook. The feasibility study's own finding is that **no longer lookback was ruled**,
so the ~1y requirement still discards history older than a year — R9 is what makes the
history inside that year count.

**R9 — frozen T5 "observations" includes BACKFILLED historical observations.**
The frozen text says *observations* (`docs/brief.md` §T5), *history*
(`iv_rank_lookback`), and `MIN_OBSERVATIONS` observations in the tenor key
(`docs/ratification.md` §T5). None of it says *live-recorded* or *EOD-polled*. So a
backfilled observation occupying the same key, under a defensible `source`, counts
toward the 60.

The alternative reading — that `iv_rank.py`'s "one EOD poll per day" line is part of the
freeze — was on the table and was declined by ruling, not by oversight. Its cost is
real and is stated here so it cannot be discovered later: a backfilled IV is a
Black-Scholes inversion of a daily bar close, while a live one is a model output read off
a chain snapshot. **Mixing them inside one percentile series is a methodological splice,
not a single measurement**, and the build that does it (a separate, later run) must
carry the provenance to make that visible: a new `source` value, an `origin='backfill'`
marker, and the same delayed-indicative-feed labelling the live soak writes. Reusing
`is_proxy` is explicitly refused — that bit means "Cboe VIX, a different index", and a
bar-inverted SPY IV is not a different index, it is the right index built the wrong way.
Reusing `manual` is refused for the same reason: that column exists to keep provenance
honest.

**Fresh NO-SHOT baseline, by version query filter — not by deletion.** Every draft row
stamps `strategy_version` (`noshot.build_noshots`), and `config_sha256` moves with this
rulebook edit because `Rulebook.fingerprint()` hashes raw file bytes. The baseline for
`1.4.0-draft` is therefore isolable with a version filter, exactly as R7 did it: nothing
is deleted, and rows written under `1.3.0-draft` stay on disk and stay readable.

**What was deliberately NOT done here, and has since been done.** When this ruling was
entered, section (e) item 1 still described the strike-axis rotation as "queued for the next
pre-registration cycle", and (e)/(f) were left alone on purpose: the rewrite has to state
**real** observation counts for the new schema, not hopes, and those counts did not exist
yet. **The backfill has since run (`a988b40`: 342 rows, 0 refusals, arm C PASS) and the
reconciliation has landed.** Section (e) item 1 is now written as *fixed under R8, verified
warm at 78 put / 68 call observations, with the density caveat stated*, and (e) carries a
*Revision record* withdrawing its structural reason in the same voice it was written in.
Section (f) is revised alongside it. Nothing in this ruling moved; only the document that
recorded the open question has been closed against real numbers.

---

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

**Not a calibration, but open:** arm C has no `entry.window_et`, so R2's "arm on the entry
window alone" has no boundary for arm C (currently treated as the whole session). See the
R2 note above. Operator question.

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
- [x] **IV warmup actually reached — for arm C, achieved; for arm B, unreachable by design.**
      `MIN_OBSERVATIONS = 60`. **Arm C reads PASS** on the ruled moneyness keys (78 put / 68
      call observations, `source=bars_bs_inversion`, `origin=backfill`, per R9), so T5 no
      longer blocks arm C at window open. **Arm B reads PENDING and will stay there:** its
      band is 0 DTE, Black-Scholes vega is identically zero at expiry, and the solver refuses
      zero-DTE outright — arm B warms on **live soak only**, ≥ 60 sessions (≈ 2027-01-23 from
      window open). Its `window.arm_b_gate` is armed and opens on the count, with no new
      ruling. *Re-verify both through the production reader before the window; a PASS backed
      by a count nobody re-read is not a PASS.*
- [x] ~~**Which tenor T5 reads**~~ — **RULED 2026-10-03 (R4): the rolling
      `dte_tenor_key`**, bucket 7 days, both arms. *Consequence ratified the same day
      (R7): the 28 deployed raw-DTE rows are orphaned, the store is not rewritten, and
      T5's warm-up restarts from zero — so this box is ticked with a known cost, not
      closed for free.*
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

## (e) Empty-window pre-commitment — DRAFTED 2026-10-03, REVISED 2026-10-03, AWAITING OPERATOR SIGNATURE

> **Drafted 2026-10-03, revised the same day after the T5 backfill, still awaiting operator
> signature.** This is section (e), added after the `2026-10-03` frozen-rule rulings debate. It
> is frozen-rule territory, so the text below is exact and the operator ratifies it as written
> or not at all — it is not "lead-ratified under delegation" the way section (a) is. Signature
> line at the bottom.
>
> **Why it carries a revision, stated up front rather than applied silently.** The structural
> reason this section originally led with — *arm C cannot trade in this window at all, and
> therefore P(trade) ≈ 0 deterministically* — **is withdrawn. It is false.** Rulings R8/R9 and
> the completed backfill (`a988b40`, 342 rows, 0 refusals) put arm C's T5 warm on a real IV
> series. The base-rate reason survives and now carries the section alone. See *Revision
> record* at the bottom, which is part of this section and not an appendix to it.

**The one thing this section closes.** Every other control in this document defends
against *editing* the rules — version bumps, monthly review only, journaled seams. None of
them stops the cheaper failure: a quiet window, someone correctly noticing the rules look
too tight, and a mid-flight loosening framed as "we learned the threshold was wrong." That
response would destroy the only asset this experiment has that a normal trading system
does not. **This is the only pre-commitment that closes it, which is why it is written
before any results exist rather than after.**

**An empty window is the designed outcome, for one reason. It is stated here so it cannot be
discovered later and mistaken for good news.**

**(1) Base rate — the checklist almost never fires, and the window is built around that fact
rather than hoping against it.** Arm C fires on **5 of 5,522** SPY sessions in 22.7 years.
P(at least one qualifying day in a 60-session window) = **5.3%**; P(empty) = **94.7%**
(`docs/reviews/2026-10-03-gate-base-rates.md`). Expected trade count over the window:
**~0.05**. In plain terms: one qualifying day, if one occurs at all, is a ~5% event. The
rulebook's own line on arm B — *"weeks with zero trades is the system working"* — is not a
special condition to be relieved of. It is the expected output.

> **A note on the measurement's one soft input, which is now load-bearing rather than
> decorative.** The 5.3% / 94.7% figure scored T5 with a Cboe VIX percentile-rank **proxy**
> (its own Limitations §2), because at the time T5 had no series to read. **That proxy is now
> retired** — arm C's T5 reads a real SPY IV series. The base-rate measurement has **not** been
> re-run against it, so T5's marginal inside the joint rate is an assumption carried forward
> from a stand-in. Under the proxy, T5 recorded **zero sole-vetoes** — it never vetoed a day
> that everything else passed — which is why its 55% pass rate cannot move the joint rate much.
> **That argument is weaker now than it was**, because "T5 does no filtering" was itself
> measured with a substitute, and the real T5 could bind on days the proxy let through. So:
> **94.7% is the best available estimate, not a re-measured number and not a floor.** What
> survives untouched is the qualitative claim it supports and the order of magnitude — one
> qualifying day per few thousand sessions is not sensitive to one gate's marginal. Anyone
> quoting a sharper number than that is quoting something this document does not support.

**The pre-commitment:**

> An empty or single-trade window is the **expected, pre-registered outcome**. It is
> **INCONCLUSIVE** — not a failure, not a bug, not evidence that the gates are broken, and
> **not evidence for loosening them**. **No gate or threshold changes mid-window.** The
> window is not extended or re-tuned in response to its trade count; extension remains a
> monthly-review decision per `window.extension`, made on schedule rather than on results.
>
> **Queued for the next pre-registration cycle (v2 design), explicitly NOT for mid-flight
> amendment:**
> 1. **T5's tenor key rotates with spot — FIXED under R8, verified warm, not closed.**
>    *The original finding, retained for the record:* `dte_tenor_key` embedded a `$5` strike
>    bucket, so the key changed as SPY moved — the same failure R4 fixed on the *DTE* axis,
>    still live on the *strike* axis. Measured on real SPY closes: **no key reached 60
>    observations since 2020** (best 54). R8 moved the rolling key's strike axis to
>    2%-of-spot moneyness buckets (`mte<dte>-<side>-mny<bucket>`), and R9 ruled that backfilled
>    observations count toward the frozen 60. Those rulings are the authority for what follows
>    and are not re-litigated here.
>
>    **What the store now holds**, read through the production reader:
>    `mte91-put-mny+0.00` **78** observations, `mte91-call-mny+0.00` **68**, floor
>    `MIN_OBSERVATIONS = 60`, every row `source=bars_bs_inversion` / `origin=backfill`.
>    **Arm C's T5 reads PASS.** (Raw distinct-session counts before the 365-day lookback and
>    the proxy-row exclusion are 90 put / 81 call; the reader's numbers are the ones that
>    count.)
>
>    **The caveat that has to travel with those numbers, because they are short.** They are
>    roughly **30% of the 261/key the offline arithmetic predicted**, and the shortfall is a
>    **contract-reconstruction fidelity limit, not a fetch failure**: 191 of 283 sessions
>    wrote nothing at all (`no_contract_with_a_bar` — 0 of ~21 requested contracts returned a
>    bar), and sessions that did resolve averaged 3.3 contracts with a bar. The ladder is a
>    *reconstruction*, since Alpaca serves no point-in-time chain, so it asks for strikes
>    around spot that were frequently never listed or never traded — and **a bar is not a
>    backfilled guess**. So this is a **warm series with a real margin over the floor, not a
>    full-density series**. Any later argument about T5's sensitivity, decay, or distribution
>    has to reason from 78/68, not from the 261 the arithmetic hoped for.
>
>    **The expiry-keyed audit series is still rotation-limited, by design.** The parallel `$5`
>    expiry-keyed keys (`20260116-put-655.00` and its siblings) are the exact-contract audit
>    trail for the approximation, and they top out at **3–4 sessions** against the same floor
>    of 60 — every absolute-strike key dies with its expiry. That is precisely why they are
>    audit-only and not a gate input. If that ever changes it needs its own ruling, the way
>    R8 got one.
> 2. **T1, T2 and T5 are redundant** at daily resolution — zero sole-vetoes, 100% pass
>    given the others pass. T4 does nearly all the filtering. (Re-measured only under the T5
>    proxy; the T5 half of this row is now carried by the real series, and the row is queued
>    for re-measurement in v2 for that reason.)
> 3. **T2b vs T3b friction** — the outsider review argues these charge twice for one
>    observation. Both are binding, so neither is removable without weakening confluence.
>
> Items 1–3 are recorded **now, pre-window, before any results exist**, so that a later
> reader sees them as *queued* rather than as *discovered after the window went quiet*.

**Journal/UI wording.** The correct note on an empty session is: *"base rate predicts 0.05
trades per window; 0 trades is unremarkable."* Not *"checklist is too tight."*

**Revision record — why reason (1) changed, in the words of the claim being withdrawn.** The
first draft of this section gave two reasons for the expected-empty outcome. Reason (1) was
**structural**: T5 is a blocking condition on arm C's frozen checklist, PENDING blocks,
`checklist.evaluate` computes `fire = not any(blocking and status != "PASS")`, an IV rank
needs 60 observations in the tenor key, and the tenor series had not warmed — therefore
**P(arm C takes a position in this window) ≈ 0, deterministically**, and that was ranked above
the base rate because it is structural rather than statistical. **That claim is withdrawn: it
is false.** R8 moved the key off the rotating strike axis, R9 made backfilled observations
countable, and the completed backfill cleared the floor on both ruled keys. **Arm C is
gate-capable and can trade.** What remains is the base rate, which is an expectation and not
a certainty.

The withdrawal was made **pre-window, with no trades taken and no results to fit** — which is
the only moment a claim like this can be retracted without contaminating anything. It is
recorded here in the same voice as the original, because a pre-commitment that quietly loses a
clause is not a pre-commitment, it is a draft. **What did not change: the pre-commitment
block above, which is in force verbatim and independently of either reason. Even if the base
rate were exactly wrong, the no-mid-flight-loosening rule and its reasoning still stand on
their own.**

**Operator signature:** ______________________  **Date:** ____________

---

---

## (f) E1 — November window scope — DRAFTED 2026-10-03, REVISED 2026-10-03, AWAITING OPERATOR RULING

> **This is a draft, not a ruling.** Both the lead and the outside reviewer independently
> converged on it, which is why it is written as exact text rather than a recommendation.
> The operator rules on it. It is placed in the frozen document so that it is signed
> **before the window opens** — the whole value of an escalation is that it cannot be
> reverse-engineered from results.
>
> **Revised 2026-10-03 after the T5 backfill.** The reviewer's structural argument — arm C
> cannot trade, therefore nothing is evaluable — **no longer holds, and the revision changes
> the reasoning and one prediction's status, not the deferrals.** Recorded here pre-window,
> with no results in hand.

**The finding, as it now stands.** The window opens arms A and C (`window.arms = [A, C]`),
with arm B held behind `window.arm_b_gate` — `armed_pending_warmup`, opening automatically
when a tenor the plan reads reaches `MIN_OBSERVATIONS`, with no new ruling needed. **Arm C is
now gate-capable:** its T5 reads PASS on a real, backfilled IV series, so nothing structurally
prevents it from trading. What is left is arithmetic: the measured base rate is ~0.05 trades
per window, so **arm C almost certainly will not trade either** — for a completely different
reason than the one this section originally recorded. A window with no arm-C trade can no
longer be read as "the gate was blocking" and it also cannot be read as "the gate was fine and
nothing came along"; **the journal cannot tell those two stories apart, and a reader who is
not told that will pick whichever one flatters the conclusion they wanted.**

**Prediction by prediction, reassessed against the current state:**

**Prediction 1 — arm A finishes the window positive in expectation.** **Unchanged, and it is
now the window's one confidently scored comparison.** It needs no gate, no feed and no
warmup, and over 60 sessions it yields an answer with ~95% confidence of existing at all.
Nothing in the backfill touches it. *(Listed so the record shows it was re-read rather than
assumed.)*

**Prediction 2 — arm B is structurally negative-EV as an asset class. STILL NOT EVALUABLE.
The deferral stands and the criterion is unchanged; the reason is now a different reason, and
the difference matters for what happens next.**

- *It was deferred because* T5 could not reach `MIN_OBSERVATIONS = 60` in the tenor key
  before the window by any route: the absolute-strike key rotated with spot and backfill hit
  the identical wall (feasibility study §2b/§5 — ≤ 22 observations inside the frozen 1y
  lookback, best case). Arm B's warmup was **impossible before the window**.
- *It is deferred because* **arm B's band is 0 DTE.** Black-Scholes vega is identically zero
  at expiry, so a 0-DTE daily print does not determine a volatility, and the inversion solver
  refuses zero-DTE contracts outright. **Arm B's T5 therefore cannot be backfilled at all, at
  any depth of history** — that is the instrument, not a data gap. It warms on **live soak
  only**, at roughly one observation per session: **≥ 60 live sessions, which the feasibility
  arithmetic puts at ≈ 2027-01-23 measured from window open.** Live rows recorded before
  window open would pull that earlier, and the gate opens **on the observation count, not the
  calendar** — so this is a warm-up arithmetic, not a date, and nobody should be tempted to
  treat a particular Monday as the trigger.
- **A second, independent blocker, recorded so the first is not mistaken for the whole
  story:** arm B also carries **T6**, which arm C does not. T6 is `calibration_pending` until
  N is frozen at the first monthly review on ≥ 20 soak sessions (§(c)), and a PENDING gate
  blocks. **A warm T5 alone would not open arm B before that freeze.**

**Escalated for ruling, prediction 2:**

> **E1(a-i) — Prediction 2 is NOT EVALUABLE IN THIS WINDOW.** It requires arm B trades, and
> arm B cannot take one: its T5 warms on live soak alone (0 DTE, zero vega, no backfill
> possible) at ≈ 60 sessions from window open, and its T6 is `calibration_pending` until the
> first monthly review. It is **deferred to the next pre-registration cycle (v2)**. Its
> criterion is **unchanged** — deferred, not rewritten to fit what the window produced. No new
> ruling is required for arm B to open if its series warms: `window.arm_b_gate` is already
> armed for exactly that.

**Prediction 3 — arm C tracks arm A with leverage-amplified variance. NOW EVALUABLE IN
PRINCIPLE AND UNDERPOWERED — and the honest status is neither "deferred" nor "tested."**

**Escalated for ruling, prediction 3:**

> **E1(a-ii) — Prediction 3 is EVALUABLE IN PRINCIPLE AND NOT POWERED.** Arm C is
> gate-capable: its T5 reads PASS on a real series, so a trade is mechanically possible in
> this window. It is not *expected* — the measured base rate is 5 of 5,522 sessions, ≈ 0.05
> trades per 60-session window. **A zero-trade window therefore cannot distinguish "arm C
> tracks arm A with leverage-amplified variance" from "arm C never fired": the two are
> identical in the journal.** The prediction is neither confirmed nor refuted by a quiet
> window; it is **inconclusive by construction, and is recorded as inconclusive rather than as
> a pass.** Its criterion is **unchanged**. One further honesty note, so a lucky trade is not
> oversold either: **if exactly one arm-C trade occurs, that is a single position, and one
> position cannot establish variance amplification.** It yields a directional observation and
> is recorded as *not the pre-registered test* — the same rule that keeps a lucky window from
> being cited as confirmation applies to a lucky trade.

**Prediction 4 — trade frequency under hunting discipline; *"if we're trading weekly, the
rules were not frozen tight enough."* THE VACUITY CONCERN SURVIVES, and it survives for a
sharper reason than before.**

- *Originally:* the checklist could not fire at all, so an empty window satisfied prediction 4
  without testing anything.
- *Now:* the gate **can** fire and the window is still almost certainly empty, so the same
  vacuity arrives by a different road — and it is arguably worse for interpretation, because
  **a quiet window is now consistent with two incompatible stories**: the gates are correctly
  frozen and the base rate is simply what it is, or something is silently preventing arm C
  from trading. The window's own output cannot separate those. **The claim "< 2 trades/month"
  is satisfied by zero trades and by one; the only informative branch is the weekly-trading
  one, and §(e) forbids reacting to it anyway.** Recording prediction 4 as passed would hand a
  later reader a result that appears to support both stories at once.

**Escalated for ruling, prediction 4:**

> **E1(b) — Prediction 4's in-window result is recorded as VACUOUS, not confirmatory.**
> Prediction 4 falsifies itself if "the rules were not frozen tight enough"; an empty window
> satisfies that test without testing anything, and now satisfies it without even being
> evidence that the gate *tried*. It is recorded as *not run*, so a later reader cannot cite
> "the rules passed prediction 4" as support.

**Escalated for ruling, the window's purpose:**

> **E1(c) — The window's stated purpose is amended** to: **score arm A against cash at ~95%
> confidence; treat arm C as a live-capable arm that is expected *not* to fire; and keep
> collecting mechanism evidence** — NO-SHOT rows, the IV series (whose live-provenance rows
> gradually replace the backfilled ones inside the frozen lookback), and the flow baseline that
> freezes T6's N. This is a legitimate thing to run and worth running, but it is a *different*
> experiment than the brief described, and the difference should be on the record before day
> one rather than discovered on it.
>
> **One correction to the original wording of this clause, made because it was wrong rather
> than merely stale:** T6's flow baseline is **arm B's**, and arm C carries T1–T5 only — T6 is
> not an arm-C mechanism input at all. The baseline is collected because it is what freezes N
> at the first monthly review, not because anything in this window reads it. Listing it
> alongside arm C's evidence overstated what arm C does.

**What E1 does NOT do.** It does not change a threshold, a gate, or a rule. It does not
extend or shorten the window. It does not alter what arm A does. It does not remove or re-arm
any arm — in particular it does **not** touch `window.arm_b_gate`, which stays armed on its
own terms. It is a statement about what the window can and cannot conclude — which is exactly
the kind of claim that is cheap to make now and impossible to make honestly later.

**Operator signature:** ______________________  **Date:** ____________

---

## Appendix: what this document is not

- It is **not** a claim the code is correct. The tests (527 passing at time of writing) are
  evidence of a different kind, and passing tests do not ratify a threshold.
- It is **not** a substitute for `docs/brief.md`. Where they disagree, the brief is the
  specification and this is the record of how the gaps were filled.
- It is **not** self-updating for sections (b), (c), or (d). Only (a) is generated; the
  rest is the operator's document, and an assistant that rewrote it would be rewriting the
  parts that are supposed to be yours.