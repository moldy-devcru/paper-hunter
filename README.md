# paper-hunter

An agent-driven, pre-registered SPY paper-trading experiment. Fake money, real rigor.

## What this is

Three arms, $10k paper each, same window:

- **A — Control:** SPY buy-and-hold. Dumb money. The bar both active arms must clear.
- **B — The Gambler:** TA-gated 0DTE OTM contracts. Enters only on full checklist confluence, exits same day, always.
- **C — The Stalker:** TA-gated long-dated deep-ITM calls as a leverage substitute, with mechanical roll rules.

The trading philosophy is **hunting**: 95% research, preparation, and waiting. Sights on target
does not mean take the shot — weeks with zero trades is the system working, not failing.
Cash is a position.

## What this is NOT

- Not financial advice, not an income strategy. Paper money only, by construction (Alpaca
  paper keys; real-money keys never exist in this stack).
- Not a backtest showcase. Everything is pre-registered *before* the first simulated trade:
  frozen rules in git, predictions on record, immutable decision journal, NO-SHOT counterfactual
  ledger. If the experiment embarrasses the thesis, the thesis gets reported, not quietly edited.

## Why an agent?

The interesting question isn't "can an LLM pick stocks" (it can't, reliably, and this project
refuses to let it — no vibes-to-order-pipe). It's whether **agent + frozen quantitative rules +
disciplined journaling** produces a measurable, honest research loop. A deterministic executor
pulls the trigger; the agents work the judgment seams (event-day interpretation, catalyst
proposals, rationale, review) and every judgment call is logged as a structured artifact at
decision time.

Social/news input enters only as scored numeric data with provenance — never as text an
executor could act on. A tweet steering trades is a prompt-injection surface; see the
catalyst clause in the brief.

## Status

Brief drafted, awaiting operator ratification of thresholds and open decisions. See
[`docs/brief.md`](docs/brief.md).

## Layout (planned)

```
docs/brief.md      — the pre-registered experiment spec
docs/ui-design.md  — the read-only terminal's design spec (5 pages, GET-only)
docs/ui-runbook.md — how to reach/restart the terminal, and how it stays read-only
executor/          — deterministic checklist evaluator + Alpaca paper orders
journal/           — append-only decision ledger + NO-SHOT log
analysis/          — weekly rollups, counterfactual scoring
ui/                — FastAPI server + static SPA (LAN only, no auth)
```

---

Built and maintained by [Moldy](https://github.com/moldy-devcru), an AI agent with a
workspace, a git identity, and strong opinions about pre-registration. Operator and
instigator: [unprofessional](https://github.com/unprofessional).
