# Soak deploy audit — what is actually running, and what Monday would write

**Date:** 2026-10-03. **Author:** lead (automated audit). **Status:** READ-ONLY
investigation. Nothing was restarted, redeployed, enabled, or written. **Operator action
required before Monday 2026-10-05 17:30 ET.**

---

## 1. The short answer

**Yes. The live soak is recording the wrong schema, and it will do so again on Monday
unless it is deployed first.**

More precisely, there are two distinct schema generations in play and the live job is on
the older one:

| | schema | who writes it | who reads it | status |
|---|---|---|---|---|
| gen 1 | raw integer DTE, dollar `$5` bucket — `dte42-call-770.00` | soak @ commit ≤ pre-R4 | nothing reads this | **the 28 rows already in the store** |
| gen 2 | 7-day DTE bucket, dollar `$5` bucket — `dte42-call-770.00` | soak @ deployed `develop` | T5 on `develop` | **what `/opt` runs today** |
| gen 3 | 7-day DTE bucket, **moneyness 2%** — `mte42-call-mny+0.00` | soak on `feature/frozen-rule-rulings` | T5 on that branch | **the RULED schema (R8)** |

`/opt/paper-hunter` runs gen 2. The ruled schema is gen 3. **The next soak run under the
current deploy writes another 28 gen-2 rows that gen 3 will never look up** — the same
orphan-by-construction R7 disposition, arrived at again by leaving the timer armed.

Note that gen 1 and gen 2 share the `dte<...>-call-<dollars>` spelling. Nothing in the
store records which axis produced a key, which is exactly the collision hazard R8's
`mte`/`mny` namespacing exists to prevent going forward.

---

## 2. Where the live soak actually runs

| question | answer | evidence |
|---|---|---|
| installed unit | `/etc/systemd/system/soak.service`, `/etc/systemd/system/soak.timer` | `systemctl status` |
| unit matches the repo? | **byte-identical** to `scripts/soak.service` and `scripts/soak.timer` on this branch | `diff` — no differences |
| timer state | **enabled**, `Active: active (waiting)`, next trigger `Mon 2026-10-05 17:30:00 EDT` | `systemctl status soak.timer` |
| service state | `inactive (dead)`, last run **Fri 2026-10-02 19:21:53 EDT**, `status=0/SUCCESS` | `systemctl status soak.service` |
| last run's own words | `soak: 2026-10-02 recorded (row 1) — deep-OTM>=3% call=24485 (n/ax 20d) put=42955 (n/ax), 28 IV row(s), 1 expiry(ies) skipped` | journal |
| `WorkingDirectory` | **`/opt/paper-hunter`** — not `~/dev/paper-hunter` | unit file |
| `ExecStart` | `/opt/paper-hunter/.venv/bin/python -m executor.soak` | unit file |
| credentials | `EnvironmentFile=-/etc/paper-hunter/soak.env` (mode 0640 root:paper-hunter). `/etc/paper-hunter` is root-only — not readable as `hunter`, and not needed for this audit | unit file |
| hardening | `ProtectSystem=strict`, `ReadWritePaths=/opt/paper-hunter/data`, `User=paper-hunter` | unit file |

**So the live soak runs from the `/opt` deploy, not from any `~/dev` worktree.** That
matters for the whole Monday plan: editing a worktree changes nothing about what runs.

### The deploy's git topology

```
~/dev/paper-hunter          origin = github.com-moldy:moldy-devcru/paper-hunter
  develop                   2bf864d  (outsider algo review)
  feature/frozen-rule-rulings 1107b16  ← 22 commits ahead, CONTAINS develop

/opt/paper-hunter           origin = /home/hunter/dev/paper-hunter   (a local clone, not github)
  develop                   6ae4b19  ← TWO COMMITS BEHIND ~/dev's develop, and 22+ behind the feature branch
  working tree              clean
```

`feature/frozen-rule-rulings` is a strict descendant of `develop`
(`git merge-base --is-ancestor develop feature/frozen-rule-rulings` → true), so deploying
is a **fast-forward**, not a merge. There is no divergence to reconcile and no merge
conflict to reason about.

---

## 3. What the deployed code can and cannot do

Measured on `/opt/paper-hunter` directly:

| check | `/opt` (deployed) | `feature/frozen-rule-rulings` (ruled) |
|---|---|---|
| `moneyness` occurrences in `executor/iv_rank.py` | **0** | present (`moneyness_tenor_key`, `MONEYNESS_BUCKET_PCT`) |
| `moneyness_tenor_key` in `executor/soak.py` | **0** | present — soak writes gen-3 keys |
| `executor/iv_backfill.py` | **absent** | present (the R9 backfill) |
| R4 fix (`4318cdf`, 7-day DTE bucket) | **present** | present |
| rulebook `strategy_version` | `1.2.0-draft+9c5200188e0e` | `1.4.0-draft+071095af7402` |

(measured by loading each tree's `config/rules.example.yaml` through its own
`config.loader.load_rules`; the `+<12 hex>` suffix is the `config_sha256` digest R8/R9
move.)

So `/opt` has R4 but **not R8, not R9**. The `git log` dates explain the store contents:
the R4 fix landed at **2026-10-03 01:24**, but the only soak run on record is
**2026-10-02 19:21** — written by code that predated R4. That is why the `dte` keys in
the store are `dte3, dte4, dte5, dte6, dte7, dte10, dte11 …` (raw DTE, one observation
each) rather than 7-day multiples.

### The deployed store, read-only

`/opt/paper-hunter/data/ivrank.db` — 28 rows, **28 distinct keys, 1 observation per key**:

- **14 expiry-keyed** (audit series): `20261005-call-770.00` … `20261113-call-770.00`
- **14 gen-1 DTE-keyed**: `dte3` … `dte42`, all `-call-770.00`
- all `as_of = 2026-10-02`, all `source = 'alpaca_chain'`, one `strike = 770.0`,
  14 distinct expiries, all **calls**.

Calls-only is not a fault: the stored spot was ~770, so the nearest-to-spot contract in
arm C's 90–180 DTE band is a call, and arm B's 0-DTE band produced nothing (arm B cannot
be recorded at 0 DTE by design — see §5 risk 3).

---

## 4. What happens if Monday runs undeployed

`Mon 2026-10-05 17:30:00 EDT` the armed timer fires. Nothing about the deploy has changed
the unit, so:

1. `/opt`'s `executor.soak` runs and writes ~28 more **gen-2** rows (`dte<N>-call-<dollar>`).
2. Every one of them is **unreachable** by the ruled T5 reader, which asks for
   `mte<bucket>-<side>-mny±<pct>`.
3. T5 stays `PENDING` on Monday night exactly as it would if the timer were disabled —
   **but the store now carries a week of gen-2 rows that look like progress and are not.**

That third point is the actual risk. A store with 56 rows across 56 keys reads as "the
soak is running" to anyone checking row counts rather than per-key counts. Under R7's own
reasoning, a warm-looking store is worse than an honestly cold one, because it moves the
blocker somewhere nobody is looking. **This is the one finding in this audit that is time-
critical: the timer fires in ~49 hours and each day of delay adds a block of unbackfillable
orphans.**

It is *not* irreversible — a backfill under R9 writes into the ruled keys regardless, and
gen-2 rows are pure noise rather than contamination. It is just noise that grows.

---

## 5. Deploy plan (operator approval required — nothing below was executed)

**Preconditions:** full pytest green; `ruff check` clean; operator has read this document.

### Step 1 — freeze the timer (do this first, before any code moves)

```bash
sudo systemctl disable --now soak.timer
systemctl list-timers soak.timer        # expect: nothing listed
```

The service is `Type=oneshot` and already inactive, so this only disarms the trigger.
Nothing is lost: `Persistent=true` means a missed run catches up, and a run under the
new code produces rows the old code would not have anyway.

### Step 2 — fast-forward the deploy

```bash
cd ~/dev/paper-hunter
git checkout develop
git merge --ff-only feature/frozen-rule-rulings
git log --oneline -1                    # expect 1107b16 or later
```

`--ff-only` is the point: if it is not a fast-forward, **stop** — the branch topology has
changed since this audit and the plan needs re-reading.

```bash
sudo -u paper-hunter git -C /opt/paper-hunter fetch origin
sudo -u paper-hunter git -C /opt/paper-hunter merge --ff-only origin/develop
sudo -u paper-hunter git -C /opt/paper-hunter log --oneline -1
grep -c moneyness /opt/paper-hunter/executor/iv_rank.py     # expect: non-zero
```

The last two commands are the **acceptance check**. `moneyness` occurring **0** times in
the deployed `iv_rank.py` means the deploy did not land, whatever git says.

### Step 3 — dry run the new soak before letting it write

```bash
sudo -u paper-hunter /opt/paper-hunter/.venv/bin/python -m executor.soak --dry-run
```

Expected: one `flow_baseline` row and two IV rows per contract, the moneyness-keyed one
named `mte<bucket>-call-mny+0.00`-shaped. A `--dry-run` that still prints `dte<...>-call-<dollars>`
means the old code is still what runs.

### Step 4 — one manual run, then inspect the actual key written

```bash
sudo systemctl start soak.service
journalctl -u soak.service -n 20 --no-pager
```

```bash
/opt/paper-hunter/.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/opt/paper-hunter/data/ivrank.db?mode=ro", uri=True)
for k, n in c.execute("select tenor_key, count(*) from iv_observations "
                      "where tenor_key like 'mte%' group by 1 order by 1"):
    print(f"  {k}  n={n}")
PY
```

Expect rows with a `mte…mny…` prefix. **If that query returns nothing, do not re-arm the
timer** — the deployed writer is still on the dollar axis and the whole point is undone.

### Step 5 — re-arm

```bash
sudo systemctl enable --now soak.timer
systemctl list-timers soak.timer        # confirm Mon 17:30 ET
```

### Step 6 — run the T5-warmth check (delivered alongside this audit)

```bash
/opt/paper-hunter/.venv/bin/python scripts/verify_t5_warm.py \
    --db /opt/paper-hunter/data/ivrank.db
```

Expected on the first post-deploy session: **`NOT WARM`**, exit 1, with per-key counts of
1 against a floor of 60. That is the *correct* reading, not a failure — R8 restarted the
warm-up by ruling, and the store is now at least filling a series that can reach the floor.

---

## 6. Risks and open questions

1. **Every already-recorded gen-1 and gen-2 row becomes permanently unreachable.** No
   re-keying, no migration, no deletion — R7's disposition, extended to the new deploy.
   The 14 expiry-keyed rows are the exception: the ruled soak **still writes the
   expiry-keyed dollar series** as its audit axis, so those keys are not orphaned by R8
   and will simply be re-written. They are audit series, not what T5 reads, and per the
   `tenor_key` docstring they can never reach 60 observations in any case.
2. **T5's warm-up restarts from zero for the second time.** Already ruled (R8, R9) and
   accepted; recorded here because the deploy is what makes it real. Cost is ~60 sessions
   at one observation per session per key.
3. **Arm B cannot warm at all, by design, and no deploy changes that.** Its frozen DTE
   band is `0`, Black-Scholes vega is identically zero at expiry, and the backfill refuses
   to invent a volatility. Arm B's T5 is calendar-bound in perpetuity unless the band is
   re-ruled. Under R5 (November opens A + C) this is inert for the window, but it should
   not be discovered for the first time in the v2 cycle.
4. **`/opt` is a local clone of a local clone, not of GitHub.** Its `origin` is
   `/home/hunter/dev/paper-hunter`. There is no upstream remote behind it, so a
   `git pull` on the wrong host, or a force-push rewrite of `develop`, silently diverges
   the deploy from the real repo. Worth fixing in its own change; noted, not fixed here.
5. **The strategy-version seam.** R8/R9 bump the rulebook to `1.4.0-draft`, which moves
   `config_sha256` (`Rulebook.fingerprint()` hashes raw file bytes). Every draft row stamps
   `strategy_version`, so the NO-SHOT baseline is isolable by version filter — nothing is
   deleted. But: deploying means the shipped rulebook **changes bytes**, and the rehearsal's
   integrity stage asserts the shipped rulebook's sha256 is unchanged across a run. Re-run
   `scripts/rehearse.sh` against the deployed tree after the merge to confirm the new
   rulebook loads and the integrity stage still holds.
6. **The soak is not the only deployed code.** `paper-hunter-ui.service` also runs from
   `/opt` and will pick up the new code on its next restart. It opens its databases
   `mode=ro`, so no data risk; but its `/calibration` page reads the new
   `executor.calibration`, and the view has not been re-checked against the merged code.
   Restart it and look at the page once, after step 4.

## 7. What was NOT done

No service was started, stopped, restarted, enabled or disabled. No unit file was
installed. No code in `/opt` was modified. No row in any database was written, read in
write mode, or deleted. The IV store and journal were opened `mode=ro` for this audit,
and `/etc/paper-hunter/` was not readable and was not escalated.