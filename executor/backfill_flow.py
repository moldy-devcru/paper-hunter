"""Historical T6 flow backfill — reconstruct a past session's deep-OTM 0DTE volume.

Why this job exists
-------------------
``executor/soak.py`` accumulates one ``flow_baseline`` row per session, and T6 is
"deep-OTM 0DTE volume on the trade-direction side >= N x 20-day baseline" with ``N``
frozen before the first trade. At one row a day, the baseline is empty for three weeks
and ``N`` cannot be calibrated from it. This job fills those rows from history so the
calibration does not have to wait for the soak to mature.

Why Alpaca can do it (docs/research/2026-10-02-backfill-feasibility.md)
------------------------------------------------------------------------
``GET /v1beta1/options/bars`` serves **per-contract historical daily bars** on the free
(Basic) tier, with options data since **February 2024**, from the same indicative feed
the live soak samples. A 1-day bar's ``v`` is the same quantity the live soak reads out
of a chain snapshot's ``dailyBar.v``.

The catch, and it is the reason this file is careful: the **chain snapshot endpoint has
no point-in-time parameter**, so Alpaca cannot tell us which contracts were listed on a
past date — an expired contract is simply not in the chain. The backfill therefore
*enumerates* the candidate 0DTE ladder itself (a strike grid around the session's own
SPY close) and treats "no bar for that symbol" as no volume, which is the honest
reading: a contract with no daily bar did not trade that day. Every run reports the
coverage it achieved, because a ladder that missed half its strikes understates the
deep-OTM buckets, and those buckets are the input to a frozen threshold.

What this job does NOT do
-------------------------
* **No fabricated zeros.** A session whose whole 0DTE ladder comes back with zero volume
  is reported as ``no_data`` and **no row is written** — the live soak already refuses to
  write a 0.0-volume session for the same reason (a fake zero in a distribution ``N`` is
  calibrated from), and the backfill inherits that rule rather than inventing a
  nuance of its own.
* **No IV backfill.** Historical point-in-time Greeks/IV do not exist on any free tier,
  and a past 0DTE contract's IV is not reconstructible from a daily bar. T5's IV-rank
  history still has to accumulate live. This job touches ``flow_baseline`` only.
* **No forked math.** Aggregation and row assembly call ``soak.aggregate_flow`` and
  ``soak.build_flow_row`` — the same functions the live path calls. A second definition
  of "deep-OTM" would be a second number for the same word.
* **No updates.** ``flow_baseline`` is append-only. A backfilled session that turns out
  to be wrong is fixed by re-collecting into a fresh journal, not by rewriting history.

Provenance
----------
Every row it writes carries ``origin='backfill'`` (live soak rows are ``'live'``), the
same ``feed='indicative'`` / ``is_delayed=True`` provenance, the same strategy version,
and the same rulebook-derived baseline lookback. The T6 calibration may weight the two
differently, but it must be able to *tell* them apart, which is what the column is for.

Operating
---------
* ``--from``/``--to`` default to the last 90 calendar days. Only weekdays are attempted
  and a date with no SPY daily bar is a no-op (holiday), exactly as in the live soak.
* **Resumable.** Each session is attempted independently: one failed day (a 4xx, a
  timeout, a malformed payload) is recorded with its reason and the run continues, so a
  transient failure on day 40 of 90 does not throw away days 1-39. Re-running the same
  range skips the sessions already recorded — ``date`` is UNIQUE, and the run *reports*
  the skip instead of swallowing it.
* **Rate-limit aware.** Reads go through the client's ``RateLimiter`` (``--max-calls-
  per-minute``, default 180 against the documented 200/min Basic cap), and the run
  reports its own call count.
* Exit 0 when every attempted session ended in a recorded / skipped / no-data outcome;
  exit 1 if any session FAILED, so a scheduled run is visibly broken rather than
  quietly short.
* ``--dry-run`` runs the whole pipeline against ``tests/fixtures/backfill_day.json``
  and makes no network call at all (the tests assert it with an exploding transport).

Run commands (as ``paper-hunter`` with ``soak.env`` sourced, against /opt/paper-hunter)
-----------------------------------------------------------------------------
Smoke test, one day, nothing written -- run this FIRST, on a free box, to confirm the
two documented-but-unverified claims in the feasibility note (expired 0DTE contract bars
are served on Basic at all; the ladder enumeration is not 422'd as invalid symbols)::

    python -m executor.backfill_flow --dry-run
    python -m executor.backfill_flow --from 2026-09-25 --to 2026-09-25 --dry-run \\
        --db /tmp/flow-smoke.db

The real thing, after the smoke test looks right -- writes rows with origin='backfill'::

    python -m executor.backfill_flow --from 2026-07-01 --to 2026-09-30 \\
        --db /opt/paper-hunter/data/journal.db --report /opt/paper-hunter/data/backfill-report.json

Same window again is a no-op per session (idempotent), so it is safe to re-run after a
partial failure. Keep the ``--report`` file: it is the per-session coverage record.

Python 3.12+, stdlib only (consistent with the rest of ``executor/``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from config.loader import DEFAULT_RULES_PATH, Rulebook, load_rules
from executor.alpaca_client import (
    AlpacaClient,
    Bar,
    BarSeries,
    Greeks,
    OptionBarSeries,
    OptionContract,
    OptionQuote,
    OptionTrade,
    RateLimiter,
    _option_bars_from_payload,
    chunk_option_symbols,
    parse_occ_symbol,
)
from executor.position_manager import to_et
from executor.soak import (
    BAR_FEED,
    CHAIN_FEED,
    CHAIN_FEED_IS_DELAYED,
    DEFAULT_DEEP_OTM_PCT,
    aggregate_flow,
    build_flow_row,
    is_weekend,
)
from journal.store import (
    DuplicateFlowBaseline,
    FlowBaselineRow,
    append_flow_baseline,
    init_db,
    list_flow_baseline,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL_DB = REPO_ROOT / "data" / "journal.db"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
BACKFILL_FIXTURE = "backfill_day.json"

#: Default window: the last 90 calendar days. Roughly 63 sessions, ~3 option-bar calls
#: each at the default ladder — a few hundred calls against a 200/min budget.
DEFAULT_LOOKBACK_DAYS = 90

#: Default strike grid. See ``zero_dte_symbols`` for why this is a *parameter* and what a
#: wrong value costs.
DEFAULT_STRIKE_STEP = 1.0

#: Default moneyness span of the reconstructed ladder, percent from the session close.
#: 10% is wider than any plausible deep-OTM threshold (the working one is 3pp) so the
#: stored histogram can re-derive any integer threshold up to 10pp exactly, and it is
#: cheap: ~3 API calls per session.
DEFAULT_RANGE_PCT = 10.0

#: Calls/min the job aims for. The documented Basic cap is 200; the headroom is for the
#: soak/trigger jobs sharing the same key on the same box.
DEFAULT_MAX_CALLS_PER_MINUTE = 180

#: Equities need a SIP ``end`` at least 15 min old on the free tier; 20 min is the same
#: backoff the live soak uses.
EQUITY_END_BACKOFF_MINUTES = 20

#: Marks a row this job wrote. The literal lives here and in schema.sql's CHECK.
BACKFILL_ORIGIN = "backfill"


class BackfillError(RuntimeError):
    """Anything that stops the run, with a message meant for a human."""


# ---------------------------------------------------------------------------
# data source seam (dry-run vs live)
# ---------------------------------------------------------------------------


class HistoricalFlowSource(Protocol):
    """What the backfill needs from the outside world.

    Two implementations: :class:`_AlpacaSource` (live) and :class:`_FixtureSource`
    (``--dry-run``). The fixture one holds canned payloads and performs no I/O.
    """

    def equity_daily_bars(self, symbol: str, *, start: str, end: str) -> BarSeries: ...

    def option_daily_bars(
        self, symbols: Sequence[str], *, start: str, end: str
    ) -> OptionBarSeries: ...


class _AlpacaSource:
    """Live reads through the read-only client."""

    def __init__(self, client: AlpacaClient) -> None:
        self.client = client
        self.calls = 0

    def equity_daily_bars(self, symbol: str, *, start: str, end: str) -> BarSeries:
        self.calls += 1
        return self.client.get_daily_bars(
            symbol,
            feed=BAR_FEED,  # type: ignore[arg-type]
            start=start,
            end=end,
            # 10000 is the documented max; a 90-day window is ~63 bars, so one page.
            limit=10000,
        )

    def option_daily_bars(
        self, symbols: Sequence[str], *, start: str, end: str
    ) -> OptionBarSeries:
        self.calls += 1
        return self.client.get_option_daily_bars(
            symbols, start=start, end=end, timeframe="1Day"
        )


class _FixtureSource:
    """Offline source over one canned session. No I/O of any kind."""

    def __init__(self, equities: BarSeries, options: OptionBarSeries) -> None:
        self._equities = equities
        self._options = options
        self.calls = 0

    def equity_daily_bars(self, symbol: str, *, start: str, end: str) -> BarSeries:
        self.calls += 1
        return self._equities

    def option_daily_bars(
        self, symbols: Sequence[str], *, start: str, end: str
    ) -> OptionBarSeries:
        self.calls += 1
        return self._options


# ---------------------------------------------------------------------------
# session maths
# ---------------------------------------------------------------------------


def session_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Weekday dates in ``[start, end]``, oldest first.

    Weekends are dropped without a network call (same rule as the live soak), but
    *holidays* are not guessed at: a weekday with no SPY daily bar in the range is
    discovered from the bar series, which is the same positive evidence the soak uses.
    """
    if end < start:
        raise BackfillError(f"--to {end.isoformat()} is before --from {start.isoformat()}")
    days = []
    day = start
    while day <= end:
        if not is_weekend(day):
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def close_by_date(bars: BarSeries) -> dict[dt.date, float]:
    """ET session date -> that session's close, for the sessions in the series.

    Last write wins for a duplicated date, so a feed that somehow repeats a stamp yields
    the later bar rather than an arbitrary one.
    """
    closes: dict[dt.date, float] = {}
    for bar in bars.bars:
        closes[to_et(bar.t).date()] = bar.c
    return closes


def zero_dte_symbols(
    underlying: str,
    expiry: str,
    spot: float,
    *,
    strike_step: float = DEFAULT_STRIKE_STEP,
    range_pct: float = DEFAULT_RANGE_PCT,
) -> list[str]:
    """Candidate OCC contract symbols for a 0DTE expiry, calls then puts.

    The strike grid is derived from the session's own SPY close, so the ladder is centred
    where the money actually was, and each symbol is built to OCC layout
    (``<root><YYMMDD><C|P><strike x 1000, 8 digits>``) — the same layout
    ``alpaca_client.parse_occ_symbol`` reads back, which is how the reconstruction cannot
    silently mislabel a contract.

    # INTERPRETATION: the grid step is a **parameter, not a fact about the world**. SPY
    lists $1-wide strikes for same-day expiries, but a session with finer ($0.50) or
    wider ($5) intervals would have listed contracts this grid never asks for, and
    missing contracts mean **understated** deep-OTM volume. The defaults are therefore
    the widest defensible reconstruction, every run prints the requested/returned counts
    per session, and ``--strike-step`` exists so an operator who knows a date used a
    finer ladder can narrow it instead of trusting a default. What the job will not do is
    pretend the ladder was complete: coverage is reported per session, every time.

    # INTERPRETATION: strikes are snapped to a multiple of ``strike_step`` by
    ``ceil(lo/step)*step`` and emitted while ``<= hi``, so the span is symmetric to within
    one step and contains no strike outside the window. Symbols are de-duplicated by the
    caller's chunker; both rights are always requested because a deep-OTM *put* print is
    exactly the thing a calls-only enumeration would miss.
    """
    if spot <= 0:
        raise BackfillError(f"spot must be > 0 to build a strike ladder, got {spot}")
    if strike_step <= 0:
        raise BackfillError(f"strike step must be > 0, got {strike_step}")
    if range_pct <= 0:
        raise BackfillError(f"range_pct must be > 0, got {range_pct}")
    root = underlying.upper()
    yymmdd = expiry[-6:]
    if len(yymmdd) != 6 or not yymmdd.isdigit():
        raise BackfillError(f"expiry must be YYYYMMDD, got {expiry!r}")
    low = spot * (1.0 - range_pct / 100.0)
    high = spot * (1.0 + range_pct / 100.0)
    strike = math.ceil(low / strike_step) * strike_step
    symbols: list[str] = []
    while strike <= high + 1e-9:
        scaled = int(round(strike * 1000))
        if scaled > 0:
            symbols.append(f"{root}{yymmdd}C{scaled:08d}")
            symbols.append(f"{root}{yymmdd}P{scaled:08d}")
        strike += strike_step
    return symbols


# ---------------------------------------------------------------------------
# reconstruction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LadderCoverage:
    """What the enumeration asked for and what the API actually answered, per session.

    Carried on the run report rather than on the row: ``flow_baseline`` has no column
    for it, and inventing one for a single job's bookkeeping would change the shape of
    the table the calibration reads. The numbers live in the ``--report`` file, which is
    the honest home for "how sure are we about this row".
    """

    requested: int
    with_bar: int
    without_bar: int
    off_session_bars: int

    def as_dict(self) -> dict[str, int]:
        return {
            "symbols_requested": self.requested,
            "symbols_with_bar": self.with_bar,
            "symbols_without_bar": self.without_bar,
            "off_session_bars": self.off_session_bars,
        }


def contracts_from_bars(
    requested: Sequence[str],
    series: OptionBarSeries,
    session: dt.date,
) -> tuple[list[OptionContract], int]:
    """Rebuild ``OptionContract``s for the contracts that traded on ``session``.

    A symbol contributes a contract only if it has a bar **stamped on that session in ET
    terms**. Off-session bars are counted and discarded rather than summed: a bar from a
    neighbouring day inside the window would otherwise be credited to this session's
    deep-OTM volume, which is a wrong number rather than a missing one.

    # INTERPRETATION: the contract is built by *parsing the OCC symbol back* rather than
    by remembering what we asked for, so strike/right/expiry in the row come from the
    same parser the live path uses. A contract whose symbol does not parse is skipped and
    counted as off-session — it cannot be placed in a distance bucket with confidence, and
    a loud gap beats a wrong bucket.
    """
    contracts: list[OptionContract] = []
    off_session = 0
    for symbol in requested:
        bars = series.bars_by_symbol.get(symbol.upper(), [])
        match = next((b for b in bars if to_et(b.t).date() == session), None)
        if match is None:
            off_session += len(bars)
            continue
        try:
            underlying, expiry, strike, right = parse_occ_symbol(symbol)
        except Exception:  # noqa: BLE001 - an unparseable symbol is data we refuse, not a crash
            off_session += 1
            continue
        contracts.append(
            OptionContract(
                symbol=symbol,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                right=right,
                # The historical bars route carries no IV and no greeks. The live chain
                # snapshot does. That difference is exactly why this job never writes to
                # the IV store, and why these contracts are only ever fed to the flow
                # aggregation.
                implied_volatility=None,
                greeks=Greeks(),
                latest_quote=OptionQuote(),
                latest_trade=OptionTrade(),
                daily_bar=match,
            )
        )
    return contracts, off_session


def session_flow(
    source: HistoricalFlowSource,
    *,
    session: dt.date,
    symbol: str,
    spot: float,
    threshold_pct: float,
    strike_step: float,
    range_pct: float,
) -> tuple[list[OptionContract], LadderCoverage]:
    """Fetch one session's 0DTE ladder and return the contracts that have volume.

    The option-bar window is ``start=end=<session>``, so the API returns at most one
    daily bar per contract and a paging problem cannot silently merge two sessions.
    """
    expiry = session.strftime("%Y%m%d")
    requested = zero_dte_symbols(
        symbol, expiry, spot, strike_step=strike_step, range_pct=range_pct
    )
    contracts: list[OptionContract] = []
    with_bar = 0
    off_session = 0
    for chunk in chunk_option_symbols(requested):
        series = source.option_daily_bars(chunk, start=session.isoformat(), end=session.isoformat())
        found, dropped = contracts_from_bars(chunk, series, session)
        contracts.extend(found)
        with_bar += len(found)
        off_session += dropped
    return contracts, LadderCoverage(
        requested=len(requested),
        with_bar=with_bar,
        without_bar=len(requested) - with_bar,
        off_session_bars=off_session,
    )


# ---------------------------------------------------------------------------
# per-session outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayResult:
    """One session's outcome. ``status`` is the machine-readable half."""

    session: str
    status: str
    summary: str
    row: FlowBaselineRow | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


#: Statuses that mean "the run did what it was asked". Anything in ``FAILED_STATUSES``
#: is a broken run.
FAILED_STATUSES = frozenset({"failed"})


@dataclass(frozen=True, slots=True)
class BackfillRun:
    """Outcome of a whole range."""

    days: list[DayResult]
    start: str
    end: str
    api_calls: int
    summary: str

    @property
    def recorded(self) -> list[DayResult]:
        return [d for d in self.days if d.status in {"recorded", "dry_run"}]

    @property
    def failures(self) -> list[DayResult]:
        return [d for d in self.days if d.status in FAILED_STATUSES]

    def report(self) -> dict[str, Any]:
        """Machine-readable per-session record — the honesty artifact for a run."""
        counts: dict[str, int] = {}
        for day in self.days:
            counts[day.status] = counts.get(day.status, 0) + 1
        return {
            "window": {"from": self.start, "end": self.end},
            "api_calls": self.api_calls,
            "counts": counts,
            "summary": self.summary,
            "sessions": [d.as_dict() for d in self.days],
        }


def backfill_flow(
    *,
    source: HistoricalFlowSource,
    conn: Any,
    rules: Rulebook,
    start: dt.date,
    end: dt.date,
    symbol: str | None = None,
    threshold_pct: float = DEFAULT_DEEP_OTM_PCT,
    strike_step: float = DEFAULT_STRIKE_STEP,
    range_pct: float = DEFAULT_RANGE_PCT,
    dry_run: bool = False,
) -> BackfillRun:
    """Reconstruct ``flow_baseline`` rows for every session in ``[start, end]``.

    Sessions are processed **oldest first**, and each row's trailing baseline is built
    from rows already in the database dated strictly before it — the same
    ``list_flow_baseline(before=...)`` contract the live soak uses. Oldest-first is what
    makes an inserted row's ``baseline_days`` meaningful: backfilling July after August
    would otherwise leave August's rows describing a baseline that July's sessions were
    not part of. If the operator backfills a *later* range into a database that already
    has earlier rows, the existing rows keep the baseline they were written with (the
    table is append-only) — so the calibration should read the histograms, which are
    threshold-re-derivable and order-independent, rather than the stored ratios.
    """
    ticker = (symbol or rules.strategy.symbol).upper()
    lookback_days = rules.checklist.t6_flow.baseline_lookback_days
    results: list[DayResult] = []

    # One equities call for the whole window, not one per session: the bar for a date IS
    # the evidence that a session happened, and asking once per day would spend ~63 calls
    # re-reading the same series.
    window_end = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=EQUITY_END_BACKOFF_MINUTES))
    equities = source.equity_daily_bars(
        ticker,
        start=start.isoformat(),
        end=window_end.isoformat().replace("+00:00", "Z"),
    )
    closes = close_by_date(equities)

    for session in session_days(start, end):
        try:
            results.append(
                _backfill_one(
                    source=source,
                    conn=conn,
                    session=session,
                    ticker=ticker,
                    closes=closes,
                    lookback_days=lookback_days,
                    threshold_pct=threshold_pct,
                    strike_step=strike_step,
                    range_pct=range_pct,
                    strategy_version=rules.strategy_version,
                    dry_run=dry_run,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad day must not kill 60 good ones
            # # INTERPRETATION: the message keeps the exception type and text (Alpaca's
            # HTTP status and body are the whole diagnosis — see the feasibility note's
            # §5), and the run continues. A systematic failure repeats on every day and
            # shows up as every day failing, which is a louder signal than one early
            # abort.
            results.append(
                DayResult(
                    session=session.isoformat(),
                    status="failed",
                    summary=(
                        f"backfill: {session.isoformat()} FAILED — "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
            )

    calls = int(getattr(source, "calls", 0))
    summary = _run_summary(results, start, end, calls, lookback_days, dry_run)
    return BackfillRun(
        days=results, start=start.isoformat(), end=end.isoformat(), api_calls=calls, summary=summary
    )


def _backfill_one(
    *,
    source: HistoricalFlowSource,
    conn: Any,
    session: dt.date,
    ticker: str,
    closes: dict[dt.date, float],
    lookback_days: int,
    threshold_pct: float,
    strike_step: float,
    range_pct: float,
    strategy_version: str,
    dry_run: bool,
) -> DayResult:
    """One session. Never raises for an expected condition; the caller catches the rest."""
    existing = conn.execute(
        "SELECT id FROM flow_baseline WHERE date = ?", (session.isoformat(),)
    ).fetchone()
    if existing is not None:
        return DayResult(
            session=session.isoformat(),
            status="already_recorded",
            summary=(
                f"backfill: {session.isoformat()} already has a flow_baseline row "
                f"(id={existing['id']}) — skipped, nothing duplicated"
            ),
            details={"row_id": existing["id"]},
        )

    spot = closes.get(session)
    if spot is None or spot <= 0:
        return DayResult(
            session=session.isoformat(),
            status="no_session",
            summary=(
                f"backfill: {session.isoformat()} has no SPY daily bar in the window — "
                f"holiday or no session, nothing written"
            ),
        )

    contracts, coverage = session_flow(
        source,
        session=session,
        symbol=ticker,
        spot=spot,
        threshold_pct=threshold_pct,
        strike_step=strike_step,
        range_pct=range_pct,
    )
    raw_total = sum(float(c.daily_bar.v) for c in contracts if c.daily_bar is not None)
    if not contracts or raw_total <= 0:
        # # INTERPRETATION: the honest-empty rule, inherited from the live soak. Zero
        # volume across the whole 0DTE ladder is indistinguishable from "no 0DTE expiry
        # was listed that day" (we cannot ask), and both are reasons to write NO row: a
        # 0.0-volume session in the baseline is a fabricated observation, and N is
        # calibrated from these numbers.
        return DayResult(
            session=session.isoformat(),
            status="no_data",
            summary=(
                f"backfill: {session.isoformat()} 0DTE ladder returned "
                f"{coverage.with_bar}/{coverage.requested} contract(s) with bars and "
                f"zero total volume — no row written (honest empty, not a 0.0 session)"
            ),
            details=coverage.as_dict() | {"spot": spot, "raw_total_volume": raw_total},
        )

    agg = aggregate_flow(contracts, spot, threshold_pct=threshold_pct)
    prior = list_flow_baseline(conn, before=session.isoformat())
    row = build_flow_row(
        session=session,
        underlying=ticker,
        feed=CHAIN_FEED,
        is_delayed=CHAIN_FEED_IS_DELAYED,
        spot=spot,
        zero_dte_expiry=session.strftime("%Y%m%d"),
        chain_contracts=len(contracts),
        agg=agg,
        baseline_rows=prior,
        lookback_days=lookback_days,
        strategy_version=strategy_version,
    )
    # Same row the live soak would write, plus the one field that says it was
    # reconstructed rather than soaked. model_copy is a rebuild, not an edit: the row is
    # inserted once and never updated.
    row = row.model_copy(update={"origin": BACKFILL_ORIGIN})
    details = agg.to_dict() | coverage.as_dict() | {"session_spot": spot}

    if dry_run:
        return DayResult(
            session=session.isoformat(),
            status="dry_run",
            summary=(
                f"backfill: dry-run {session.isoformat()} — reconstructed "
                f"deep-OTM>={agg.threshold_pct:g}% call={agg.call_volume:.0f} "
                f"put={agg.put_volume:.0f} from {coverage.with_bar}/{coverage.requested} "
                f"contract(s); baseline {row.baseline_days}/{lookback_days}d; nothing written"
            ),
            row=row,
            details=details,
        )

    try:
        row_id = append_flow_baseline(conn, row)
    except DuplicateFlowBaseline:
        return DayResult(
            session=session.isoformat(),
            status="already_recorded",
            summary=(
                f"backfill: {session.isoformat()} already has a flow_baseline row — "
                f"skipped, nothing duplicated"
            ),
            details=details,
        )
    return DayResult(
        session=session.isoformat(),
        status="recorded",
        summary=(
            f"backfill: {session.isoformat()} recorded (row {row_id}, origin=backfill) — "
            f"deep-OTM>={agg.threshold_pct:g}% call={agg.call_volume:.0f} "
            f"put={agg.put_volume:.0f} from {coverage.with_bar}/{coverage.requested} "
            f"contract(s) with bars"
        ),
        row=row,
        details=details | {"row_id": row_id},
    )


def _run_summary(
    results: Sequence[DayResult],
    start: dt.date,
    end: dt.date,
    calls: int,
    lookback_days: int,
    dry_run: bool,
) -> str:
    """One line, always, on success and on partial failure alike."""
    counts: dict[str, int] = {}
    for day in results:
        counts[day.status] = counts.get(day.status, 0) + 1
    tally = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "no sessions"
    wrote = "nothing written (dry-run)" if dry_run else "rows carry origin=backfill"
    return (
        f"backfill: {start.isoformat()}..{end.isoformat()} — {tally}; {calls} API call(s); "
        f"baseline lookback {lookback_days}d; {wrote}"
    )


# ---------------------------------------------------------------------------
# fixtures (dry-run)
# ---------------------------------------------------------------------------


def load_fixture_source(
    fixture_dir: Path = FIXTURE_DIR, symbol: str = "SPY"
) -> tuple[_FixtureSource, dt.date]:
    """Build an offline source from ``tests/fixtures/backfill_day.json``.

    The fixture carries its own session date, so ``--dry-run`` with no ``--from/--to``
    replays exactly the session the fixture describes. The equity block is the documented
    single-symbol bars envelope and the option block the documented historical
    option-bars envelope (``{"bars": {occ: [...]}}``), so a dry run exercises the real
    parsers.
    """
    payload = json.loads((fixture_dir / BACKFILL_FIXTURE).read_text(encoding="utf-8"))
    session = dt.date.fromisoformat(str(payload["session"]))
    equity_payload = dict(payload["stock_bars"])
    equity_payload.setdefault("symbol", symbol)
    equity_payload.setdefault("next_page_token", None)
    bars_block = equity_payload.get("bars") or []
    if isinstance(bars_block, dict):
        bars_block = bars_block.get(symbol) or next(iter(bars_block.values()), [])
    bars = sorted((Bar.from_json(b) for b in bars_block), key=lambda b: b.t)
    equities = BarSeries(symbol=symbol, timeframe="1Day", feed=BAR_FEED, bars=bars)
    return _FixtureSource(equities, _option_bars_from_payload(payload["option_bars"])), session


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_flow",
        description="Reconstruct historical T6 flow_baseline rows from per-contract bars",
    )
    parser.add_argument("--rules", default=str(DEFAULT_RULES_PATH), help="frozen rulebook path")
    parser.add_argument("--db", default=str(DEFAULT_JOURNAL_DB), help="journal SQLite path")
    # None, not a pre-filled default: the dry-run branch needs to know whether the
    # operator named a window at all, so it can replay the fixture's own session instead
    # of the last 90 days (which the fixture does not cover).
    parser.add_argument(
        "--from",
        dest="from_date",
        default=None,
        help=f"first session date YYYY-MM-DD (default: today-{DEFAULT_LOOKBACK_DAYS}d)",
    )
    parser.add_argument(
        "--to", dest="to_date", default=None, help="last session date YYYY-MM-DD (default: today)"
    )
    parser.add_argument("--symbol", default=None, help="override the rulebook symbol")
    parser.add_argument(
        "--deep-otm-pct",
        type=float,
        default=DEFAULT_DEEP_OTM_PCT,
        help=(
            f"deep-OTM distance threshold, percent from spot (default {DEFAULT_DEEP_OTM_PCT:g}; "
            "same default and same integer-flooring rule as the live soak)"
        ),
    )
    parser.add_argument(
        "--strike-step",
        type=float,
        default=DEFAULT_STRIKE_STEP,
        help=(
            f"strike grid step for the reconstructed ladder (default {DEFAULT_STRIKE_STEP:g}). "
            "A session with a finer ladder is UNDERCOUNTED unless this is narrowed; the "
            "per-session requested/returned counts are always reported"
        ),
    )
    parser.add_argument(
        "--range-pct",
        type=float,
        default=DEFAULT_RANGE_PCT,
        help=(
            f"ladder span in percent from the session close (default {DEFAULT_RANGE_PCT:g}); "
            "the histogram re-derives any integer threshold up to this exactly"
        ),
    )
    parser.add_argument(
        "--max-calls-per-minute",
        type=int,
        default=DEFAULT_MAX_CALLS_PER_MINUTE,
        help=(
            f"pace of historical API calls (default {DEFAULT_MAX_CALLS_PER_MINUTE}; "
            "the documented Basic cap is 200/min)"
        ),
    )
    parser.add_argument(
        "--report", default=None, help="write the per-session JSON report to this path"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the whole pipeline against tests/fixtures; no network, no writes",
    )
    parser.add_argument("--json", action="store_true", help="print the run as JSON instead")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rules = load_rules(args.rules)
        if args.max_calls_per_minute < 1:
            raise BackfillError("--max-calls-per-minute must be >= 1")

        if args.dry_run:
            source, fixture_session = load_fixture_source(
                symbol=(args.symbol or rules.strategy.symbol)
            )
            # No window named -> replay the fixture's own session. A window named is
            # honoured as written, and dates the fixture does not cover come back
            # "no session" — which is itself worth seeing.
            window_given = args.from_date is not None or args.to_date is not None
            default_from, default_to = _default_window()
            if window_given:
                from_date = (
                    dt.date.fromisoformat(args.from_date) if args.from_date else default_from
                )
                to_date = dt.date.fromisoformat(args.to_date) if args.to_date else default_to
            else:
                from_date = to_date = fixture_session
            conn = init_db(":memory:")
        else:
            client = AlpacaClient.from_env(
                rate_limiter=RateLimiter(max_calls=args.max_calls_per_minute)
            )
            source = _AlpacaSource(client)
            default_from, default_to = _default_window()
            from_date = (
                dt.date.fromisoformat(args.from_date) if args.from_date else default_from
            )
            to_date = dt.date.fromisoformat(args.to_date) if args.to_date else default_to
            conn = init_db(args.db)

        run = backfill_flow(
            source=source,
            conn=conn,
            rules=rules,
            start=from_date,
            end=to_date,
            symbol=args.symbol,
            threshold_pct=args.deep_otm_pct,
            strike_step=args.strike_step,
            range_pct=args.range_pct,
            dry_run=args.dry_run,
        )

        for day in run.days:
            if not args.json:  # --json is for machines; the prose would break json.loads
                print(day.summary)
        if args.report:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(
                json.dumps(run.report(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        if args.json:
            print(json.dumps(run.report(), indent=2, sort_keys=True))
        else:
            print(run.summary)
        # A session that failed is a broken run: exit non-zero so a scheduled backfill
        # is visibly incomplete rather than quietly short of its window.
        return 1 if run.failures else 0
    except Exception as exc:  # noqa: BLE001 - an operator-facing job prints the failure
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _default_window() -> tuple[dt.date, dt.date]:
    """The window argparse falls back to when ``--from``/``--to`` are omitted."""
    today = dt.datetime.now(dt.UTC).date()
    return today - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS), today


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BACKFILL_ORIGIN",
    "DEFAULT_MAX_CALLS_PER_MINUTE",
    "DEFAULT_RANGE_PCT",
    "DEFAULT_STRIKE_STEP",
    "BackfillError",
    "BackfillRun",
    "DayResult",
    "HistoricalFlowSource",
    "LadderCoverage",
    "backfill_flow",
    "build_parser",
    "close_by_date",
    "contracts_from_bars",
    "load_fixture_source",
    "main",
    "session_days",
    "session_flow",
    "zero_dte_symbols",
]
