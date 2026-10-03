# Runbook — installing the daily-cycle timers

**The plan / watch / EOD legs have never existed as units.** `soak.service` and
`soak.timer` are installed and enabled; the other three legs of the daily cycle are not,
which is what `docs/rehearsal/2026-10-03-full-cycle.md` §5 calls the "cron trio" and
rates NO-GO. This runbook installs the three missing pairs.

**Nothing here has been executed.** The units are built and tested
(`tests/test_daily_cycle_units.py`, 43 assertions) but are not installed, not enabled,
and not started. Enabling them is an operator decision.

> **Do this runbook AFTER the deploy in
> [`docs/reviews/2026-10-03-soak-deploy-audit.md`](../reviews/2026-10-03-soak-deploy-audit.md).**
> The soak timer is currently armed against a `/opt` checkout that runs the **wrong IV
> key schema**, and it fires Monday at 17:30 ET. Disarming it first is step 1 of that
> document and it takes thirty seconds. Doing the two in the other order means Monday's
> soak adds another block of rows T5 will never read.

---

## 0. What you are installing

| leg | unit | timer | when | catches up if missed? |
|---|---|---|---|---|
| pre-market plan | `plan.service` | `plan.timer` | 09:20 ET weekdays | **yes** |
| intraday watch loop | `watch.service` | `watch.timer` | 09:30 ET weekdays | **no** — deliberately |
| EOD / NO-SHOT ledger | `eod.service` | `eod.timer` | 16:15 ET weekdays | **yes** |
| soak (already installed) | `soak.service` | `soak.timer` | 17:30 ET weekdays | yes |

All four share `WorkingDirectory=/opt/paper-hunter`, `User=paper-hunter`,
`ProtectSystem=strict`, `ReadWritePaths=/opt/paper-hunter/data`, and
`EnvironmentFile=-/etc/paper-hunter/soak.env`. The path is **verified**, not assumed —
the audit document §2 confirms the live job runs from `/opt` and not from a `~/dev`
worktree. If the deploy root ever moves, all four units move in the same change.

### Why the catch-up settings differ

They are not inconsistent; they follow from what each job's artifact costs when it is
late.

- **plan and EOD catch up.** Both write into an append-only journal. A missed run leaves
  a permanent gap that cannot be reconstructed later, because reconstructing it means
  recomputing a decision from state that has since changed.
- **watch does not catch up.** The loop is time-boxed (`--stop-et 16:00`). Starting it at
  11:00 would watch a partial session and write `NO_TRADE` rows for a window it never saw.
  A fabricated partial session is worse than an honest empty one.

### Why the times are what they are

- **plan 09:20** — the plan is built from the prior session's settled bar (wants to be
  late), and `executor/main.py` deliberately skips journalling a plan decided at or after
  09:30 ET (must be early). The window is about ten minutes wide.
- **watch 09:30** — the open, ten minutes after the plan so the artifact exists.
- **EOD 16:15** — the brief's cadence. Alpaca's consolidated SIP bars are not queryable
  until roughly 15 minutes after the requested `end` (the reason `soak.timer` sits at
  17:30), so 16:15 against a 15:30 hard close is 45 minutes of margin.
- **soak 17:30** — pre-existing, unchanged.

---

## 1. Preconditions

- [ ] `docs/ratification.md` read, at least the R1–R9 entries and §(b).
- [ ] The soak timer is disarmed (audit doc §5 step 1).
- [ ] The `/opt` deploy is on the ruled schema — `grep -c moneyness /opt/paper-hunter/executor/iv_rank.py` returns a non-zero count.
- [ ] `sudo -u paper-hunter /opt/paper-hunter/.venv/bin/python -m executor.soak --dry-run` succeeds.
- [ ] `/etc/paper-hunter/soak.env` exists, mode 0640, root:paper-hunter, **paper** keys only.
- [ ] `.venv/bin/pytest -q` green and `.venv/bin/ruff check --exclude scratch .` clean on the branch you are deploying from.

## 2. Install

```bash
cd <worktree with the units>
sudo cp scripts/plan.service scripts/plan.timer \
        scripts/watch.service scripts/watch.timer \
        scripts/eod.service scripts/eod.timer \
        /etc/systemd/system/
sudo chmod 0644 /etc/systemd/system/{plan,watch,eod}.{service,timer}
sudo systemctl daemon-reload
```

## 3. Verify the units WITHOUT running them

```bash
systemd-analyze verify /etc/systemd/system/plan.service  /etc/systemd/system/plan.timer
systemd-analyze verify /etc/systemd/system/watch.service /etc/systemd/system/watch.timer
systemd-analyze verify /etc/systemd/system/eod.service   /etc/systemd/system/eod.timer
```

Then check each `ExecStart` actually parses against the deployed CLI — this is the check
that caught the stale `--flow-gate` list, and it is worth repeating on the deploy host
because the deploy and the repo are different trees:

```bash
sudo -u paper-hunter /opt/paper-hunter/.venv/bin/python -m executor.main \
    --dry-run --db /opt/paper-hunter/data/journal.db watch --help >/dev/null && echo "watch CLI ok"
```

## 4. One manual run of each leg, in order, before enabling anything

This is the part worth not skipping. Each job is inert in the sense that it places no
orders, but each writes to the same journal the experiment reads.

```bash
sudo systemctl start plan.service
journalctl -u plan.service -n 30 --no-pager
ls -l /opt/paper-hunter/data/plans/$(date +%F).json
```

Read the plan output. A plan that is `NO_TRADE` in every cell is **expected** while T5
is cold — that is the honest state, not a fault. What you are checking is that rows
landed and that the journal grew.

```bash
sudo systemctl start watch.service
journalctl -u watch.service -n 30 --no-pager
```

The loop runs until `--stop-et 16:00`. To test it without waiting, override the clock —
the CLI accepts `--now` and refuses a naive timestamp:

```bash
sudo -u paper-hunter /opt/paper-hunter/.venv/bin/python -m executor.main \
    --dry-run --db /opt/paper-hunter/data/journal.db watch --ticks 3 --now 2026-10-05T14:00:00-04:00
```

Then:

```bash
sudo systemctl start eod.service
journalctl -u eod.service -n 30 --no-pager
```

`cmd_eod` tolerates a missing plan, so an EOD pass that writes `NO-SHOT` rows with a
`NULL` `counterfactual_entry_ref` means the plan step failed — go back to step 4.

## 5. Enable

```bash
sudo systemctl enable plan.timer watch.timer eod.timer
systemctl list-timers 'plan.timer' 'watch.timer' 'eod.timer' 'soak.timer'
```

Expect four rows, each `Mon..Fri` in ET. **Enable, do not start** — `enable` on a timer
arms the trigger; the job runs at its own time.

## 6. Re-arm the soak (last, because the deploy is what makes it correct)

```bash
sudo systemctl enable --now soak.timer
systemctl list-timers soak.timer
```

## 7. After the first real session — check these four things

1. **Four journal writes per session**, in order: plan (pre-open), watch ticks, EOD, soak.
2. **`strategy_version` on every row** is the currently deployed one. A row stamped with an older version is a row the NO-SHOT baseline filter will exclude — silent, and exactly the kind of thing that looks like data until it is counted.
3. **T5 warmth**, with the script delivered alongside this runbook:
   ```bash
   /opt/paper-hunter/.venv/bin/python scripts/verify_t5_warm.py \
       --db /opt/paper-hunter/data/ivrank.db
   ```
   `NOT WARM` is the **expected** first result. It is R8's cost, already ruled, not a
   regression.
4. **Per-key observation counts**, not row counts. A store with hundreds of rows across
   hundreds of keys is a cold store that looks warm. `verify_t5_warm.py` prints the
   per-key table for exactly this reason.

---

## Rollback

```bash
sudo systemctl disable --now plan.timer watch.timer eod.timer
sudo rm /etc/systemd/system/{plan,watch,eod}.{service,timer}
sudo systemctl daemon-reload
```

Rolling back the units does not roll back the rows they wrote. The journal is
append-only and nothing here deletes anything — a disabled cycle leaves the sessions it
already ran, which is the intended behaviour and the reason `docs/ratification.md`
filters the NO-SHOT baseline by `strategy_version` rather than by date.

## Known gaps at the time of writing

- **Arm B cannot warm, by design.** Its frozen DTE band is `0`, Black-Scholes vega is
  identically zero at expiry, and the backfill refuses to invent a volatility. Arm B's T5
  is calendar-bound in perpetuity unless that band is re-ruled. Under R5 (November opens
  A + C) this is inert for the window.
- **`watch.service` pins `--flow-gate carry_forward` explicitly.** That states the policy
  in the unit file instead of inheriting it from the rulebook, so a future rulebook
  change to `intraday_policy` will not be picked up until this line changes too. The
  trade is deliberate — a loud mismatch beats silent drift — but it is a real coupling.
- **`/opt` has no upstream remote.** Its `origin` is `/home/hunter/dev/paper-hunter`, not
  GitHub, so a force-push or a rewrite on the repo side silently diverges the deploy.
  Noted in the audit doc; not fixed here.
- **No weekly rollup timer.** `docs/brief.md` puts the weekly rollup to the operator in
  the EOD leg. `analysis/rollup.py` exists (`tests/test_rollup.py`) but no unit invokes
  it, and none is proposed here — a weekly operator-facing rollup is a different job
  from the three session legs and wants its own decision about timing and delivery.