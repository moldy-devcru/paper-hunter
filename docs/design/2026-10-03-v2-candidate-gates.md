# v2 candidate gates — design fuel, not rulings

**Date:** 2026-10-03. **Status: CANDIDATES FOR THE v2 PRE-REGISTRATION CYCLE. NOT
RULINGS. NOT IMPLEMENTED. NOT IN ANY RULEBOOK.**

mads shared a personal OTM method on 2026-10-03. Three of its observations look like they
could be encoded as checklist conditions, and this note writes them down so the v2 cycle
starts from something concrete rather than from memory. **Nothing here is proposed for
mid-window amendment** — that is closed by the pre-commitment in
`docs/ratification.md` §(e). The version seam rule from the same document applies: a v2 gate
gets its own ruling entry and its own `strategy_version`, pre-registered before any result
exists.

Read this alongside `docs/reviews/2026-10-03-gate-base-rates.md` (on
`feature/gate-base-rate-measure`). That measurement is the reason this note is framed the
way it is, and §4 below is where it bites.

---

## 0. The three, in one line each

| # | candidate | measures | capture cost | verdict |
|---|---|---|---|---|
| **C1** | Open interest as a liquidity / interest signal | whether the OTM strikes carry real positioning and a two-sided market, vs. being empty shells | parser field only — **no new API call** | strongest candidate; needs one capture-first season |
| **C2** | Same-day IV direction | momentum or mean-reversion in the arm's own tenor, day over day | **already captured** — free from the existing IV store | cheapest to evaluate, most likely to be redundant with T5 |
| **C3** | Diminishing returns across further OTM strikes | premium per unit of distance collapsing — the ladder running out of information | **new columns** on the IV observations | most interesting conceptually, most expensive to get right |

---

## 1. C1 — open interest as a liquidity / interest signal

### What it would measure

Two distinct things, which should not be conflated:

1. **Liquidity** — is there a two-sided market at the strikes being bought? An OTM contract
   with a real bid *and* a real ask at size is tradeable and its premium means something. An
   OTM contract quoted 1 wide at size 1 is a price, not a market. The current T5 gate reads
   **IV rank**, which is a statement about where implied volatility sits in its own history;
   it says nothing about whether the contract can actually be transacted at the quoted
   premium. A high IV rank on an untradeable strike is not a hostile setup, it is an
   un-measurable one.
2. **Interest / positioning** — is anyone actually holding or writing this strike? OI on the
   deep-OTM strikes is the most direct available proxy for the crowding the whole hunting
   thesis is about. It is also the one input in this note that survives the "chain snapshot
   completeness" problem that made the T6 calibration outliers untrustworthy: OI changes
   slowly and is reported daily, so a missing row is a detectable gap rather than a
   plausible-looking number.

### Candidate gate formulation

Compatible with the existing architecture: a `checklist.t7_open_interest` block with
`id: T7`, `applies_to: [B, C]`, and an evaluator returning a `Condition(id="T7",
status=…, blocking=…)` — the same three-status (PASS / FAIL / PENDING) shape every other
gate uses, so `fire = not any(blocking and status != "PASS")` keeps working untouched.

Two sub-conditions, deliberately separable so each can be measured on its own:

- **`liquidity`**: for the contract the arm would buy, `latest_quote.bid_size > 0` **and**
  `latest_quote.ask_size > 0`, and `ask - bid <= spread_max_pct` of mid. `blocking: false`
  at first — see §4.
- **`positioning`**: OI on the trade-direction side at or above a percentile of its own
  trailing distribution, mirroring T5's shape (a rank against the arm's own history, never
  a raw level — a raw OI threshold is a spot-price threshold wearing a disguise, and it
  rotates the same way the `$5` strike bucket did before R8).

**The percentile-not-level discipline is the whole ballgame here.** An absolute OI number is
worthless across a year of SPY moving 10%. Any OI gate must be a rank against a rolling
window, keyed on the same `mte…mny…` schema T5 now uses, so it inherits R8's fix instead of
repeating it.

### Data requirements

**Does the current soak capture it? No, and the good news is how little is missing.**

- `OptionContract.latest_quote` **already carries `bid`, `bid_size`, `ask`, `ask_size`**
  (`executor/alpaca_client.py`). The bid/ask half of the liquidity sub-condition is
  therefore **available today and stored nowhere** — the parse exists, the persistence does
  not.
- **Open interest is not parsed at all.** `OptionContract.from_json` reads
  `impliedVolatility`, `greeks`, `latestQuote`, `latestTrade`, `dailyBar`, `prevDailyBar`,
  `minuteBar` — there is no `openInterest` field on the dataclass.
- **Whether Alpaca's snapshot actually carries OI has not been verified in this repo.** The
  research doc says the consolidated chain returns OI and is "a strict superset of Alpaca's
  indicative chain" on that point, which implies yes, but that is an inference from a
  vendor comparison, not a field read off a live response. **Verify against a real snapshot
  before designing around it.** This is exactly the kind of claim that should not be
  designed on top of unchecked, and it is cheap to check — one API call.

Work if pursued: add `open_interest` to `OptionContract.from_json`; add an OI observation
table (or columns) alongside `iv_observations`, stamped with the same `origin` and `source`
discipline the backfill established so a capture-only season is auditable. **No new API call
and no new rate-limit pressure** — the snapshot already carries every field.

### Risks

- **Redundancy with T4 and T5 is the default expectation.** See §4. This candidate has to
  earn a sole-veto before it earns a `blocking: true`.
- **OI is a daily-close quantity.** Reading it intraday at 09:35 gives you *yesterday's*
  positioning. That is a real and invisible way for a gate to be measuring something other
  than what its name says, and the EOD-confirmed / carry-forward distinction R3 built for T6
  applies here too.
- **Overconcentration in one expiry distorts the aggregate.** Total OI across a chain is
  dominated by whichever expiry everyone is rolling. Read OI **per contract at the arm's own
  tenor**, never chain-wide, or the signal is mostly an expiry-calendar artifact.
- **Overfit surface is large**: percentile window, OI-vs-volume ratio, side selection. Three
  free parameters on a 22-year daily series is enough to manufacture a result. Freeze them
  before looking.

---

## 2. C2 — same-day IV direction

### What it would measure

Whether the arm's own tenor's implied volatility is **rising or falling into the entry**.
Two readings, and they point opposite ways:

- **Momentum**: IV rising into a long-premium buy is the regime that pays — the thesis is
  that a washout is repriced upward.
- **Mean reversion**: IV rising into the buy means paying up, and T5's existing ceiling
  (`iv_rank_max = 50`) is already a *level* constraint on exactly this. A direction
  condition would be the derivative of what the level gate already partly does.

This candidate is the one most likely to be redundant, and §4 says why with numbers.

### Candidate gate formulation

```yaml
checklist:
  t5b_iv_direction:
    id: T5b
    applies_to: [B, C]
    source: same_tenor_as_t5          # never a different tenor — that is not corroboration
    lookback_days: 1                  # prior session's observation of the SAME key
    call_bias: iv_delta_min           # rising into a call buy
    put_bias:  iv_delta_min
    units: percentage_points          # NOT percent-of-percent
    blocking: false                   # measured first, promoted only on evidence
```

Placed as `T5b` rather than `T7` deliberately: it is the same evidence T5 already reads
(the same store, the same tenor key), so it belongs beside T5 where the redundancy question
can be asked out loud. Splitting it into an unrelated `T7` would hide that it is T5 plus a
first derivative.

### Data requirements

**Already captured. This is the one thing on this list that costs nothing.**

The IV store retains every observation per tenor key with an `as_of`, and
`IvRankStore.history()` returns them oldest-first. Two consecutive observations of the same
`mte…mny…` key **are** the same-day IV direction. No new column, no new table, no schema
change, and it is equally available from the R9 backfilled rows (which is a
methodological splice worth naming: the first post-backfill "direction" readings will be
comparing a backfilled historical print to a live one, and that seam should be visible in
whatever v2 builds on it).

The one thing it does *not* give you is **intraday** direction. T5b evaluated at 09:35
against an EOD store is measuring yesterday's move. If intraday direction is wanted, that
needs a real change: the soak's `minute_bar` is already fetched, so an intraday IV proxy is
*computable* from it — but computing it is new code and a new ruling about what "same-day"
means at 09:35, not a capture gap.

### Risks

- **Redundant with T5's ceiling, very likely.** `iv_rank_max = 50` already bounds how
  expensive the entry may be. T5b would have to veto days T5 passes and that the joint
  checklist would otherwise fire on. §4 measures how little room there is for that.
- **A derivative of a spliced series.** Per R9, backfilled and live observations share a
  percentile series. A *level* splice is a methodological caveat; a *first derivative* across
  the splice boundary is worse, because it manufactures a direction from two different
  measurement methods. The seam has to be marked, not smoothed.
- **Sign asymmetry risk.** Implied volatility generally ratchets up and decays slowly, so
  "rising" and "falling" are not symmetric events and a single threshold will misprice the
  rarer side. Measure both sides before picking a sign convention.

---

## 3. C3 — diminishing returns across further OTM strikes

### What it would measure

This is the most interesting of the three and the most expensive to do honestly.

The operator's observation: **as you walk further out the money, the bid/ask often stops
moving.** The quote is the same at three consecutive strikes. The natural reading is that
the ladder has run out of information — you are paying essentially the same premium for a
lot further from ATM, which means the further strikes are not "cheaper lottery tickets,"
they are the same ticket.

Formally: **premium per unit of distance is collapsing.** For a ladder of OTM strikes
`k₁ < k₂ < … < kn` on the trade side, measure

```
slope_i = (premium_i − premium_{i−1}) / (strike_i − strike_{i−1})
```

A healthy ladder has a non-trivial, roughly monotone premium curve. A flat ladder —
`slope ≈ 0` across two or more consecutive steps — is the signature the operator noticed.

### Candidate gate formulation

The architecture fit is *poor*, and saying so plainly is more useful than forcing it.

A ladder shape is not a scalar, and the checklist evaluates one boolean per condition.
Options, in the order I would consider them:

1. **Non-blocking advisory condition** — a `T8` that returns PASS/FAIL with
   `blocking: false`, recorded in the journal and visible in the histogram, never vetoing.
   Cheapest, zero risk, and it is what a candidate should be before it is ever a gate.
2. **A derived scalar with a frozen threshold** — collapse the ladder to one number (mean
   absolute slope across the arm's window, or the fraction of ladder steps with
   `slope ≈ 0`) and gate on that. This loses the shape and keeps a threshold, which is the
   usual trade and the usual overfit trap.
3. **Fold it into arm C's contract selection instead of the checklist.** R1 already made the
   roll replacement a *selection* rule ("earliest expiry in band, strike nearest delta 0.80,
   inside the premium cap"). A flat ladder is arguably a selection problem — pick the last
   strike where the ladder is still paying — not a veto problem. This may be the right home,
   and putting it here avoids adding a seventh boolean to an AND that already rejects 99.9%
   of sessions.

**Recommendation: option 1 first, option 3 as the serious contender.** Not option 2 — a
single frozen slope threshold is the least defensible of the three and the easiest to fit.

### Data requirements

**Does the current soak capture it? No — this is the expensive one.**

- Bid/ask are already **parsed** (`OptionContract.latest_quote.bid/ask/bid_size/ask_size`)
  but **persisted nowhere**. The `iv_observations` table has no quote columns.
- Nothing stores a *ladder*. Each ATM contract currently writes two rows (expiry-keyed and
  moneyness-keyed); a ladder shape needs the **full set of OTM strikes at one expiry at one
  timestamp**, which is a different shape from what the soak keeps.
- `executor.soak.aggregate_flow` already walks deep-OTM strikes for T6, so the *walk*
  exists and is tested — what is missing is persisting what it saw at each rung.

Work if pursued: a new table (ladder snapshots: `underlying, as_of, expiry, side, strike,
bid, ask, mid, iv, moneyness`) populated from the chain the soak already fetches. **No new
API call.** Storage is the cost — a full chain per session per expiry is a few hundred rows
a day, which is fine, but it is a schema migration rather than an added column and it lands
in the append-only zone the rehearsal's integrity stage protects.

### Risks

- **The signal may be mostly a data artifact.** Identical quotes across strikes is exactly
  what a *delayed indicative feed* looks like when the underlying has not moved — the feed
  has nothing new to say, so it repeats the last print. `scripts/soak.timer`'s own header
  already records that Alpaca's feed is indicative and delayed. **This candidate might be
  measuring the feed, not the market**, and that possibility has to be ruled out before the
  signal is believed — not after. The first capture season is for answering that question.
- **Quote staleness is indistinguishable from a flat ladder** in exactly the data as stored.
  Any threshold needs a freshness check on the quote timestamp, which is captured
  (`OptionQuote.t`) but currently discarded.
- **Widening-spread and flat-ladder are not the same thing.** A genuinely two-sided wide
  market is liquid-but-expensive; an identical quote across strikes is thin. These need
  different treatment and a single "premium" number conflates them.
- **Most overfit-prone of the three.** Number of rungs, which expiry, which strikes, what
  counts as "identical", what counts as "further out" — six or more free parameters, on a
  signal whose own existence is not yet established.

---

## 4. The finding that governs all three candidates

From `docs/reviews/2026-10-03-gate-base-rates.md` (arm C, 5,522 SPY sessions, 22.7 years,
rulebook `1.2.0-draft+9c5200188e0e`):

> Arm C fires on **5 of 5,522** sessions — **0.091%**, projecting **0.054 trades per
> 60-session window** and a **94.7%** probability of an empty window. T4 is the dominant
> constraint (91 sole-vetoes). **T1, T2 and T5 do no filtering at all** — 0 sole-vetoes,
> **100% pass given the others pass.**

Three consequences, stated before any of the candidates above can be argued with:

1. **A new gate cannot be measured on the conjunctive checklist.** The set of otherwise-
   green sessions is *five days in twenty-two years*. No marginal, no sole-veto count, no
   correlation coefficient can be computed on n=5. Any v2 gate must be studied the way the
   base-rate doc studied the existing ones — **as a standalone condition across all 5,522
   sessions, correlated against each existing gate** — and only then asked whether it adds
   anything inside the AND. A gate adopted without that study is a gate whose redundancy is
   unknown, and the measured precedent is that redundancy is the likely answer.

2. **T5 currently buys nothing.** Zero sole-vetoes, 100% pass-given-others-pass. T5 has
   also cost the most of anything on the checklist: two schema restarts, two warm-up
   restarts (R4, R8), a backfill, and a warm-up that will not complete inside the window
   because arm B cannot be backfilled at all. That is not an argument against T5 — it is a
   blocking condition and blocking is the point. But it is a strong prior that **C2, being
   T5's own first derivative, will be redundant too.** C2 should be expected to fail its
   measurement, and that should be recorded as a result rather than treated as a surprise.

3. **More gates make the window emptier, not better.** The window opens A + C with B inert
   (R5), and arm C's expected trade count is ~0.05. Adding three conditions to an AND that
   fires once every eleven years does not improve the experiment; it guarantees the window
   is empty and removes any chance of learning whether it was empty for a good reason.

### What that implies for the v2 cycle, concretely

- **Capture first, gate later.** The strongest argument for acting before v2 is
  pre-registered is not to make the gates fire — it is to **start recording OI, quotes and
  ladders now, non-blocking**, so that when v2 is pre-registered there is a season of data
  behind each candidate. This is precisely the reasoning that put the soak timer in place
  for T5's warm-up, and the clock argument is identical: a candidate with no capture history
  cannot be pre-registered honestly, and pre-registration is what makes the window's results
  worth anything.
- **Every candidate starts `blocking: false`.** Promotion to a blocking condition is a
  separate ruling with its own evidence, in the same way R3/R4/R5 were separate rulings.
- **The v1 window is not the validation set.** 94.7% empty is pre-registered as the expected
  outcome (§(e)). v2 cannot be validated by v1's results; it needs its own base-rate study
  on its own data, before it is frozen.

---

## 5. Open questions for the operator

1. **C3's premise depends on the feed.** Should we treat the flat-ladder observation as a
   market fact or as a possible artifact of the delayed indicative feed? That answer changes
   whether C3 is worth a capture season at all, and it is answerable from the data we are
   already recording — the soak's own output has quote timestamps and repeated quotes.
2. **C1's liquidity sub-condition may be worth separating from its positioning half.**
   "I cannot actually trade this strike" is a different statement from "everyone is positioned
   here," and only the first is a plausible *veto*. The second is a thesis input.
3. **Does OI have a place in T6 rather than in a new gate?** T6 already reads deep-OTM
   volume against a baseline. OI is the position-side twin of that volume signal. It may
   belong as a second input to T6 rather than as T7 — which would be an amendment to a frozen
   gate rather than a new one, and deserves its own ruling either way.
4. **Capture-season length.** All three candidates need history before they can be measured.
   The soak's T5 warm-up teaches the cost: 60 sessions at one observation per session per
   key. If the capture season is planned, its length should be chosen against that number
   rather than guessed.

---

## What this document is not

Not a proposal, not a recommendation to implement, not a draft rulebook, and not grounds for
changing anything before the v1 window closes. §(e) of `docs/ratification.md` pre-commits
to no mid-window amendment of the gates, and this note exists so that the v2 cycle has
something to argue with — including the possibility that all three candidates measure
nothing.