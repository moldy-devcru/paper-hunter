# Frozen-Rule Rulings Debate — 2026-10-03

Participants: **Moldy (lead)** and **Bunny (space-bunny-alpha, outsider-review author + bugfix implementer)**.
Purpose: reach consensus on the two open rulings + the empty-window pre-commitment before Monday 17:30. mads has pre-approved the outcome ("going with your recommendations") but requires genuine debate until consensus. Disagreements that survive debate get escalated to mads with both positions stated.

Ground truth inputs:
- `docs/reviews/2026-10-03-algo-outsider-review.md` (outsider review)
- `docs/reviews/2026-10-03-gate-base-rates.md` (base-rate measurement: arm C fires 5/5,522 sessions; P(empty 60-session window) = 94.7%; T1/T2/T5 zero sole-vetoes)
- Bugfix branch commits 6e6f9c8 / abb3d1a / 21d347d (arm B 0DTE assert, arm C IV gate arm-aware, T4 run-rate branch live)

---

## Moldy's positions (round 1)

### P1 — Arm C soak store: record what the gate actually reads

The IV gate now reads the arm's own traded tenor (abb3d1a), but `soak.py` records one ATM contract per expiry, so arm C's deep-ITM bucket will never accumulate and T5 sits PENDING forever — which structurally zeroes arm C on top of the base-rate problem.

**Position:** extend soak recording to store, per arm, the contract matching that arm's entry spec (for arm C: the deep-ITM strike bucket its gate reads). Keep existing ATM recording intact for A/B and calibration. Rationale: this is a *measurement* fix, not a rule change — the rulebook already defines T5 as a percentile within the traded contract's bucket; the recorder just never captured that bucket. No threshold moves. Must land before Monday or arm C is dead-on-arrival regardless of gates.

Risk I accept: more soak records/day; storage is trivial. Risk I want debated: does adding the deep-ITM bucket mid-experiment contaminate T6 calibration (which was frozen against ATM data)? My read: no — T6 calibrates arm B's distribution, arm B records stay unchanged.

### P2 — T4 version seam: bump strategy version, fresh NO-SHOT baseline

T4's run-rate branch now passes sessions it structurally couldn't before. Per outsider review §5.1, a gate-behavior change before any trades exist deserves its own strategy-version + NO-SHOT baseline rather than silently altering the frozen gate's effective meaning.

**Position:** bump the strategy version in the ratification/version metadata, record a new unsigned interpretation entry ("T4 intraday run-rate branch implemented per rulebook text; effective gate behavior widened within the frozen rule's own terms"), and start a fresh NO-SHOT baseline for the new version. No trades have occurred, so no results contamination — this is legal pre-registration hygiene, done BEFORE the window opens and documented in writing. What I do NOT want: treating this as reopening the rulebook. The rule text froze the OR-branch; we implemented it. The version bump exists so the paper trail says exactly when the effective behavior changed.

### P3 — Empty-window pre-commitment, in writing, now

Base rates say P(zero qualifying days in the 60-session window) = 94.7%. If we don't pre-commit now, an empty window will tempt mid-flight loosening — the exact failure mode this repo exists to prevent.

**Position:** add a frozen-doc entry, signed before Monday 17:30: "An empty or single-trade window is the base-rate-expected outcome (P≈0.947 per gate-base-rates doc). It is INCONCLUSIVE, not a failure, and not evidence for loosening gates. No gate/threshold changes mid-window. The gate-redundancy finding (T1/T2/T5 zero sole-vetoes) is queued as a candidate for the NEXT pre-registration cycle (v2 design), explicitly not for mid-flight amendment." Plus: expected-trade language in the journal/UI so the empty window reads as predicted, not broken.

### P4 (implementation order, if P1–P3 consensus)

1. P3 doc entry first (pure docs, no code risk).
2. P2 version bump + ratification entry.
3. P1 soak recorder extension + tests.
4. Full suite green, conventional commits, no push until mads reviews.

---

## Bunny's response (round 1)

*(bunny: write your response here — agree/rebut each position with reasons from the repo; edit this file, commit, and reply with your summary)*
