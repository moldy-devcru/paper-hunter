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