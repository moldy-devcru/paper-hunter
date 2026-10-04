# Live seam sweep — every market-data read path, audited once

**Date:** 2026-10-03. **Branch:** `fix/plan-daily-series` (tip `89a322a`, parent `ede6342`).
**Scope:** every code path in this repo that reads live market data — `executor/main.py`
subcommands `hunt-plan` / `watch` / `eod`, `executor/soak.py`, the backfill sources, the
UI bar cache, and the operator scripts. **Status:** 3 seams fixed, 11 call sites audited,
0 pushed.

---

## 1. Why this document exists

`ede6342` fixed one seam of a bug class. Running the other two legs by hand against the
deployed code immediately produced two more, and then a sweep found a third. Three
consecutive live-path bugs, all shipped through a fully green suite.

The suite was not lying. It was answering a different question. Every fixture in this
repo's test suite **always answers bars** — a mock that hands back a payload no matter
what the query said cannot observe that the query never asked for a span. So "the read
works" was tested with the one condition that never occurs live.

That is the thing worth fixing, and it is why this document is longer than a changelog.

---

## 2. The two failure shapes

### (a) A bars read with no resolvable span answers **HTTP 200 with zero bars**

Measured 2026-10-03, paper credentials, `GET /v2/stocks/SPY/bars`:

| query | bars returned |
|---|---|
| `timeframe=1Day&feed=sip&limit=400` | **0** |
| `timeframe=1Day&feed=iex&limit=400` | **0** |
| `timeframe=1Day&feed=sip&end=<now-20m>&limit=400` | **0** |
| `timeframe=1Day&feed=sip&start=<window>&sort=desc&limit=400` | **400**, newest 2026-10-02 |
| `timeframe=1Day&feed=sip&start=<window>&limit=400` (no `sort`) | newest only, through 2026-09-30 |

Three facts, all measured, none guessed:

1. **No error, no warning.** An empty result set is indistinguishable from "no trading
   occurred." Nothing in the client, the loop, or the log says "your query was malformed."
2. **An `end` alone is not a window.** Asking for everything up to now returns nothing.
   This is the non-obvious one, and it is why `soak` shipped a second copy of the same bug
   wearing a different hat.
3. **`sort` is load-bearing, not cosmetic.** Without `sort=desc` the route truncates at
   the *newest* edge of the window and returns a stale tail — a different wrong answer
   from a nearly identical call.

The behaviour that makes this class expensive: **several of these reads swallow the
emptiness into a fallback.** `soak` reported `SoakError("no usable SPY close")` — a
sentence blaming the market for a missing span in the query. The EOD pass reported
`InsufficientData: EMA(50) needs at least 50 values, got 0` from three layers below the
call that caused it. Both read like data problems. Both were query problems.

### (b) A store accessor returns a different type than the reader assumes

`store.observations()` returns `IvObservation` — a stored *reading*. `rank` is a field of
`IvRankResult` — what `store.iv_rank()` **returns**. The watch leg read `o.rank` off the
former.

The reason no test caught it: `observations()` on an empty store returns `[]`, the loop
body never ran, and every fixture-based test used a fresh store. The first run against a
store holding real backfilled rows went straight to `AttributeError`. The guard was never
wrong; it was never asked.

Live evidence:

```
/home/hunter/dev/ph-debate/data/ivrank.db   342 rows, 111 tenors
    mte91-put-mny+0.00            90   <- warm (MIN_OBSERVATIONS = 60)
    mte91-call-mny+0.00           81   <- warm
    sources:   bars_bs_inversion x342
    origins:   backfill x342
    -> IV rank 13.75, as of 2026-09-18

/opt/paper-hunter/data/ivrank.db             28 rows, 28 tenors
    -> no tenor reached MIN_OBSERVATIONS; T5 honestly reports not-warm
```

Two stores, same code, opposite behaviour — which is exactly what a latent type seam
looks like from the outside. The 28-row one would have reported `None` (wrongly) and the
342-row one would have raised.

---

## 3. The sweep

Every call site in the repo that constructs an `AlpacaClient` or reaches a market-data
read. "verified" means the call is asserted by a named test; "fixed" means it was a seam
and now is not.

| # | call site | read | window? | end-clamp? | type-safe? | status |
|---|---|---|---|---|---|---|
| 1 | `watch_loop.AlpacaWatchData.daily_series` | `get_daily_bars` 1Day | `**bar_window` | sip clamp | n/a | **fixed** (`ede6342`) |
| 2 | `watch_loop.AlpacaWatchData.watch_snapshot` | `get_intraday_bars` | `**bar_window` | realtime feed, no clamp | n/a | **fixed** (`ede6342`) |
| 3 | `watch_loop.AlpacaWatchData._run_rate_series` | `get_intraday_bars` | `**bar_window` | realtime feed | n/a | **fixed** (`ede6342`) |
| 4 | `main.cmd_hunt_plan` → `AlpacaWatchData(client)` | via 1–3 | inherited | inherited | n/a | **verified** — `test_hunt_plan_over_a_windowed_mock_writes_a_plan` |
| 5 | `main._watch_iv_rank` | `store.observations()` → `.rank` | n/a | n/a | **NO** | **fixed** (`eff84c6`) |
| 6 | `main.cmd_eod` | `get_daily_bars` 1Day sip | **was `limit` only** | was a hand-rolled backoff | n/a | **fixed** (`34f1aba`) |
| 7 | `main.cmd_eod` | `get_option_chain` | n/a — not a time series | n/a | n/a | **verified-ok** |
| 8 | `main._eod_client` (`--offline`) | `MockTransport` | n/a | n/a | n/a | **verified-ok** — `tests/test_eod_offline.py` |
| 9 | `soak._AlpacaSource.daily_bars` | `get_daily_bars` 1Day | **was `end` only** | was a hand-rolled backoff | n/a | **fixed** (`89a322a`) |
| 10 | `soak._AlpacaSource.option_chain` | `get_option_chain` | n/a | n/a | n/a | **verified-ok** |
| 11 | `soak` store reads → `_collect_chain_iv` / `_iv_series` | `store.observations()` | n/a | n/a | yes — `iv`/`tenor_key`/`as_of` | **verified** — `test_every_attribute_a_live_reader_touches_exists_on_a_store_row` |
| 12 | `backfill_flow.AlpacaIvSource.equity_daily_bars` | `get_daily_bars` 1Day | explicit `start`/`end`, `limit=10000` | caller-supplied | n/a | **verified-ok** — window is the caller's job and it supplies one |
| 13 | `iv_backfill.AlpacaIvSource.equity_daily_bars` | `get_daily_bars` 1Day | explicit `start`/`end` | caller-supplied | n/a | **verified-ok** — same shape as 12 |
| 14 | `ui/barcache.BarCache._fetch` | `get_daily_bars` / `get_intraday_bars` | explicit `start`/`end` from the gap calc | `sip_end_is_queryable` backoff | n/a | **verified-ok** — the UI already had the clamp policy |
| 15 | `ui/barcache.default_client` | `AlpacaClient(...)` construction | n/a | n/a | n/a | **verified-ok** — lazy, rate-limited |
| 16 | `scripts/backfill_iv_rank.py` | `AlpacaIvSource` | via 13 | inherited | n/a | **verified-ok** |
| 17 | `scripts/verify_t5_warm.py` | `store.observations()`, `store.iv_rank()` | n/a | n/a | **yes** — reads `tenor_key` only | **verified-ok** — the correct pattern, and the model for the fix |

**Totals: 17 call sites audited. 4 seams found and fixed (#5, #6, #9, plus the shared-policy
extraction). 13 verified sound.**

`get_option_chain` and the `/v1beta1/options/snapshots` calls (#7, #10) do not take a
time-series window and were checked against the client's own contract, not the bars
policy — they are out of scope for this window rule and are noted so the omission is
visible rather than silent.

---

## 4. The fix: one policy, not four call sites that agree today

`bar_window` was written inside `executor/watch_loop` for one caller. Three other reads
then needed the same answer and each found it — or failed to — for itself.

It now lives in **`executor/bar_windows.py`** and holds its constants
(`BARS_PER_SESSION`, `SESSIONS_PER_CALENDAR_DAY`, `WEEKEND_HOLIDAY_PADDING_DAYS`,
`SIP_END_BACKOFF_MINUTES`, `REALTIME_FEEDS`). `watch_loop.bar_window` remains as a
re-export so existing callers keep working, and `tests/test_live_seams.py` asserts the
identity `watch_loop.bar_window is bar_windows.bar_window` — **by identity, not by
behaviour**, because a copied implementation would pass a behaviour test and fail a
second week.

`cmd_eod` (`EOD_BARS_LIMIT = 400`, `EOD_BAR_FEED = "sip"`) and
`soak._AlpacaSource` both import it. Neither re-derives it.

---

## 5. What would have caught all of this

### 5a. A mock with the live route's contract

The single highest-value change, and it is small. `tests/test_live_seams.py` defines one
helper and every regression uses it:

```python
def _bars_route(payload, *, require="start"):
    """The live route's contract: a query with no resolvable span answers zero bars."""
    def route(params):
        if not params.get(require):
            return {"bars": [], "symbol": "SPY", "next_page_token": None}
        return payload
    return route
```

A fixture that **refuses** windowless reads. This is what turns "the read works" into
"the read works when the read is correct."

### 5b. A static sweep, so the *next* one is a failing test

`test_no_live_bars_read_in_the_repo_is_limit_only` walks every `.py` in the repo with
`ast`, finds every call to `get_daily_bars` / `get_intraday_bars` / `get_bars`, and fails
if any of them passes neither an explicit `start`/`end` nor a `**bar_window(...)` splat.

It covers call sites no test injects a transport into — `ui/barcache`, the backfill
sources — which is where the remaining risk actually lives. `scratch/` is excluded
deliberately and by name: `probe_window_probe.py` sends the windowless shapes **on
purpose**, because the measurement is the evidence. Sweeping the evidence would be
demanding that the proof commit a crime.

### 5c. The attribute protocol, stated as a test

`test_every_attribute_a_live_reader_touches_exists_on_a_store_row` materialises an
observation **from a row** and reads every attribute live readers touch: `underlying`,
`as_of`, `tenor_key`, `iv`, `source`, `is_proxy`, `expiry`, `right`, `strike`,
`created_at`, `origin`. A future column drift — a backfill writing fewer columns than a
live poll — fails at the boundary instead of at the far end of a trading session.

And its companion asserts the negative: **an `IvObservation` must not have `.rank`.** If
it ever does, the test says so and the `iv_rank()` call becomes redundant. The distinction
the fix turns on is pinned, not left in a comment.

---

## 6. What a CI-able offline smoke run needs, per leg

Spec only — no CI was built, and none should be until the gaps below are closed.

| leg | entry point today | what it needs |
|---|---|---|
| `eod` | **has one.** `--offline --offline-fixture` builds a real `AlpacaClient` over `MockTransport`. | The fixture is a static payload map, so it does **not** refuse windowless reads. Give the fixture route the same window contract as `_bars_route` above and this leg is smoke-gated end to end. Small change, high value. |
| `soak` | **has one.** `--dry-run` swaps in `_FixtureSource`, whose transport raises on any call. | Same gap. Swap its transport for the window-refusing route and the silent-0-bar degradation cannot survive a green run. |
| `watch` | **none.** | `cmd_watch` calls `AlpacaClient.from_env()` unconditionally. It needs the same `--offline-fixture` swap `eod` has, plus a plan file and an `IvRankStore` on disk (both already constructible from `tests/fixtures_synthetic.py`). **This is the leg that had the most seams and it is the one that cannot currently be rehearsed.** |
| `hunt-plan` | **partial.** Covered by a MockTransport test since `ede6342`. | `cmd_hunt_plan` builds its client from env too, so the same `--offline` swap would cover the CLI subcommand, not just the library call. |

`test_the_three_legs_all_have_an_offline_entry_point` asserts what is true today and
records the `watch` gap as an **asserted absence** — so the question "is it still missing?"
stays answerable, and the test tells you to close it for real the day someone does.

---

## 7. Evidence

```
$ python -m pytest tests/test_live_seams.py -q
12 passed

$ git checkout ede6342 -- tests/test_live_seams.py   # run the suite on the parent
8 failed, 4 passed
```

The 8 failures are the regressions. The 4 that pass on `ede6342` are forward guards — the
attribute protocol, the type distinction, the offline-entry-point spec, and the probe's
presence — which are supposed to pass on both commits.

Full suite on this branch: **1242 passed, 2 failed** → both failures were the generated
ratification document's line numbers shifting with `executor/main.py`; regenerated with
`python scripts/gen_ratification_interpretations.py --write`, after which
`tests/test_ratification_doc.py` is 10/10 and the whole suite is green.

Ruff clean on every file touched: `executor/bar_windows.py`, `executor/main.py`,
`executor/soak.py`, `executor/watch_loop.py`, `tests/test_live_seams.py`,
`scratch/probe_seam_sweep.py`.

The measurement script is committed at `scratch/probe_seam_sweep.py`. It reads credentials
from `~/.config/paper-hunter/alpaca.env` **in-process** (never printed), makes minimal
paced calls, and prints only counts and bar dates.

---

## 8. The one-line version

Every live read in this repo now names a window it can actually resolve, through one
policy that lives next to the data rather than next to a caller — and the fixture that
tells the difference between a working read and a malformed one is in the suite, because
a mock that always answers bars is what let three of these ship green.