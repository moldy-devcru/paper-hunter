# Project Brief: SPY Paper-Trading Experiment ("Operation Hunting Blind")

Status: DRAFT for operator ratification — nothing executes until this is frozen in git and signed off.
Author: Moldy, 2026-09-29.

## Premise

Agent-driven technical-analysis trading on SPY, paper money only (Alpaca paper API), run as a
pre-registered hypothesis test with an immutable decision journal. Not an income strategy —
a research project proving (or disproving) whether disciplined TA signal-selection can beat
naive controls under identical simulated conditions.

**Trading philosophy (operator, ratified):** hunting. 95% research/preparation/waiting, sights on
target, and STILL walking away if the shot isn't perfect. Weeks with zero trades is the system
working, not the system failing. Cash is a position.

## Arms (3, $10k paper each, $30k total, same window, all rules frozen pre-first-trade)

- **Arm A — Control:** SPY buy-and-hold. $10k at market open of day 1. No decisions, no trades.
- **Arm B — The Gambler:** TA-gated 0DTE OTM contracts on SPY. Enters ONLY on perfect setups
  (checklist below). Exits same day, always. Never holds to expiry.
- **Arm C — The Stalker:** TA-gated long-dated deep ITM calls (leverage substitute). Enters ONLY
  on perfect setups. Holds with pre-defined roll/exit rules.

TA rulebook applies to B and C. A is dumb money by design — the bar both active arms must clear.

## Pre-registered predictions (ON RECORD before first trade — anti-narrative-fitting)

1. Arm A finishes the window positive in expectation (market beta).
2. Arm B (0DTE OTM) is structurally negative-EV as an asset class: theta + OTM decay burn most
   premiums. The falsifiable claim under test: **selective high-conviction timing can overcome
   the structural decay.** My honest prediction as of drafting: B underperforms A, possibly to
   near-zero. If B beats A over the window, that is a genuine finding and I will say so loudly.
3. Arm C tracks A with leverage-amplified variance; the question is whether TA entries pick
   better roll points than a fixed quarterly roll (a 4th shadow-sim, costless, can run in the
   journal as hypothetical).
4. Trade frequency: under hunting discipline I expect < 2 trades/month across B and C combined.
   If we're trading weekly, the rules were not frozen tight enough.

## The Setup Checklist (ALL conditions must be true — confluence, not any-one)

### Common trend gate (both B and C)
- T1: Price vs 50EMA and 200EMA alignment in trade direction (calls: close > 50EMA > 200EMA;
  puts: inverted). Counter-trend trades require T2+T3+T4+T5 ALL plus a documented reversal
  trigger — default is WITH trend only.
- T2: RSI(14) regime: > 50 for call bias, < 50 for put bias, AND not exhausted (> 75 / < 25 =
  no new entries, wait for reset).
- T2b: MACD(12,26,9) agreement — MACD line above signal for call bias, below for put bias;
  histogram expanding in trade direction preferred (contracting = momentum fading, log as
  veto reason). No entries against a fresh MACD cross < 1 day old unless T3a squeeze-release
  fires (whipsaw guard).
- T3: Bollinger condition — one of: (a) squeeze release (bandwidth < 20th percentile of
  60d, then expanding in trade direction), (b) band rejection (close tags lower band and
  closes back inside for calls; inverted for puts).
- T4: Volume confirmation — relative volume ≥ 1.5x 20-day average on the signal candle (or
  intraday run-rate ≥ 1.5x for intraday triggers).
- T5: Options-chain corroboration — no hostile IV setup: entry-tensor IV rank < 50 for
  premium-buying (B), or for C, IV on the chosen strike within normal band (no earnings/FOMC
  day entries — event calendar checked daily, hard veto).
- T6 (Arm B only, flow gate — added 2026-09-29 per operator's April-2025 case study): deep-OTM
  0DTE volume on the trade-direction side ≥ Nx 20-day baseline (N to be calibrated from
  historical SPY flow data during implementation; frozen before first trade). Quantifiable
  gamma-squeeze footprint — the robot-executable half of the social signal.

### Catalyst clause (news/social exception path — human-in-loop, per-trade)

Social media and breaking news NEVER enter the robot path as text. Two speeds only:

1. **T6 flow data** (above): numeric, sourced, boolean-checkable.
2. **Exception path:** on a major catalyst (tariff announcements, Fed surprises, presidential
   posting sprees, flow-report virality), the agent may PROPOSE an off-checklist entry with
   full decision-time journal reasoning, but the operator approves or vetoes live, per trade.
   Social content reaches the proposing agent as scored numeric data with provenance
   (sentiment value, source rank, chatter volume) — never as instructions (envelope-vs-content
   rule; a tweet steering tool calls is the purest prompt-injection surface). Exception-path
   P&L is tracked SEPARATELY in the journal so the window can score: does the operator's
   hunting eye beat the frozen rules? Survivorship caveat on record: the April 9, 2025
   $50→$3000 0DTE trade ("great day to buy" post + reported deep-OTM call flows) is the
   inspiration, but it's one remembered winner from a month of faded "sure things" — hence
   separate measurement, not baked-in trust.

Data-plumbing note: real-time social firehose is expensive/laggy (X API paywalled; Reddit
free but slow). Flow data is the tractable, load-bearing half — prioritize it.
**UPDATE (operator, 2026-09-29):** a dedicated Chromium session logged into X
with the operator's account already exists, following options-flow watchers, financial, and
macro accounts. That
solves the catalyst-clause intake: the proposing agent reads the curated follow-graph via
that browser session, scores it into structured numeric artifacts (chatter volume, source
rank, sentiment), and ONLY those artifacts feed the proposal. The raw feed never touches
the executor. Same envelope-vs-content discipline: what the agent reads on X is data, never
instructions — including (especially) replies/QTs to the followed accounts.

### Arm B specifics (0DTE)
- Entry window: 09:45–14:00 ET only (no opening-drive noise trades, no late hero entries).
- Strike: first OTM strike beyond the setup-day range projection; premium ≤ 2% of arm bankroll
  (hard cap: $200/trade at $10k — max 50 total losses before arm is dead, and it will be long
  before then).
- Time stop: if position not ≥ +30% by 2 hours after entry, close at market.
- Profit take: close 50% at +100%, trail rest; close ALL by 15:30 ET. No exceptions, no expiry.
- Max 1 concurrent position. Max 3 entries per week even if setups appear (anti-tilt governor).

### Arm C specifics (deep ITM)
- Contract: 90–180 DTE, delta ≥ 0.80 at entry.
- Size: premium ≤ 50% of arm bankroll per position (cash buffer for rolls).
- Roll rule (frozen): roll forward when delta < 0.70 OR DTE < 45, whichever first — roll to
  next expiry meeting entry criteria. Roll is mechanical, not discretionary.
- Exit-all rule: T1 breaks (close < 50EMA for 3 consecutive EODs) → liquidate to cash, wait
  for full checklist re-satisfaction.
- Max 1 concurrent position.

### The NO-SHOT log (the hunting part)
Every day the sights were on something and we didn't shoot gets a journal entry: which
conditions failed, indicator values, and the outcome the rejected trade WOULD have had
(tracked hypothetically). This is how we measure whether the discipline is saving us or
costing us — the counterfactual ledger. Review monthly.

## Signal cadence (operator: all three matter, especially for 0DTE)

1. **Pre-market (08:30–09:15 ET):** EOD indicator stack recomputed, event calendar veto check,
   watchlist levels set (Bollinger bands, EMA levels, trigger prices). Output: today's hunt plan.
2. **Intraday (continuous, executor watch loop):** trigger-price watch on the plan's levels; checklist
   re-verified with live data at trigger time (stale-checklist entries are forbidden). Alerts
   on trigger, human-readable rationale in journal at decision time.
3. **EOD (16:15 ET):** positions marked, time stops enforced, journal closed for the day,
   NO-SHOT entries written. Weekly rollup to the operator.

## Data requirements (verify before brief is final)

- SPY OHLCV daily + intraday (1m/5m): Alpaca data API — verify tier limits.
- Options chains with Greeks + IV (B and C need live chains; T5 needs IV rank = ~1y history):
  **OPEN QUESTION** — Alpaca options data depth vs supplemental source (Tradier free tier /
  Polygon / CBOE delayed). To be verified first implementation task.
- Event calendar (FOMC, CPI, OPEX): free sources, static file refreshed weekly.

## Journaling & review (rigor condition #3)

- Every decision (trade, no-trade, roll, stop) = immutable append-only entry: timestamp,
  indicator snapshot (ALL checklist values, not just the deciding ones), checklist state,
  stated reasoning, conviction 1-10. Written AT decision time by the executing agent.
  Post-hoc edits forbidden; corrections are new entries referencing the old.
- Ledger: SQLite or PG table in this repo's runtime (paper data only — nothing sensitive).
- Weekly rollup: P&L per arm vs A, trade/no-trade counts, checklist-failure histogram
  (which conditions veto most), counterfactual ledger delta.
- Monthly review: operator + agents. Rule changes ONLY at monthly review, ONLY versioned commits,
  NEVER mid-position. Any change = new strategy version, journal notes the seam.

## Execution stack (proposed — revised 2026-09-29: strictly boolean rules ⇒ deterministic executor)

- **Executor = dumb deterministic script** (no LLM in the loop): evaluates the frozen
  checklist against live data, places Alpaca paper orders,
  enforces time-stops/profit-takes mechanically. A script does boolean perfectly and an LLM
  does it expensively — the robot pulls the trigger.
- **Agent judgment seams (the LLM agents):** event-day interpretation, catalyst-clause
  proposals (operator approves/vetues), NO-SHOT rationale, journal narration, counterfactual
  ledger review, weekly rollups. Every agent decision point produces a structured logged
  artifact — no vibes-to-order-pipe, ever.
- Kill switch: paper-only by construction (Alpaca paper keys), plus a literal env-var guard
  and separate key storage. Real-money keys never exist on this fleet.

## Open decisions for the operator

1. Repo home: `moldy-devcru/paper-hunter` (this repo).
2. Window length: propose 3 months minimum before any conclusions (0DTE arm needs sample).
3. Checklist thresholds above are Moldy's draft from the operator's indicator list — ratify/edit each.
4. Arm C roll rule delta/DTE numbers — ratify/edit.
5. Who pulls the trigger: executor runs autonomously within frozen rules (recommended —
   the rules ARE the human oversight, pre-registered), or human approval per trade (slower,
   but adds a tilt-governor). For B especially, intraday windows argue for autonomous.
