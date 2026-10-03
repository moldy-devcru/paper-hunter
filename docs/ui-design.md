# paper-hunter UI — design spec (v1, 2026-10-02)

Author: Moldy (lead), from operator intent expressed 2026-10-02 in #moldy.
Status: DESIGN — implementation dispatched to subagents in phases U1-U4.

## Operator requirements (verbatim intent)

- Candlestick chart, red/green, timeframes: 5m, 10m, 15m, 30m, 1h, 4h, 1d, 1w, 1M, 1Q.
- Searchable, sortable, zoom in/out.
- Toggle technical indicators on/off.
- Buy/sell signal markers per arm.
- Arm account holdings, trade list, plus the signal-surfacing panels already scoped
  (equity race, NO-SHOT hunting ledger, veto histogram, calibration gauge, rollups).
- **LAN-only for now.** No public subdomain. Read-only, always.

## Architecture

```
[Alpaca free tier] ──fetch──> [bar cache sqlite] ──┐
[journal.db (RO)] ────────────────────────────────┼──> [FastAPI server] ──HTTP──> [static SPA]
[ivrank.db (RO)] ─────────────────────────────────┘     yharnam LAN :8478        browser (mads)
```

- **Server:** FastAPI + uvicorn, bound to LAN interface only (no tailscale, no edge nginx,
  no public DNS). systemd user service `paper-hunter-ui.service`. Python 3.12, repo venv.
- **Read-only by construction:** every DB opened `file:...?mode=ro`; the server exposes
  ZERO write endpoints (a test asserts no non-GET routes exist). The UI cannot touch the
  experiment. Same append-only guarantee end to end.
- **Frontend:** static files served by FastAPI (`ui/static/`), NO build chain (no npm, no
  webpack, no React). Vanilla ES modules + one vendored library. Rationale: dependency-light,
  auditable, and a build chain is a maintenance tax on a research tool.

## Charting library: TradingView lightweight-charts (MIT, vendored locally)

Chosen because it IS the operator's requirement list, natively:
- candlesticks (red/green configurable), time-scale zoom/pan (mouse wheel + pinch),
  crosshair with OHLCV readout — all built in, canvas-rendered, ~45KB;
- line/histogram series for indicator overlays (EMA, Bollinger) and subpanes (RSI, MACD);
- **series markers** = per-arm buy/sell/roll/stop annotations anchored to bars;
- vendored into `ui/static/vendor/` (no CDN at runtime — LAN tool must work internet-down).

Timeframe handling: server aggregates from cached Alpaca bars —
1m bars → 5/10/15/30m/1h/4h (server-side bucketing, session-aware: buckets never span
the 09:30 open or cross 16:00 close; 4h buckets anchor at open, last bucket partial).
Daily bars → 1d/1w/1M/1Q (calendar bucketing, ET sessions). Free-tier reality: intraday
bars >15min old are full SIP; the last 15 minutes are IEX-only — the UI stamps the feed
provenance on every chart ("SIP consolidated" vs "IEX partial, <15min stale") instead of
pretending. The chart is a research view, not an execution view; the executor has its own
staleness guards.

## Indicators: computed server-side by `executor/indicators.py`

Deliberate dogfooding: the UI shows EMA(50/200), RSI(14), MACD(12,26,9), Bollinger(20,2)
computed by THE SAME CODE the checklist evaluates — what mads sees is bit-identical to what
the algo saw at decision time. Toggleable per-series in the UI; overlays as line series on
the main pane, RSI/MACD as synced subpanes (lightweight-charts multi-pane via synced time
scales). Volume histogram with relvol coloring (≥1.5x baseline highlighted — T4 made visible).

## Signal markers

From journal `decisions` (kind TRADE/STOP/ROLL/PROPOSAL/VETO) joined to bars by timestamp:
- Arm A: gray (it trades once; that's the point)
- Arm B: amber markers — entry ▲ / exit ▼, time-stops as ✕, 15:30 hard-close as ⏐
- Arm C: blue markers — entry/roll/exit-all
- Exception-path (catalyst clause): magenta, always visually distinct (separate P&L, separate color)
- NO-SHOT near-misses: hollow marker + click → popup with the failed conditions and the
  counterfactual outcome ("we skipped this; it would have made/lost X"). THE hunting panel
  lives directly on the chart, not only in a table — you should be able to see discipline happen.

## Pages (SPA, hash-router, 5 tabs)

1. **Terminal** — the chart above. Default: SPY 15m, EMAs on, today's session.
2. **Arms** — three cards (A/B/C): bankroll, deployed, cash, open positions (live from
   Alpaca paper + journal cross-check, mismatches flagged loudly), per-arm equity curve,
   P&L vs control. Shadow-roll sim curve overlaid on C's card.
3. **Ledger** — every trade/decision row: searchable (symbol/arm/kind/reasoning text),
   sortable (any column), filterable by date range. Journal rows are immutable; the UI
   renders `references` chains as "correction of #N" links.
4. **Hunt** — today's hunt plan (per direction × arm: checklist grid, trigger prices,
   watch levels), NO-SHOT ledger with counterfactual outcomes, veto histogram (which
   condition says no most, over time — stacked bars per week).
5. **Calibration** — the T6 story in pictures: per-session deep-OTM call/put volume bars
   with the 20-session baseline mean and the forming P90 line; threshold-distance histogram
   (the 1pp buckets the soak stores); IV-rank history (proxy vs real provenance distinguished
   by color); window-start checklist status rendered from `Rulebook.pending_calibrations`.

Reports page deferred: weekly rollup markdown posts to Discord (channel 1555737766292365332)
via the OpenClaw message path — the chat IS the report surface; duplicating it in the UI
adds nothing until there's history worth browsing. (U4 may revisit.)

## API surface (all GET, all read-only)

```
/api/health                     → {status, dbs, feed_provenance}
/api/bars?tf=15m&from=&to=      → [{t,o,h,l,c,v,feed}]   (cached, session-aware buckets)
/api/indicators?tf=15m&set=ema50,ema200,rsi,macd,bb
/api/signals?from=&to=&arm=     → [{t,price,arm,kind,journal_id}]
/api/arms                       → per-arm bankroll/positions/equity curve points
/api/trades?q=&arm=&kind=&sort= → paginated ledger rows
/api/huntplan?date=             → today's/any day's plan cells
/api/noshots?from=&to=          → entries + counterfactual outcomes
/api/histogram?veto=weekly      → veto counts per condition per week
/api/calibration                → flow_baseline rows, P90 status, ivrank series, pending list
/api/rollup?week=               → weekly_rollup_text markdown (for archival browsing)
```

Bar cache: `data/barcache.db` (sqlite, mutable — it's a CACHE, not a record; rebuildable
by re-fetching; explicitly outside the append-only zone, documented as such). 200 req/min
free-tier rate limit respected via the Phase-3a RateLimiter; cache-first fetch strategy
(never re-pull history we already hold).

## Build phases (bunny labor, lead-reviewed seams)

- **U1 — API server + bar cache + timeframe aggregation + indicator endpoints** (backend,
  tests offline with fixture bars; aggregation math is algo-adjacent → lead reviews).
- **U2 — Terminal page** (vendored lightweight-charts, candles + timeframe switch + zoom +
  indicator toggles + volume pane; markers stubbed).
- **U3 — markers + Arms + Ledger + Hunt pages** (journal joins, tables with search/sort).
- **U4 — Calibration page + systemd unit + LAN deploy** (bind, service, smoke test from
  a second LAN host, docs: how to reach it, how to restart, how it stays read-only).

## Non-goals (v1)

- No public exposure, no auth (LAN trust boundary; revisit if ever exposed — then auth
  FIRST, not after).
- No order placement from the UI, ever — the executor is a separate process; the UI is a
  window, not a door.
- No websocket streaming in v1 (poll refresh 30-60s is honest for delayed/IEX data).
- No mobile layout promises (works or doesn't; mads's desktop is the target).
