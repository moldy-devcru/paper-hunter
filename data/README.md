# Data layer notes

Runtime data files for the executor. Nothing here is a source file — the code reads and
writes all of it, and the paths are documented so a reader does not have to grep.

## `ivrank.db` — IV-rank history store (created on first write)

Owned by [`executor/iv_rank.py`](../../executor/iv_rank.py). **Separate database, not a
table in the journal DB**, and that is a deliberate call:

- The journal is an append-only ledger with trigger-enforced UPDATE/DELETE. The IV store
  is a *mutable* time series that gets re-read and re-scored.
- Putting an overwritable table underneath triggers written for a different purpose is
  how a "harmless" correction starts failing loudly, or worse, stops being corrected.
- Separating them means a corrupt IV rebuild can never touch the experiment record.

Table: `iv_observations(underlying, as_of, tenor_key, expiry, right, strike, iv, source,
is_proxy, created_at)`, unique on `(underlying, as_of, tenor_key, source)`.

### Why we build it ourselves

No free source has point-in-time historical IV (research note §1: Alpaca serves IV only
as a current snapshot field with historical option data starting Feb 2024; Tradier and
Massive have none either). So the real series is accumulated by the executor's daily EOD
chain poll, one row per contract per day, and T5 reads PENDING until a tenor has enough
of it. That is the whole reason this store starts *before* the trading window opens.

### Warmup floor and the proxy series

- `MIN_OBSERVATIONS = 60` — about three months of trading days. Not a year, and not a
  substitute for one. Chosen as the smallest window where a percentile rank is not
  dominated by a handful of readings.
- Below the floor, `iv_rank()` returns `None` with `status="warmup"`. The checklist turns
  `None` into PENDING, which **blocks** — an under-built history can never wave a trade
  through.
- [`scripts/seed_ivrank.py`](../../scripts/seed_ivrank.py) backfills the free Cboe VIX
  daily CSV as a **warmup seed only**. Those rows carry `is_proxy=1` and are filed under
  the `VIX` underlying, so two independent guards (the flag and the key) would both have
  to be wrong for VIX data to reach a real SPY IV rank. The rows are never deleted at the
  switchover, so the seam stays visible in the store instead of disappearing from it.

## `journal.db` — the experiment ledger (created on first write)

Owned by [`journal/store.py`](../journal/store.py), schema in
[`journal/schema.sql`](../journal/schema.sql). Append-only, trigger-enforced.

### `flow_baseline` — T6's numerator and baseline, accumulated a session at a time

One immutable row per trading session, written by `executor/soak.py`. This is where the
20-day baseline T6 compares against comes from: there is no free source for historical
SPY deep-OTM 0DTE volume, so the history has to be built before the window opens, one
row per day, and the calibration (`N`) reads off the distribution that accumulates here.

- `date` is UNIQUE, so a same-day re-run cannot insert a second row; the writer checks
  first and reports a no-op rather than swallowing a duplicate.
- `feed` / `is_delayed` / `session_spot` / `chain_contracts` are mandatory provenance.
  These numbers come from the Indicative Pricing Feed (~15-min-delayed, synthetic), and a
  flow figure without its feed attached is not auditable.
- `deep_otm_threshold_pct` is stored per row. A threshold change is a rule change, and
  rows aggregated at different thresholds are never mixed (the baseline builder excludes
  them, so a change visibly restarts the day count rather than creating a jump).
- `call_volume_by_distance` / `put_volume_by_distance` are the raw 1-percentage-point
  histogram. Any *integer* threshold re-derives exactly from them, so revising the
  threshold later costs nothing and needs no re-fetch.
- Baseline means and the T6 ratios are computed from **prior** rows only. A session never
  counts itself in its own baseline.

## `soak.env` — credentials for the soak timer (NOT in this directory)

`scripts/soak.service` reads `/etc/paper-hunter/soak.env` via `EnvironmentFile`, and the
line is prefixed with `-` so a missing file makes the unit fail with a readable message
rather than a silent no-op.

That file is **deliberately not created here and must not be committed**. It belongs
outside the repo for the ordinary reason (a secret in git is a secret in every clone and
every fork), plus a project-specific one: this repo is a pre-registered experiment whose
value depends on nobody quietly editing its inputs. A credential file inside the tree is
an input to the tree.

Create it once, on the host, as root:

```sh
sudo install -o root -g paper-hunter -m 0640 /dev/null /etc/paper-hunter/soak.env
sudoedit /etc/paper-hunter/soak.env
```

with exactly:

```
ALPACA_PAPER_KEY=<paper key>
ALPACA_PAPER_SECRET=<paper secret>
```

`0640 root:paper-hunter` — readable by the job's user, not by anything else on the box.
**Paper credentials, never live ones.** This job only reads; there is no reason for it
to hold keys that can place real orders, and a job that can is a job that eventually
does. The same two variables serve `executor/main.py` and `scripts/seed_ivrank.py`, so
one file covers the whole executor.

Verify without the network: `sudo -u paper-hunter /opt/paper-hunter/.venv/bin/python -m
executor.soak --dry-run` (fixtures, writes nothing). Verify with it:
`sudo systemctl start soak.service && journalctl -u soak.service -n 20`.

## `events/` — the T5 hard veto

Owned by [`event_calendar.py`](event_calendar.py). See [`events/README.md`](events/README.md)
for the refresh procedure. The calendar is data, not code: adding an event is a YAML edit
plus a commit, so the veto cannot be silently changed by a deploy.

## Alpaca feed notes that affect stored data

From the research note, and load-bearing for anything written to these databases:

- Free-tier equity realtime is **IEX only**. Relative volume (T4) is only valid when the
  numerator and the 20-day baseline come from the same feed, so the builder refuses to
  compute across feeds rather than quietly returning a number that compares two different
  universes.
- Free-tier options quotes are the **Indicative Pricing Feed** — synthetic derivatives of
  OPRA, with trades delayed 15 minutes. Fine for a paper experiment; recorded on the ledger
  because indicative quotes distort paper fills.
- Historical SIP bars require an `end` at least 15 minutes old. `alpaca_client` surfaces
  that check (`sip_end_is_queryable`) and does not silently rewrite a caller's `end`.