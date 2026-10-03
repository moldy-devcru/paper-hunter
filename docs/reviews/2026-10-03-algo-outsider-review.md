# Outsider Review: the SPY paper-hunter algorithm, its three arms, and its frozen rules

**Reviewer:** fresh-eyes subagent. No prior involvement with this repo, no stake in the
experiment's conclusions.
**Date:** 2026-10-03
**Strategy version reviewed:** `1.2.0-draft`
**Constraint:** review only — no code was modified. This file is the sole write.

---

## 0. Bottom line

This is unusually disciplined work and the pre-registration discipline is real, not
decorative. Sixty-seven rule ambiguities were resolved *before* the window opens, each one
written down with its reasoning next to the code line it governs, and the document says
out loud that "if a row here and the code disagree, the code is what runs." The
`FIX (...)` comments in `checklist.py` and `watch_loop.py` show the author finding and
naming their own bugs. Rare, and worth saying plainly.

But I think the experiment as specified **cannot answer the questions it pre-registered**,
and the reason is not subtle: it is arithmetic. Six independent blocking conditions
ANDed together, two of which fire on roughly 1 day in 20, produce a system that will
almost certainly take zero to two positions in a 60-session window. Pre-registered
prediction 3 ("arm C tracks A with leverage-amplified variance") needs arm C trades to
exist. Prediction 2 ("arm B is structurally negative-EV as an asset class") needs arm B
trades to exist. Prediction 4's own falsification test — "if we're trading weekly, the
rules were not frozen tight enough" — is a test the design is currently set up to pass
vacuously.

My recommendation is **not** to loosen the checklist. Loosening it after seeing zero
trades is precisely the failure mode this entire repository exists to prevent, and the
pre-registration is the only asset here worth protecting. My recommendation is to
measure the base rate *before* the window opens, fix two genuine implementation gaps that
are costing signals for free, and pre-commit in writing to what happens if the window
comes up empty. Sections 3 and 5 have specifics.

Four things I would call outright bugs rather than design opinions, in severity order:
arm B never asserts it is buying a 0DTE; arm C's IV gate is read off the wrong contract;
T4's intraday run-rate rule exists in the rulebook and no code reads it; and arm C's
entry criteria can be mutually unsatisfiable at plausible SPY price levels. Details in
section 4.

---

## 1. What each arm actually does, in plain language

**Arm A — Control.** Buy $10k of SPY at the open of day one and never touch it. No
checklist, no decisions, no exits. It is the benchmark. This is the right design; a
control that is too complicated stops being a control.

**Arm B — The Gambler.** Buy a same-day-expiry (0DTE), out-of-the-money SPY option. Only
when *every* condition on a seven-item checklist is true. Then get out the same day,
always: half (which, at the only size the code trades, is all of it) at +100%, everything
else at 15:30 ET, and a two-hour time stop if the position is not up at least 30% by
then. One position at a time, at most three entries a week. The idea being tested is
whether picking the *one day in twenty* that ticks every box is enough selection to beat
the constant bleed of 0DTE theta.

**Arm C — The Stalker.** Buy a deep-in-the-money, long-dated SPY call (90–180 days out,
delta 0.80 or better) as a leveraged stand-in for owning the index, and hold it for
months. Roll it mechanically — forward in time or down in delta — when it decays past
delta 0.70 or 45 days from expiry. Liquidate to cash if SPY closes below its 50-day
average three sessions running. Same checklist minus the flow condition.

**The hunting discipline** is real and correctly implemented: the checklist is a strict
AND, the veto is automatic, and NO-SHOT rows record what the system saw and declined.
Nothing in the design lets a judgement call sneak an order through, which is the single
most important property this repo has and which most trading code does not have.

---

## 2. Are the signals sound, too few, too many, well-calibrated?

**Individually, they are textbook and they are implemented correctly.** I checked the
arithmetic rather than assuming: Wilder's RSI uses Wilder smoothing (not the naive
one-liner that most Python RSI snippets use); the Bollinger standard deviation is
population, not sample; EMA seeding is SMA-of-first-N; the MACD signal line is an EMA of
the *defined* tail of the MACD line, which is the thing people get wrong by one bar;
and `bandwidth_percentile` excludes the newest value from its own comparison window.
These are all correct, and they are correct in the ways that matter — the differences
between these choices and the common alternatives are exactly the differences that make
a backtest disagree with a chart. The indicator module is the strongest code in the repo.

**Together, they are far too tight, and the tightness is not distributed the way the
brief assumes.** The brief promises "confluence, not any-one," which is the right
principle, and it promises fewer than two trades a month. Here is what confluence
actually costs, using conservative standalone daily-bar pass rates for SPY:

| Condition | What it needs | Rough share of days it passes alone |
|---|---|---|
| T1 | close > 50EMA > 200EMA (calls) | ~45% |
| T2 | 50 < RSI(14) < 75 | ~45% |
| T2b | MACD line > signal **and** histogram expanding | ~25–30% |
| T3 | squeeze-release *or* 2σ band rejection | ~3–6% |
| T4 | relative volume ≥ 1.5× | ~6–10% |
| T5 | IV rank < 50 | ~50% |
| T6 | flow ratio ≥ N (by design, P90) | ~10% |

Multiply those and arm C lands around **2 × 10⁻⁴** — one qualifying day per few thousand
sessions. The conditions are positively correlated (a high-volume trend day tends to pass
T1, T2b and T4 together), so the honest number is higher than the naive product, but not
by an order of magnitude. The structural point survives regardless: **the binding
constraints are T4 and T3, not the trend gate the brief emphasises.** T1 and T2 are doing
almost no filtering work. If the operator's instinct was "the trend gate is the strict
one," the arithmetic says otherwise.

Two specific calibration problems, both of which I think are unintentional:

**T3a is close to self-contradictory at daily resolution.** It requires bandwidth
percentile below the 20th of the trailing 60 *and* bandwidth higher than the previous
bar's *and* the middle band rising. A bar sitting in the bottom 20% of its own
volatility history has almost always been falling into that percentile. Requiring it to
have ticked up versus exactly one bar prior means T3a can fire only on the first or
second session of a volatility bottom — roughly one or two days per quarter of quiet.
That is fine as a *design*, but it means the T3 OR group is effectively "T3b only."

**T2b fights T3b.** T3b for calls wants a bar that dipped to the lower 2σ band and
closed back inside — a reversal bar, by definition the tail of a momentum fade. T2b vetoes
when the MACD histogram is contracting, which is exactly what that bar produces. Both are
blocking. This is the same structural bug the author already found and fixed once: there
is a `FIX (2026-10-02)` comment in `checklist.evaluate` recording that T3a and T3b were
previously ANDed and that "no series could ever fire." That instinct was right and it has
not been applied one level up, to the condition set as a whole.

**One more, and it is about the exits rather than the entries: arm C has no
profit-taking rule at all.** Its entire exit ladder is "roll" and "liquidate if SPY closes
below the 50EMA three days running." A deep-ITM long call that has run from $3,000 to
$6,000 has exactly two ways out: wait for a trend break, or roll. There is no frozen rule
that says take money off a winner. Section 5 proposes one, and it happens to fix a second
problem at the same time.

---

## 3. Risks and overfitting concerns

**What is genuinely strong.** The anti-narrative-fitting machinery is the best I have
seen in a retail-quant repo: PENDING is a distinct third state from PASS and FAIL so an
uncalibrated gate can neither pass trades nor silently block them; the rulebook loader
rejects unknown keys and refuses to start with calibrations silently open; the T6
calibration formula (N = P90 of the 20-session ratio distribution) was fixed *before*
anyone saw the distribution, and the document names its own weakness ("unstable on 20
samples") rather than waiting to be asked. That is pre-registration done properly.

**The overfitting risk that actually matters here is not threshold-tuning. It is the
empty-window response.** The design is set up to produce very few trades. Sooner or later
the window will come up with zero or one, someone will correctly observe that the rules
were too tight, and the cheapest available response will be to loosen them. That response
would destroy the only thing this experiment has that a normal trading system does not.
The defences currently in the repo are about *editing* rules (version bumps, monthly
review only, journaled seams) — good, and none of it stops a mid-window loosening framed
as "we learned the threshold was wrong."

**Cheap insurance, and it must be written down now, before results exist:** pre-commit,
in the rulebook, to what a thin window means. My suggested wording is roughly — *if the
window closes with fewer than N qualifying signals, the result on trade *frequency* is
reported as "rules too tight at the frozen thresholds," the thresholds are **not**
changed, and the window is extended rather than re-tuned.* Writing that sentence down
while nobody has results is worth more than every interpretation row in section (a),
because it is the only one that closes the loophole the others do not cover.

**Smaller items.** Sixty-seven interpretations were all made by the same author who
drafted the thresholds they interpret, with no out-of-sample check that the resulting
gate can ever fire — that check is free and absent. The April 2025 catalyst-clause trade
is one remembered winner, which the brief already says on the record. The arm-C
counterfactual uses a delta-weighted proxy that ignores the second-order terms that make
deep-ITM long calls behave like the underlying; that limitation is labelled everywhere it
appears, which is the correct handling.

---

## 4. Bugs and doc/code inconsistencies

Ordered by severity. Each is something I verified by reading the code path, not by
inference about intent.

### 4.1 Arm B never asserts it is buying a 0DTE contract — *correctness, high*

`watch_loop._arm_b_contract` selects contracts with
`c.expiry == snapshot.chain.contracts[0].expiry`, commented `# nearest expiry only (0DTE)`.
That is the expiry of whatever contract the API returned first; the chain is never sorted
by expiry (`OptionChain.expiries()` sorts a copy). There is **no DTE assertion anywhere in
arm B's entry path**. A correct `nearest_expiry(on_or_after=...)` helper exists in
`alpaca_client.py:734` and is not used here; `_dte_from_expiry` already exists and is
used for arm C.

If the first-listed expiry is not today's, arm B silently buys an n-DTE contract, and
every downstream claim in the brief — "exits same day, always," "never holds to expiry,"
`hold_to_expiry: false`, and prediction 2's claim about 0DTE *as an asset class* — stops
describing what is being traded. It fails silently: the journal records the OCC symbol,
and only a reader who knows to check the expiry stamp would catch it.

### 4.2 Arm C's IV gate is read off the wrong contract — *correctness + brief divergence, high*

The brief's arm-C clause is "IV on the **chosen strike** within normal band" — i.e. the
deep-ITM 0.80-delta contract arm C actually buys. The code
(`hunt_plan._iv_rank_for_direction` → `_atm_for_right`) picks the contract whose strike is
nearest spot across the **whole chain, with no DTE filter**, and uses that for both arms.
Front-month strikes are the finest-grained and cluster closest to spot, so the winner is
almost always a short-dated contract.

Two consequences. First, the gate measures the wrong volatility: a 150-DTE premium
purchase is being cleared or blocked by where same-day short-dated ATM vol sits in its
own distribution. Second — and this is the part that makes it worse than a small
approximation — **IV rank is a percentile within the bucket's own history**, so a
short-dated ATM rank and a 150-DTE ITM rank are not the same quantity at all. They have
different underlying observations and different percentiles.

`soak.py` records only the ATM contract per expiry, so the store could not answer the
right question even if the selector asked it. Fixing the selector is a code change;
deciding what the store must accumulate going forward is a ruling, and should get one.

### 4.3 `intraday_run_rate_min` is a frozen rule the code never reads — *medium-high*

The rulebook carries `checklist.t4_volume.intraday_run_rate_min: 1.5`, and the brief says
"relative volume ≥ 1.5x 20-day average **on the signal candle** (or intraday run-rate
≥ 1.5x for intraday triggers)." Across all of `executor/`, that field appears exactly
three times: the loader's field definition, a test asserting the value, and a *string
inside a log message* in `checklist._t4`. T4 is evaluated solely as
`relvol >= relative_volume_min`. The run-rate branch is not implemented.

This matters more than a missing feature, because of what it interacts with.

### 4.4 T4 divides a possibly-partial day's volume by a full-day average — *medium-high*

`build_relvol` computes `current = volumes[-1]` against a 20-day mean of **completed**
sessions. Nothing anywhere excludes a same-session forming 1-day bar (I grepped
`alpaca_client.py` for `partial` / `forming` / `incomplete`: nothing), and the intraday
provider re-fetches daily bars every tick. Alpaca's `1Day` route returns the in-progress
session bar. *I have flagged this as an inference rather than a verified fact* — it wants
one live call to `GET /v2/stocks/SPY/bars?timeframe=1Day` during a session to settle — but
the code has no guard either way, so the failure mode is unguarded regardless.

If it holds, then to pass T4 at 14:00 you need today's *finished* volume to exceed about
1.67× the 20-day mean: T4 becomes a forecast of the rest of the session rather than a
measurement of it, and it structurally fails for most of every session. Fixing this is
precisely what the unimplemented rule in 4.3 was written for.

### 4.5 Arm C's entry criteria can be mutually unsatisfiable — *feasibility, medium-high*

Frozen together: 90–180 DTE, delta ≥ 0.80, premium ≤ 50% of a $10,000 bankroll (so
≤ $50/share, $5,000 per contract). Black-Scholes at r=4%, q=1.3%, σ=15%, solving the
strike for delta = 0.800:

| SPY spot | 90 DTE | 135 DTE | 180 DTE |
|---|---|---|---|
| 500 | $3,328 (33%) | $4,015 (40%) | $4,577 (46%) |
| 550 | $3,661 (37%) | $4,416 (44%) | **$5,034 — breaches** |
| 600 | $3,994 (40%) | $4,818 (48%) | **$5,492 — breaches** |
| 650 | $4,326 (43%) | **$5,219 — breaches** | **$5,950 — breaches** |
| 700 | $4,659 (47%) | **$5,621 — breaches** | **$6,407 — breaches** |

There is no relaxation ladder: if nothing clears all three, `_arm_c_contract` returns
`None` and the manager raises `no_candidate`. So **arm C's feasibility is a function of
the index level, not of the setup.** R5 makes arm C one of only two arms active at window
open, since B is inert until T5 warms. If SPY opens November above roughly $550–600, arm
C is a no-trade arm by construction and the window scores Arm A against cash. The real
chain's IV will differ from my 15% assumption and the exact crossover will move, but the
shape will not. This deserves an explicit feasibility check before the window rather than
a discovery on day one.

### 4.6 Even when arm C trades, it is structurally under-invested versus its own control — *design, medium*

At spot 500 the cheapest qualifying contract deploys 33–46% of bankroll, and `qty` is
hard-coded to 1. The other 54–67% sits in cash for the whole window. Arm A deploys 100%
on day one. Pre-registered prediction 3 says arm C "tracks A with leverage-amplified
variance" — but a position at ~0.4× notional exposure has *less* variance than A, not
more, and carries a structural theta and roll drag on top. As specified, arm C cannot
win the comparison it was entered into.

The genuinely interesting question inside prediction 3 — do TA entries pick better *roll
points* than a fixed quarterly roll? — is well posed, and `analysis/shadow_roll.py` is
the right instrument for it. But it is a roll-timing question, not a "leverage-amplified
variance" question, and the prediction should probably be reworded to say which one it
means.

### 4.7 The intraday "re-verification with live data" cannot change T1–T4 — *medium*

`watch_loop.reverify_cell` rebuilds the snapshot via `build_snapshot(daily=snapshot.daily,
...)`, and `build_snapshot` sets `close = daily.bars[-1].c`. The live `spot` argument is
used only for a provenance note, never as the signal close. So T1, T2, T2b, T3 and T4
are recomputed from the same bar series the pre-market plan used, and only T5 and T6 are
genuinely live inputs.

The brief's requirement — "checklist re-verified with live data at trigger time
(stale-checklist entries are forbidden)" — is satisfied in letter: a snapshot is rebuilt
and re-evaluated, and the staleness budget is enforced honestly. But the re-evaluation is
not over live data for five of the seven conditions. Given 4.4, whether it moves at all
depends on whether a forming daily bar is present. Worth making explicit rather than
leaving to a reader.

### 4.8 The plan builds arm C / put cells, and they can be "green" — *medium*

`build_hunt_plan` loops over `DIRECTIONS = ("call", "put")` for every active arm,
including C, whose rulebook entry is `right: call`. Nothing filters directions per arm —
not the plan builder, not `_arm_c_contract` (it simply finds no matching contract), not
`_arm_c_entry` (whose governors are the window, the slot limit and `no_candidate`).

So a C/put cell can be fully confluence-green at 08:30, get journalled as `PROPOSAL`,
be **armed by the watch loop under R2**, consume its one-per-session re-verification, and
then die every session at `no_candidate`. That is a phantom target in the "sights on
target" record, and a steady drip of misleading rows into the veto stream and the
checklist-failure histogram. Arm C is calls-only; the plan should say so.

### 4.9 Both entry paths price the premium cap off the bid, not the ask — *low-medium*

`_arm_b_contract` and `_arm_c_contract` both use `contract_price()`, which is documented
as "the direction that liquidates a long" — bid, then mid, then ask. Buying costs the ask.
The codebase already knows this: `position_manager._ask_mark` exists precisely to price a
*buy* on the ask, and its docstring says the two directions "deliberately do not share a
function." The roll path uses `_ask_mark`; both **entry** paths use the bid-first
helper. The roll's cap check is conservative and the entries' is not. Severity is bounded
— the entry also places a limit at the bid, so an over-budget contract realistically
fails to fill rather than over-spends — but it biases the affordability check in the
permissive direction for exactly the check that is hardest to satisfy (4.5).

### 4.10 Two frozen rulebook parameters have no effect at the only size in use — *low*

`_entry_leg` always sets `qty = 1`. The profit take closes `half_quantity(1)`, and
`half_quantity` rounds **up** by documented interpretation — so "close 50% at +100%"
closes **100%** of a 1-lot position. Net behaviour: arm B's take-profit is "close
everything at +100%," and `trail_remainder: true` has nothing left to trail. The
behaviour is defensible; the inconsistency is that `close_pct_at_100pct_gain: 0.50` and
`trail_remainder: true` are still advertised as frozen rules with no effect.

### 4.11 Documented and already tracked, but still the top operational blocker

Only one timer exists in the repo: `scripts/soak.timer`, 17:30 ET. **There is no timer
for the pre-market plan, the intraday watch loop, or the EOD close** — the other three
steps of the brief's signal cadence. Under the 2026-10-02 autonomy ruling ("autonomous,
that's the whole point," with no per-trade approval gate by design), an autonomous
executor with no scheduler means no entries happen unless a human starts the commands by
hand on the right days.

This is already known: `docs/rehearsal/2026-10-03-full-cycle.md` §5 is titled "GO / NO-GO
checklist for arming the cron trio" and the verdict is NO-GO pending data readiness. I am
flagging it because it is *still* on the critical path and it is the one gap that makes
every other finding moot if it is not closed.

### 4.12 One thing to confirm rather than a code issue

Arm A buys SPY and holds. Alpaca paper accounts generally do not pay dividends. A ~1.3%
annual yield difference is small over three months but it is a systematic, one-directional
tilt in the headline "which arm beat the control" comparison, and it costs one API call to
check.

---

## 5. Concrete suggestions for more signal opportunities

Ranked by value-per-unit-of-disruption. The first two are not rule changes — they are
implementing rules that already exist.

**5.1 Implement the intraday run-rate branch of T4.** *(no rule change; fixes 4.3 and 4.4)*
Compare today's cumulative volume at time *t* against the same time-of-day cumulative
volume averaged over the prior 20 sessions, at the same 1.5× bar the rulebook already
carries. This is the rule the brief already wrote for exactly the situation the code is
in. Expected effect: large, and strictly positive — it replaces a condition that is
structurally near-ungoable intraday with one that is a genuine measurement. It is also
the single change most likely to change the *character* of the entries rather than just
their count, so it deserves its own strategy-version seam and its own NO-SHOT baseline.

**5.2 Pre-commit the base-rate check, and re-examine T2b-vs-T3b.** *(measurement first)*
Compute the frozen checklist's historical fire rate on several years of SPY daily bars
before the window opens, and report it against the brief's own "< 2 trades/month"
prediction. This is not tuning — it is the same discipline already applied to T6's P90:
a definition fixed before the data exists, with the numbers shown. Whatever it says, it
should be recorded, because right now the base rate is an unstated assumption and every
other decision in this review depends on it.

If the base rate comes back where my arithmetic suggests, the cheapest structural fix is
not to weaken conditions but to **stop double-counting correlated facts**. T3b (a 2σ band
tag with a close back inside) and T4 (1.5× relative volume) are substantially the same
observation — a 2σ excursion *is* a volume event. ANDing them charges twice for one
fact. A documented rule that T4 is satisfied by a T3b band tag *with* volume above
average, rather than independently, would be a smaller and more honest change than
lowering the 1.5 threshold, and it would also resolve the 4.2-style T2b/T3b conflict,
which has the same character.

**5.3 Add a third arm-C exit: a mechanical, frozen take-profit.** *(the biggest missing
sell signal)*
Arm C's exit ladder is roll-or-trend-break. There is no rule that banks a gain. Propose,
at the next monthly-review seam with the numbers shown: close 50% of the position at
+50% on cost, and/or trim when delta recovers above 0.90. This is currently a *sell*
rule the system does not have, and adding it does two useful things at once — it gives
the winning case somewhere to go, and it partially compensates for the under-investment
in 4.6 by recycling capital into the next qualifying position instead of leaving 55% of
the bankroll idle for three months. The counter-argument is real and should be recorded:
it converts arm C into a trading arm rather than a hold arm, which is a change to what
the arm *is*, not a tweak. That is a ruling, not a suggestion — which is why it belongs
at the monthly review and not in a patch.

**5.4 Add a second, genuinely different arm-C entry regime — pullback-in-uptrend.**
*(the honest way to add signal)*
The current checklist finds volatility *bottoms* and band *rejections*; both are reversal
shapes. A deep-ITM long call is more naturally bought on a **pullback that holds** — T1
aligned, RSI in the 40s–55s (momentum cooling, not exhausted), price reclaiming the 20EMA
or the middle Bollinger band from below on declining-but-not-collapsing volume. That is a
momentum-continuation setup, not a reversal one, and it occurs on a different and larger
set of days. Adding it as an explicit second T3 sub-condition (a genuine OR arm, with its
own logged branch) rather than as a threshold relaxation keeps the pre-registration
intact: it is a *new frozen path*, versioned and declared before the window, not a
loosening of an existing one. The risk is real — two entry regimes is twice the surface
for hindsight fitting — which is exactly why it belongs on paper, with its own base-rate
computed first, rather than arriving as a patch.

**5.5 Cheap, no-risk additions to signal *capture* rather than signal *generation*.**
* Assert DTE = 0 on arm B's chosen contract (4.1) and veto with a reason otherwise —
  one line, using a helper that already exists.
* Filter arm C's direction grid to calls (4.8) — one line, removes a phantom cell.
* Use `_ask_mark` on the entry cap checks (4.9) — one line, restores the mirror the
  docstrings promise.
* Log entry-clock-time bucket on every decision so the F12 asymmetry (a 09:45 entry gets
  two hours of optionality, a 14:00 entry gets none) is measurable from the journal
  afterwards instead of being lost.

**5.6 What I would *not* do.** I would not add indicators. The checklist already carries
four families of technical evidence plus volume plus options positioning, and T1/T2 are
demonstrably doing almost no filtering work (section 2) — adding a fifth overlapping
indicator would tighten nothing and would add five more degrees of freedom to justify
later. And I would not touch the T6 flow gate's rarity: a gate designed to pass 10% of
days is doing exactly what it was designed to do, and it is one of the more interesting
objects in the whole experiment.

---

## 6. What I would verify before the window opens

Cheap checks, in the order I would run them. None requires changing the rulebook.

1. **Does Alpaca's `1Day` route return the forming session bar?** One call during market
   hours. Settles 4.4, and with it whether 4.7 means anything.
2. **Run the frozen checklist over ≥5 years of SPY daily bars and print the fire rate,
   per condition and joint.** Settles the central uncertainty in this review. Everything
   in section 5 depends on it.
3. **Check arm C's feasibility at plausible SPY levels** — for a grid of spot prices, the
   cheapest contract satisfying 90–180 DTE / delta ≥ 0.80, and its premium against the
   $5,000 cap. Settles 4.5 with real chain numbers instead of Black-Scholes.
4. **Arm the cron trio** once the rehearsal's data-readiness gates clear (4.11).
5. **Confirm whether Alpaca paper pays SPY dividends** (4.12).

---

## 7. In one paragraph

The rigor here is real and the pre-registration is the valuable asset; protect it by
pre-committing now to what an empty window means, and do not loosen thresholds in
response to one. The checklist as frozen is far tighter than the brief's own frequency
prediction contemplates, and the two conditions causing almost all of that are T3 and
T4 — one of which (T4's intraday run-rate branch) is a rule the rulebook already
contains and no code reads. Two of the four things I'd call outright bugs — arm B not
asserting it is buying a 0DTE, and arm C's IV gate being read off a short-dated ATM
contract instead of the deep-ITM strike it buys — are the kind that pass silently and
corrupt the experiment's central claims rather than merely failing loudly. Fix those,
measure the base rate before opening the window, and the experiment can honestly report
whatever it finds.

---

## Appendix A — raw read notes

Kept so a reader can check my work without re-reading the source. `F1`–`F15` map to the
sections above.

* **F1** `intraday_run_rate_min` is in the loader (`config/loader.py:451`) and the
  rulebook; the only reference in `executor/` is a format string in `checklist._t4`'s
  detail text. → 4.3
* **F2** `build_relvol` uses `volumes[-1]` against `volumes[-21:-1]`; nothing filters a
  forming daily bar. → 4.4
* **F3** `reverify_cell` → `build_snapshot(daily=...)`; `close = daily.bars[-1].c`; the
  live `spot` argument only feeds a provenance note. → 4.7
* **F4** Base-rate arithmetic on the six blocking conditions. → 2
* **F5** T3a needs bottom-20th-percentile bandwidth *and* up-versus-one-bar *and* middle
  band rising. → 2
* **F6** T2b's contracting-histogram veto against T3b's reversal-bar requirement.
  Same shape as the already-fixed T3a/T3b AND bug (`checklist.evaluate`, FIX
  2026-10-02). → 2
* **F7** `watch_loop._arm_b_contract`: `c.expiry == snapshot.chain.contracts[0].expiry`.
  `OptionChain.nearest_expiry()` at `alpaca_client.py:734` is unused; `_dte_from_expiry`
  is used only for arm C. → 4.1
* **F8** `contract_price` is bid-first; `_ask_mark` exists and is used only by the roll.
  → 4.9
* **F9** Black-Scholes grid for arm C's 90–180 DTE / delta 0.80 / ≤$5,000 constraint. → 4.5
* **F10** `qty=1` in `_entry_leg`; arm A deploys 100%, arm C 33–46%. → 4.6
* **F11** `half_quantity` ceils; `qty=1` makes "close 50%" close 100%; `trail_remainder`
  has nothing to trail. → 4.10
* **F12** Entry window closes 14:00; time stop is 120 minutes; a 14:00 entry has 35
  minutes to the hard close and can never see the time stop. → 5.5
* **F13** `_atm_for_right` = `min(pool, key=|strike - spot|)` over the whole chain, no
  DTE filter, shared by both arms; `soak.py` records only the ATM contract per expiry.
  → 4.2
* **F14** `build_hunt_plan` loops `DIRECTIONS` for every active arm including C, whose
  `entry.right` is `call`. → 4.8
* **F15** `checklist.evaluate` carries a FIX comment recording that T3a/T3b were
  previously ANDed and that "no series could ever fire." The same check has not been
  applied to the condition set. → 2, 3
