"""Daily EOD soak job — accumulate T6 flow inputs and IV-rank history, before the window.

Why this job exists
-------------------
Two of the seven frozen conditions cannot be evaluated on day one of the trading
window, and both need history that has to be accumulated *one session at a time*:

* **T5 / IV rank.** No free source has point-in-time historical IV (research note
  ``2026-10-02-options-data-sources.md`` §1: Alpaca serves IV only as a current
  snapshot field). ``executor/iv_rank.py`` is the accumulator; this job is its pump.
* **T6 / deep-OTM 0DTE flow.** ">= N x 20-day baseline" needs 20 prior sessions of
  SPY 0DTE volume before ``N`` can even be calibrated (research note
  ``2026-10-02-flow-data-market.md`` §4: real-time programmatic OPRA is a $2,000/mo
  non-display fee; >15-min-delayed is $0, and T6 is ``evaluation: EOD_only`` so
  delayed is the *correct* tier, not a compromise).

Every day this job runs and every day it does **not** trade, a session of history
is one session closer to a defined T5 and a calibrated T6. That is the whole point,
and it is why the job is scheduled (scripts/soak.timer) rather than run by hand.

What one run does
-----------------
1. Resolve the session date (ET) and refuse weekends without touching the network.
2. Pull SPY daily bars. If the newest bar is not today's, there was no session —
   a holiday, or a feed that has not landed the bar yet. Either way: no-op, exit 0.
3. Pull the options chain, keep the 0DTE expiry, aggregate deep-OTM volume per side.
4. Record one ATM IV observation per live expiry into the IV store (Phase-3a
   ``tenor_key`` bucketing, plus a rolling DTE-bucketed key — see ``iv_rank``).
5. Append one immutable ``flow_baseline`` row for the session, with provenance and
   the trailing-baseline means and ratios T6 reads.

Nothing here places orders, reads the paper account, or writes anything but the two
data stores. The whole job is ``alpaca_client`` (read-only) + the two stores.

Exit codes / output (systemd contract)
--------------------------------------
* exit 0 + a single summary line on stdout for every success **and every no-op**
  (weekend, no session, already recorded). A skipped day is a successful day.
* non-zero + a message on stderr for any failure. No tracebacks at the operator:
  ``main`` catches, prints ``error: <Type>: <msg>``, returns non-zero.

``--dry-run`` does the whole pipeline against the offline fixtures in
``tests/fixtures`` and makes no network call at all — the transport it is handed
raises if touched, and the test asserts exactly that.

Python 3.12+, stdlib only.
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
    OptionChain,
    OptionContract,
    _chain_from_payload,
)
from executor.bar_windows import bar_window
from executor.iv_rank import DEFAULT_DB_PATH as DEFAULT_IV_DB_PATH
from executor.iv_rank import (
    DTE_BUCKET_DAYS,
    MONEYNESS_BUCKET_PCT,
    STRIKE_BUCKET_SIZE,
    IvObservation,
    IvRankStore,
    moneyness_bucket,
    moneyness_tenor_key,
    tenor_key,
)
from executor.position_manager import to_et
from journal.store import (
    DuplicateFlowBaseline,
    FlowBaselineRow,
    append_flow_baseline,
    init_db,
    list_flow_baseline,
    utcnow,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL_DB = REPO_ROOT / "data" / "journal.db"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"

#: Option feed the soak reads. Alpaca's free tier has exactly one options feed with
#: IV + volume in the snapshot: the Indicative Pricing Feed. Recorded on every row
#: (schema: ``feed``, ``is_delayed``) because its trades are ~15 min delayed and
#: synthetic-derived from OPRA — a flow number without that attached is not auditable.
CHAIN_FEED = "indicative"

#: The Indicative Pricing Feed delays option trades; the row says so explicitly.
CHAIN_FEED_IS_DELAYED = True

#: Equity bar feed for the daily series. SIP (consolidated) is used because the same
#: series also backs T4 relative volume in the executor, and snapshot_builder refuses
#: to compute relvol across feeds — the soak's bar history and the executor's should
#: be one universe, not two.
BAR_FEED = "sip"

#: Working aggregation threshold for the deep-OTM buckets, in percent distance from
#: spot. NOT a frozen rule — see the rulebook's ``checklist.t6_flow.deep_otm``, where
#: the *frozen* threshold is still ``calibration_pending``. This value only chooses
#: which pre-aggregated column gets filled; the per-distance histogram below is the
#: threshold-free data, and it re-derives any integer threshold exactly.
DEFAULT_DEEP_OTM_PCT = 3.0

#: Distance buckets are 1 percentage point wide, keyed by their lower bound. For an
#: integer threshold t, ``sum(bucket for bucket >= t)`` is exactly the volume of
#: contracts at >= t percent from spot — no approximation — which is why thresholds
#: are documented as integer percentage points.
DISTANCE_BUCKET_PCT = 1.0

#: Expiries further out than this are not IV-polled. This is a BOUND ON THE SWEEP, not
#: a statement about which tenors matter: ``IV_MAX_DTE`` was 45 while arm C's frozen entry
#: band is 90-180 DTE (``arms.C.entry.dte``), so the sweep structurally could not observe
#: the tenor arm C's own gate reads. That number was a fossil of the pre-``abb3d1a`` world
#: — the comment here used to justify polling a "shorter-dated ATM proxy" for a
#: quarter-year contract, which is precisely the mismatch ``abb3d1a`` removed from the
#: gate side. The gate now reads the arm's own band, so the recorder must poll that band.
#:
#: 180 covers arm C's whole entry window. Rows stay bounded because one observation is
#: written per (expiry, arm-matched contract) rather than per contract, and the sweep
#: skips expiries outside it.
IV_MAX_DTE = 180

#: Per-arm IV bands, read from the rulebook at run time (see ``_bands_from_rules``) and
#: defaulted here. Arm B trades DTE 0; arm C trades the 90-180 DTE deep-ITM band. Sweeping
#: both is what lets one recorder serve both gates: each arm's row is written under the
#: same key its gate reads, which is the only reason a rank can ever resolve.
ARM_IV_BANDS: tuple[tuple[str, int, int], ...] = (
    ("B", 0, 0),
    ("C", 90, 180),
)

#: Bars pulled per run. Enough for the bar-date check with slack; this job does not
#: compute indicators (the executor does that from its own pull).
BARS_LIMIT = 10


class SoakError(RuntimeError):
    """Anything that stops the run, with a message meant for a human."""


# ---------------------------------------------------------------------------
# data source seam (dry-run vs live)
# ---------------------------------------------------------------------------


class SoakData(Protocol):
    """What the soak needs from the outside world.

    Two implementations: :class:`_AlpacaSource` (live) and :class:`_FixtureSource`
    (``--dry-run``). The fixture source holds a transport that raises on any call, so
    "dry-run makes no network call" is enforced by construction and asserted by test
    rather than promised in a docstring.
    """

    def daily_bars(self, symbol: str) -> BarSeries: ...

    def option_chain(self, symbol: str) -> OptionChain: ...


class _AlpacaSource:
    """Live reads through the read-only client."""

    def __init__(self, client: AlpacaClient, *, bars_limit: int = BARS_LIMIT) -> None:
        self.client = client
        self.bars_limit = bars_limit

    def daily_bars(self, symbol: str) -> BarSeries:
        # FIX 2026-10-03 (live seam sweep): this read named an `end` and a `limit` and NO
        # `start`, which the live route answers with **zero bars** and HTTP 200 — measured
        # today, paper creds, `1Day`/sip: `end+limit` -> n=0, `bar_window(limit)` -> n=400.
        # An `end` alone is not a window; the API resolves a span, and `limit` is a cap on
        # the answer rather than something it can resolve one from.
        #
        # The failure was SILENT and worse than a crash: `run_soak` reads
        # `bars.bars[-1].c if bars.bars else 0.0` and raises `SoakError("no usable SPY
        # close")` on 0.0 — so the reported symptom was "no close for the session", which
        # reads like a data/calendar fact about the market rather than a query that never
        # asked for a span. Every soak run would have blamed the tape.
        #
        # The 20-minute SIP backoff this used to apply by hand is now part of the shared
        # policy (`executor.bar_windows.bar_window`), which also adds the `sort` that
        # keeps the newest bar inside the limit — the same two omissions as `cmd_eod`.
        return self.client.get_daily_bars(
            symbol,
            feed=BAR_FEED,  # type: ignore[arg-type]
            limit=self.bars_limit,
            **bar_window(timeframe="1Day", feed=BAR_FEED, limit=self.bars_limit),
        )

    def option_chain(self, symbol: str) -> OptionChain:
        return self.client.get_option_chain(symbol, feed=CHAIN_FEED)  # type: ignore[arg-type]


class _FixtureSource:
    """Offline source over the repo's canned payloads. Performs no I/O."""

    def __init__(self, bars: BarSeries, chain: OptionChain) -> None:
        self._bars = bars
        self._chain = chain

    def daily_bars(self, symbol: str) -> BarSeries:
        return self._bars

    def option_chain(self, symbol: str) -> OptionChain:
        return self._chain


# ---------------------------------------------------------------------------
# session resolution
# ---------------------------------------------------------------------------


def is_weekend(day: dt.date) -> bool:
    """Saturday or Sunday. Checked before any network call."""
    return day.weekday() >= 5


def newest_bar_day(bars: BarSeries) -> dt.date | None:
    """ET date of the newest bar in the series, or ``None`` if there are no bars."""
    if not bars.bars:
        return None
    return to_et(bars.bars[-1].t).date()


def resolve_session(
    session: dt.date | None, bars: BarSeries | None, *, force: bool = False
) -> tuple[dt.date | None, str]:
    """Decide which session this run is recording.

    Returns ``(date, reason)``. ``date is None`` means "no session, no-op" — weekend,
    holiday, or a feed that has not landed today's bar yet. All three are ordinary
    outcomes, not errors: the timer fires on weekdays including market holidays.

    # INTERPRETATION: "did a session happen?" is answered by **the presence of a
    # daily bar stamped with that date**, not by a weekday check and not by
    # ``/v2/clock``. The clock answers "is the market open *right now*", which at
    # 17:30 ET is closed on every trading day — useless for this question. A bar for
    # the date is the positive evidence that a session occurred, and its absence is
    # the positive evidence that one did not, so one data point settles both the
    # holiday case and the "bar not landed yet" case without a second source that
    # could disagree with the first.
    #
    # ``force`` exists for the one case where that evidence is genuinely wrong: a
    # late/partial bar pull where the session did happen. It is an explicit operator
    # override recorded in the run summary, never a default.
    """
    day = session or to_et(dt.datetime.now(dt.UTC)).date()
    if is_weekend(day):
        return None, f"{day.isoformat()} is a weekend — no session, nothing recorded"
    if force:
        return day, "forced: recording without bar evidence for this date"
    if bars is None:
        return day, "bar check skipped (no bar series supplied)"
    newest = newest_bar_day(bars)
    if newest is None:
        return None, "no daily bars returned — cannot confirm a session happened"
    if newest != day:
        return None, (
            f"no bar for {day.isoformat()} (newest is {newest.isoformat()}) — "
            f"holiday, or the bar has not landed; no session to record"
        )
    return day, f"bar for {day.isoformat()} confirms a session"


# ---------------------------------------------------------------------------
# deep-OTM aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlowAggregate:
    """Deep-OTM volume for one session, per side, plus the raw histogram."""

    call_volume: float
    put_volume: float
    total_volume: float
    call_by_distance: dict[str, float]
    put_by_distance: dict[str, float]
    threshold_pct: float
    contracts_considered: int
    contracts_without_volume: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_volume": self.call_volume,
            "put_volume": self.put_volume,
            "total_volume": self.total_volume,
            "call_by_distance": self.call_by_distance,
            "put_by_distance": self.put_by_distance,
            "threshold_pct": self.threshold_pct,
            "contracts_considered": self.contracts_considered,
            "contracts_without_volume": self.contracts_without_volume,
        }


def otm_distance_pct(strike: float, spot: float) -> float | None:
    """Percent distance of a strike from spot, or ``None`` if it is not OTM.

    OTM is side-specific: a call is OTM above spot, a put below. An ATM contract has
    no distance, which is why ``None`` is returned rather than ``0.0`` — an ATM print
    is not a zero-distance deep-OTM print, and folding it in would quietly inflate
    every bucket's floor.

    # INTERPRETATION: moneyness is measured against **spot** (the session's SPY
    close), not against a forward or an option-adjusted reference. The brief says
    "first OTM strike beyond the setup-day range projection" for *entry* strike
    selection, which is a different question answered intraday by the hunt plan; T6
    is a population statistic over the whole day's 0DTE chain, and a single stable
    reference is what makes one session's buckets comparable to the next one's. The
    buckets are stored with the spot they were computed against, so a reader can
    re-derive or discard them; there is no hidden normalisation.
    """
    if spot <= 0:
        raise SoakError(f"spot must be > 0 to measure moneyness, got {spot}")
    if strike == spot:
        return None
    return abs(strike - spot) / spot * 100.0


def bucket_key(distance_pct: float) -> str:
    """Histogram key for a distance: the 1pp bucket's integer lower bound.

    # INTERPRETATION: buckets are keyed by ``floor(distance)`` and are 1 percentage
    point wide, so ``sum(v for k, v in buckets.items() if int(k) >= t)`` is exactly
    the volume at >= t percent from spot **for any integer t**. That is the property
    that lets the threshold be revised later without re-fetching (and re-paying for)
    OPRA history — and it is why thresholds here are integer percentage points. A
    fractional threshold would split a bucket and could only be approximated, so if
    one is ever wanted, the honest move is to re-collect, not to interpolate.
    """
    return str(int(math.floor(distance_pct / DISTANCE_BUCKET_PCT)))


def aggregate_flow(
    contracts: Sequence[OptionContract],
    spot: float,
    *,
    threshold_pct: float = DEFAULT_DEEP_OTM_PCT,
) -> FlowAggregate:
    """Deep-OTM volume per side over one expiry's contracts.

    Volume source is the contract's ``dailyBar.v``. A contract with no daily bar
    (and no trade) contributes nothing and is counted in
    ``contracts_without_volume`` — reported, never silently dropped, because a feed
    that stops reporting volume looks identical to a market that stopped trading if
    the count is not carried.

    # INTERPRETATION: the aggregate uses the **unrounded** strike for the OTM test and
    the histogram, but the aggregate *threshold* is floored to an integer (see
    :func:`bucket_key`), so a threshold of 3.5% is honoured as 3%. The alternative —
    interpolating partial buckets — would invent volume that no contract printed. The
    applied threshold is returned in the result so the row records which one was used.
    """
    if threshold_pct <= 0:
        raise SoakError(f"deep-OTM threshold must be > 0, got {threshold_pct}")
    applied = float(int(math.floor(threshold_pct)))
    call_by: dict[str, float] = {}
    put_by: dict[str, float] = {}
    call_volume = 0.0
    put_volume = 0.0
    considered = 0
    missing = 0
    for contract in contracts:
        considered += 1
        bar = contract.daily_bar
        volume = float(bar.v) if bar is not None else 0.0
        if bar is None:
            missing += 1
        otm = contract.strike > spot if contract.right == "call" else contract.strike < spot
        if not otm:
            continue
        distance = otm_distance_pct(contract.strike, spot)
        if distance is None:
            continue
        key = bucket_key(distance)
        target = call_by if contract.right == "call" else put_by
        target[key] = target.get(key, 0.0) + volume
        if distance >= applied:
            if contract.right == "call":
                call_volume += volume
            else:
                put_volume += volume
    return FlowAggregate(
        call_volume=call_volume,
        put_volume=put_volume,
        total_volume=call_volume + put_volume,
        call_by_distance=dict(sorted(call_by.items(), key=lambda kv: int(kv[0]))),
        put_by_distance=dict(sorted(put_by.items(), key=lambda kv: int(kv[0]))),
        threshold_pct=applied,
        contracts_considered=considered,
        contracts_without_volume=missing,
    )


def threshold_from_buckets(buckets: dict[str, float], threshold_pct: float) -> float:
    """Volume at >= ``threshold_pct`` from a stored histogram.

    The exact re-derivation that makes a threshold change cheap; used by the
    calibration tooling and asserted against ``aggregate_flow`` in the tests.
    """
    applied = float(int(math.floor(threshold_pct)))
    return sum(v for k, v in buckets.items() if float(k) >= applied)


# ---------------------------------------------------------------------------
# IV snapshot
# ---------------------------------------------------------------------------


def _bands_from_rules(rules: Rulebook) -> tuple[tuple[str, int, int], ...]:
    """Per-arm ``(arm, dte_min, dte_max)`` read from the frozen rulebook.

    Arm B's ``entry.dte`` is a plain integer ``0``; arm C's is a ``DTERange``. Both
    normalize to the same triple so the recorder cannot drift from the bands the gates
    read — the drift being exactly the bug this fixes.
    """
    bands: list[tuple[str, int, int]] = []
    for arm in ("B", "C"):
        dte = getattr(rules.arms, arm).entry.dte
        if isinstance(dte, int):
            bands.append((arm, dte, dte))
        else:
            bands.append((arm, int(dte.min), int(dte.max)))
    return tuple(bands)


def arm_contract_for_band(
    chain: OptionChain,
    spot: float,
    session: dt.date,
    dte_min: int,
    dte_max: int,
) -> OptionContract | None:
    """The contract an arm's IV gate would read: nearest-to-spot inside its DTE band.

    Mirrors ``hunt_plan._iv_contract_for_arm`` deliberately, and the mirror is the whole
    point of the change: the recorder must write the key the *gate* reads, or the store
    accumulates a series T5 never queries and the gate stays in warmup forever. The gate
    picks nearest-to-spot-within-band (not the delta-selected deep-ITM strike arm C
    actually buys) — see ``abb3d1a`` and its note that moving the selector to the traded
    strike is a store-accumulation question. Both sides must agree, so both read the same
    rule.

    # INTERPRETATION: ``right`` is deliberately NOT filtered. One EOD sweep serves both
    arms and the gate filters by direction at read time, so recording both sides is what
    lets a call-side rank warm without a second pass tomorrow.
    """
    best: OptionContract | None = None
    for contract in chain.contracts:
        try:
            expiry_day = dt.datetime.strptime(contract.expiry or "", "%Y%m%d").date()
        except ValueError:
            continue
        dte = (expiry_day - session).days
        if not (dte_min <= dte <= dte_max):
            continue
        if best is None or abs(contract.strike - spot) < abs(best.strike - spot):
            best = contract
    return best


def _observation_for(
    contract: OptionContract,
    *,
    underlying: str,
    session: dt.date,
    spot: float,
    bucket_size: float,
    dte_bucket_days: int,
    moneyness_pct: float = MONEYNESS_BUCKET_PCT,
) -> list[IvObservation]:
    """Both tenor keys for one contract — expiry-keyed and rolling moneyness-keyed.

    RULED 2026-10-03 (operator): the rolling key's strike axis is
    moneyness-relative (``strike/spot - 1`` at ``moneyness_pct``, default 2%), so
    ``spot`` is a required argument here. The expiry-keyed series keeps its dollar
    ``$5`` buckets — it is an audit series, not the one T5 reads, and its cost is one
    extra row per contract.
    """
    try:
        expiry_day = dt.datetime.strptime(contract.expiry or "", "%Y%m%d").date()
    except ValueError:
        return []
    dte = (expiry_day - session).days
    common = {
        "underlying": underlying.upper(),
        "as_of": session.isoformat(),
        "iv": float(contract.implied_volatility),  # type: ignore[arg-type]
        "source": "alpaca_chain",
        "is_proxy": False,
        "expiry": contract.expiry,
        "right": contract.right,
        "strike": contract.strike,
    }
    return [
        IvObservation(
            tenor_key=tenor_key(
                expiry=contract.expiry,  # type: ignore[arg-type]
                right=contract.right,
                strike=contract.strike,
                bucket_size=bucket_size,
            ),
            **common,
        ),
        IvObservation(
            tenor_key=moneyness_tenor_key(
                dte=dte,
                right=contract.right,
                moneyness_bucket=moneyness_bucket(
                    contract.strike, spot, moneyness_pct
                ),
                dte_bucket_days=dte_bucket_days,
            ),
            **common,
        ),
    ]


def arm_iv_observations(
    chain: OptionChain,
    spot: float,
    *,
    session: dt.date,
    underlying: str,
    bands: tuple[tuple[str, int, int], ...] = ARM_IV_BANDS,
    bucket_size: float = STRIKE_BUCKET_SIZE,
    dte_bucket_days: int = DTE_BUCKET_DAYS,
    moneyness_pct: float = MONEYNESS_BUCKET_PCT,
) -> tuple[list[IvObservation], int]:
    """``(observations, skipped_bands)`` — one reading per arm, under that arm's own band.

    # INTERPRETATION: this is a MEASUREMENT fix, not a rule change. The rulebook already
    # defines T5 for arm C as a percentile within the traded contract's tenor
    # (``iv_on_chosen_strike_within_normal_band``); the recorder simply never captured that
    # tenor, so the gate read a bucket nothing had ever written. No threshold moves and no
    # gate changes — a series starts accumulating where there was no series.

    # INTERPRETATION: a band with no contract, or whose nearest contract carries no usable
    # IV, is SKIPPED and counted, never filled from a neighbouring expiry. Filling would
    # write a reading under a tenor key naming a contract nobody quoted, and the next
    # session's fill would name a different one — an IV history that never described a
    # single instrument. A gap is recoverable; a mislabelled series corrupts every
    # percentile built on it.
    """
    observations: list[IvObservation] = []
    skipped = 0
    seen_keys: set[str] = set()
    for _arm, dte_min, dte_max in bands:
        contract = arm_contract_for_band(chain, spot, session, dte_min, dte_max)
        if contract is None or contract.implied_volatility is None:
            skipped += 1
            continue
        if float(contract.implied_volatility) <= 0:
            skipped += 1
            continue
        for observation in _observation_for(
            contract,
            underlying=underlying,
            session=session,
            spot=spot,
            bucket_size=bucket_size,
            dte_bucket_days=dte_bucket_days,
            moneyness_pct=moneyness_pct,
        ):
            # A band can resolve to the same contract as another (a 0DTE-only chain also
            # satisfies arm C's band when it has no long-dated leg). The store's uniqueness
            # key is (underlying, as_of, tenor_key, source), so a duplicate is harmless —
            # but de-duplicating keeps ``iv_rows`` a count of facts rather than a count of
            # bands that happened to agree.
            if observation.tenor_key in seen_keys:
                continue
            seen_keys.add(observation.tenor_key)
            observations.append(observation)
    return observations, skipped


def iv_observations(
    chain: OptionChain,
    spot: float,
    *,
    session: dt.date,
    underlying: str,
    max_dte: int = IV_MAX_DTE,
    bucket_size: float = STRIKE_BUCKET_SIZE,
    dte_bucket_days: int = DTE_BUCKET_DAYS,
    moneyness_pct: float = MONEYNESS_BUCKET_PCT,
) -> tuple[list[IvObservation], int]:
    """``(observations, skipped_expiries)`` — one ATM reading per live expiry, twice-keyed.

    Reuses the Phase-3a bucketing (``iv_rank.tenor_key``, $5 strike buckets) so the
    soak and the checklist key IV history identically — for the EXPIRY-keyed audit
    series. The rolling key T5 actually reads is moneyness-relative as of the
    2026-10-03 key-schema ruling; see ``_observation_for``.

    # INTERPRETATION: for each expiry we record the **single ATM contract** (nearest
    strike to spot, calls-first tie-break — ``OptionChain.atm_contract``), not every
    strike in the chain. Two reasons, both load-bearing: the store's uniqueness key is
    ``(underlying, as_of, tenor_key, source)``, so a per-contract poll would write
    thousands of rows a day for a series that is re-read once per decision; and the
    tenor T5 needs is the ATM one, because arm B buys ATM-ish premium and arm C reads
    "IV on the chosen strike within normal band" against the same neighbourhood. One
    row per expiry per day is the smallest series that answers the question asked.

    # Each reading is stored under both the expiry-keyed tenor and the rolling
    # moneyness-keyed tenor (see ``iv_rank.moneyness_tenor_key`` for why the
    # expiry-keyed series can never leave warmup on its own, and for why the rolling
    # key is namespaced). Both are written under the same ``source``, so a reader
    # picking one is picking a tenor, not a data vintage.

    # INTERPRETATION: an expiry whose ATM contract carries no usable IV is **skipped
    # entirely** rather than filled in from the nearest contract that does. The
    # tempting alternative — "use the 626 put instead of the 625 call" — would write a
    # reading under a tenor key naming a strike nobody quoted, and the next day the
    # same key would be filled from a different strike, producing an IV history that
    # never described one contract. A missing reading is recoverable; a mislabelled
    # one silently corrupts every percentile built on it. The cost is a real gap in
    # the series, so the run reports how many expiries were skipped
    # (``iv_skipped_expiries``) instead of letting the gap pass as a quiet market.
    """
    observations: list[IvObservation] = []
    skipped = 0
    for expiry in chain.expiries():
        try:
            expiry_day = dt.datetime.strptime(expiry, "%Y%m%d").date()
        except ValueError:
            continue
        dte = (expiry_day - session).days
        if dte < 0 or dte > max_dte:
            continue
        contract = chain.atm_contract(spot, expiry)
        if contract is None:
            skipped += 1
            continue
        if contract.implied_volatility is None or float(contract.implied_volatility) <= 0:
            skipped += 1
            continue
        iv = float(contract.implied_volatility)
        common = {
            "underlying": underlying.upper(),
            "as_of": session.isoformat(),
            "iv": iv,
            "source": "alpaca_chain",
            "is_proxy": False,
            "expiry": expiry,
            "right": contract.right,
            "strike": contract.strike,
        }
        observations.append(
            IvObservation(
                tenor_key=tenor_key(
                    expiry=expiry,
                    right=contract.right,
                    strike=contract.strike,
                    bucket_size=bucket_size,
                ),
                **common,
            )
        )
        observations.append(
            IvObservation(
                tenor_key=moneyness_tenor_key(
                    dte=dte,
                    right=contract.right,
                    moneyness_bucket=moneyness_bucket(
                        contract.strike, spot, moneyness_pct
                    ),
                    dte_bucket_days=dte_bucket_days,
                ),
                **common,
            )
        )
    return observations, skipped


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------


def trailing_baseline(
    rows: Sequence[FlowBaselineRow],
    *,
    lookback_days: int = 20,
    threshold_pct: float | None = None,
) -> tuple[int, float | None, float | None]:
    """``(days, mean_call, mean_put)`` over the most recent prior sessions.

    Only the last ``lookback_days`` rows count, and rows aggregated at a different
    deep-OTM threshold are **excluded** rather than mixed in: comparing a 3%-OTM
    session against a 5%-OTM baseline is not a noisier signal, it is a different
    quantity. A threshold change therefore restarts the baseline, which is visible as
    a day count climbing back up rather than as a discontinuity in the ratios.
    """
    usable = [r for r in rows if threshold_pct is None or r.deep_otm_threshold_pct == threshold_pct]
    window = usable[-lookback_days:] if lookback_days > 0 else usable
    if not window:
        return 0, None, None
    mean_call = sum(r.deep_otm_call_volume for r in window) / len(window)
    mean_put = sum(r.deep_otm_put_volume for r in window) / len(window)
    return len(window), mean_call, mean_put


def _ratio(today: float, baseline: float | None) -> float | None:
    """Today's volume over the baseline mean, or ``None`` when that is undefined.

    A zero baseline mean yields ``None`` rather than infinity: "0 x baseline" is not a
    ratio, and a division that returns ``inf`` here would sail through any ``>= N``
    comparison as a pass.
    """
    if baseline is None or baseline <= 0:
        return None
    return today / baseline


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SoakRun:
    """Outcome of one soak run. ``summary`` is the single stdout line."""

    session: str | None
    status: str
    summary: str
    flow_row: FlowBaselineRow | None = None
    iv_rows: int = 0
    iv_skipped_expiries: int = 0
    #: Arm bands that resolved to no contract (or an unreadable one) on this run. Kept
    #: separate from ``iv_skipped_expiries`` because they are a different fact: this one
    #: says "this arm has no tenor to rank today", which for arm C is the warm-up
    #: clock, not a hole in the series.
    iv_skipped_bands: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def wrote_flow(self) -> bool:
        return self.flow_row is not None


def build_flow_row(
    *,
    session: dt.date,
    underlying: str,
    feed: str,
    is_delayed: bool,
    spot: float,
    zero_dte_expiry: str | None,
    chain_contracts: int,
    agg: FlowAggregate,
    baseline_rows: Sequence[FlowBaselineRow],
    lookback_days: int,
    strategy_version: str,
) -> FlowBaselineRow:
    """Assemble the row, including the baseline means and T6 ratios.

    The baseline is built from rows dated **strictly before** this session (the
    caller passes them pre-filtered), so a session never counts itself.
    """
    days, mean_call, mean_put = trailing_baseline(
        baseline_rows, lookback_days=lookback_days, threshold_pct=agg.threshold_pct
    )
    return FlowBaselineRow(
        date=session.isoformat(),
        underlying=underlying.upper(),
        feed=feed,
        is_delayed=is_delayed,
        session_spot=spot,
        zero_dte_expiry=zero_dte_expiry,
        chain_contracts=chain_contracts,
        deep_otm_threshold_pct=agg.threshold_pct,
        deep_otm_call_volume=agg.call_volume,
        deep_otm_put_volume=agg.put_volume,
        deep_otm_total_volume=agg.total_volume,
        call_volume_by_distance=agg.call_by_distance,
        put_volume_by_distance=agg.put_by_distance,
        baseline_lookback_days=lookback_days,
        baseline_days=days,
        baseline_call_mean=mean_call,
        baseline_put_mean=mean_put,
        ratio_call=_ratio(agg.call_volume, mean_call),
        ratio_put=_ratio(agg.put_volume, mean_put),
        strategy_version=strategy_version,
        ts=utcnow(),
    )


def run_soak(
    *,
    source: SoakData,
    conn: Any,
    iv_store: IvRankStore | None,
    rules: Rulebook,
    session: dt.date | None = None,
    underlying: str | None = None,
    threshold_pct: float = DEFAULT_DEEP_OTM_PCT,
    force: bool = False,
    dry_run: bool = False,
) -> SoakRun:
    """One EOD soak pass. See the module docstring for the whole contract.

    ``conn`` is a journal SQLite connection (already initialised with schema.sql);
    ``iv_store`` may be ``None`` to skip the IV half, but the soak refuses to skip it
    silently — the run summary says so, because "IV history is accumulating" is the
    claim this job exists to make true.
    """
    symbol = (underlying or rules.strategy.symbol).upper()
    t6 = rules.checklist.t6_flow
    lookback_days = t6.baseline_lookback_days

    if session is not None and is_weekend(session):
        return SoakRun(
            session=session.isoformat(),
            status="weekend",
            summary=f"soak: {session.isoformat()} is a weekend — no session, nothing recorded",
        )

    bars = source.daily_bars(symbol)
    session_day, reason = resolve_session(session, bars, force=force)
    if session_day is None:
        return SoakRun(
            session=None,
            status="no_session",
            summary=f"soak: no session to record — {reason}",
            details={"reason": reason},
        )

    existing = conn.execute(
        "SELECT id FROM flow_baseline WHERE date = ?", (session_day.isoformat(),)
    ).fetchone()
    if existing is not None:
        return SoakRun(
            session=session_day.isoformat(),
            status="already_recorded",
            summary=(
                f"soak: {session_day.isoformat()} already has a flow_baseline row "
                f"(id={existing['id']}) — idempotent no-op, nothing duplicated"
            ),
        )

    chain = source.option_chain(symbol)
    spot = bars.bars[-1].c if bars.bars else 0.0
    if spot <= 0:
        raise SoakError(f"no usable SPY close for {session_day.isoformat()} (spot={spot})")

    expiry = session_day.strftime("%Y%m%d")
    zero_dte = chain.for_expiry(expiry)
    if not zero_dte:
        # Honest empty, not a silent zero: on a session with no listed 0DTE expiry the
        # chain genuinely has nothing to aggregate, and writing zeros would put a fake
        # 0.0x-baseline session into a distribution N is calibrated from.
        return SoakRun(
            session=session_day.isoformat(),
            status="no_zero_dte",
            summary=(
                f"soak: {session_day.isoformat()} has no listed 0DTE expiry "
                f"({expiry}) in a {len(chain)}-contract chain — no flow row written"
            ),
            details={"contracts": len(chain)},
        )

    agg = aggregate_flow(zero_dte, spot, threshold_pct=threshold_pct)
    prior = list_flow_baseline(conn, before=session_day.isoformat())
    row = build_flow_row(
        session=session_day,
        underlying=symbol,
        feed=CHAIN_FEED,
        is_delayed=CHAIN_FEED_IS_DELAYED,
        spot=spot,
        zero_dte_expiry=expiry,
        chain_contracts=len(zero_dte),
        agg=agg,
        baseline_rows=prior,
        lookback_days=lookback_days,
        strategy_version=rules.strategy_version,
    )

    iv_rows = 0
    iv_skipped = 0
    iv_skipped_bands = 0
    if iv_store is not None:
        observations, iv_skipped = iv_observations(
            chain,
            spot,
            session=session_day,
            underlying=symbol,
            # Written under the SAME widths the plan reads, or T5 scores a series
            # nothing ever wrote (FIX 2026-10-03, R4).
            bucket_size=STRIKE_BUCKET_SIZE,
            dte_bucket_days=rules.checklist.t5_options_chain.dte_bucket_days,
            moneyness_pct=MONEYNESS_BUCKET_PCT,
        )
        # Per-arm rows on top of the per-expiry sweep. Without these the ATM sweep cannot
        # write arm C's 90-180 DTE tenor at all: the nearest-to-spot contract in that band
        # is not the ATM contract of any expiry the sweep visits, so the gate read a key
        # that accumulated nothing and T5 sat in warmup by construction (see IV_MAX_DTE).
        arm_observations, arm_skipped = arm_iv_observations(
            chain,
            spot,
            session=session_day,
            underlying=symbol,
            bands=_bands_from_rules(rules),
            bucket_size=STRIKE_BUCKET_SIZE,
            dte_bucket_days=rules.checklist.t5_options_chain.dte_bucket_days,
            moneyness_pct=MONEYNESS_BUCKET_PCT,
        )
        observations = observations + arm_observations
        iv_skipped_bands = arm_skipped
        iv_rows = iv_store.record_many(observations)

    if dry_run:
        return SoakRun(
            session=session_day.isoformat(),
            status="dry_run",
            summary=(
                f"soak: dry-run {session_day.isoformat()} — computed "
                f"deep-OTM>={agg.threshold_pct:g}% call={agg.call_volume:.0f} "
                f"put={agg.put_volume:.0f}, baseline {row.baseline_days}/{lookback_days}d, "
                f"{iv_rows} IV row(s), {iv_skipped} expiry(ies) skipped; nothing written"
            ),
            flow_row=row,
            iv_rows=iv_rows,
            iv_skipped_expiries=iv_skipped,
            iv_skipped_bands=iv_skipped_bands,
            details=agg.to_dict() | {"session_spot": spot, "reason": reason},
        )

    try:
        row_id = append_flow_baseline(conn, row)
    except DuplicateFlowBaseline:
        return SoakRun(
            session=session_day.isoformat(),
            status="already_recorded",
            summary=(
                f"soak: {session_day.isoformat()} already has a flow_baseline row — "
                f"idempotent no-op, nothing duplicated"
            ),
        )

    return SoakRun(
        session=session_day.isoformat(),
        status="recorded",
        summary=(
            f"soak: {session_day.isoformat()} recorded (row {row_id}) — "
            f"deep-OTM>={agg.threshold_pct:g}% call={agg.call_volume:.0f} "
            f"({_fmt_ratio(row.ratio_call)}x {lookback_days}d) "
            f"put={agg.put_volume:.0f} ({_fmt_ratio(row.ratio_put)}x), "
            f"{_iv_note(iv_rows, iv_skipped, iv_skipped_bands)}"
        ),
        flow_row=row,
        iv_rows=iv_rows,
        iv_skipped_expiries=iv_skipped,
        iv_skipped_bands=iv_skipped_bands,
        details=agg.to_dict() | {"session_spot": spot, "reason": reason, "row_id": row_id},
    )


def _iv_note(iv_rows: int, skipped: int, skipped_bands: int = 0) -> str:
    """The IV half of the summary line.

    # INTERPRETATION: the skip count is reported *whenever it is non-zero*, not only
    when it is alarming. A reader who sees ``0 expiries skipped`` learns nothing new
    and stops looking; a reader who sees ``3 expiries skipped`` learns that three
    days of IV history for those tenors are missing while it is still cheap to go
    back and find out why.

    # INTERPRETATION: band skips are reported separately and labelled as the warm-up
    clock rather than folded into the expiry count. "3 expiries skipped" means three
    holes in a series that otherwise accumulates; "1 arm band skipped" means an arm
    has no tenor to rank today, which is the expected state until T5 warms and would
    be misread as corruption if it were summed into the other number.
    """
    parts = [f"{iv_rows} IV row(s)"]
    if skipped:
        parts.append(f"{skipped} expiry(ies) skipped (no usable ATM IV)")
    if skipped_bands:
        parts.append(f"{skipped_bands} arm band(s) with no rankable contract")
    return ", ".join(parts)


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


# ---------------------------------------------------------------------------
# fixtures (dry-run)
# ---------------------------------------------------------------------------


def load_fixture_source(fixture_dir: Path = FIXTURE_DIR, symbol: str = "SPY") -> _FixtureSource:
    """Build a :class:`_FixtureSource` from the repo's canned payloads.

    The chain fixture's contracts are stamped with their own expiries (a December
    quarterly plus a near one), so a dry run against it is a *replay* of that fixture's
    session, not of today. That is the point: dry-run answers "does the pipeline
    compute a row from this payload", and the session date is passed in explicitly by
    the test.
    """
    bars_payload = json.loads((fixture_dir / "stock_bars.json").read_text(encoding="utf-8"))
    chain_payload = json.loads((fixture_dir / "option_chain.json").read_text(encoding="utf-8"))
    # The fixture mirrors the documented single-symbol envelope: ``bars`` is a flat list.
    bars_block = bars_payload.get("bars", [])
    raw = (
        bars_block
        if isinstance(bars_block, list)
        else bars_block.get(symbol) or next(iter(bars_block.values()), [])
    )
    bars = [Bar.from_json(b) for b in raw]
    bars.sort(key=lambda b: b.t)
    chain = _chain_from_payload(symbol, CHAIN_FEED, chain_payload)
    return _FixtureSource(
        BarSeries(symbol=symbol, timeframe="1Day", feed=BAR_FEED, bars=bars),
        chain,
    )


def _chain_from_fixture(payload: dict[str, Any], symbol: str) -> OptionChain:
    """Parse the canned chain envelope (mirrors ``alpaca_client._chain_from_payload``)."""
    return _chain_from_payload(symbol, CHAIN_FEED, payload)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soak",
        description="Daily EOD soak: accumulate T6 flow inputs and IV-rank history",
    )
    parser.add_argument("--rules", default=str(DEFAULT_RULES_PATH), help="frozen rulebook path")
    parser.add_argument("--db", default=str(DEFAULT_JOURNAL_DB), help="journal SQLite path")
    parser.add_argument("--iv-db", default=str(DEFAULT_IV_DB_PATH), help="IV-rank store path")
    parser.add_argument(
        "--session", default=None, help="session date YYYY-MM-DD (default: today ET)"
    )
    parser.add_argument("--symbol", default=None, help="override the rulebook symbol")
    parser.add_argument(
        "--deep-otm-pct",
        type=float,
        default=DEFAULT_DEEP_OTM_PCT,
        help=(
            f"deep-OTM distance threshold, percent from spot (default {DEFAULT_DEEP_OTM_PCT:g}; "
            "floored to an integer, and NOT a frozen rule — see rulebook t6_flow.deep_otm)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="record even without a daily bar for the date (operator override, logged)",
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
        session = (
            dt.date.fromisoformat(args.session)
            if args.session
            else to_et(dt.datetime.now(dt.UTC)).date()
        )
        if args.dry_run:
            source: SoakData = load_fixture_source(symbol=(args.symbol or rules.strategy.symbol))
            conn = init_db(":memory:")
            iv_store = None
        else:
            client = AlpacaClient.from_env()
            source = _AlpacaSource(client)
            conn = init_db(args.db)
            iv_store = IvRankStore.open(args.iv_db)

        run = run_soak(
            source=source,
            conn=conn,
            iv_store=iv_store,
            rules=rules,
            session=session,
            underlying=args.symbol,
            threshold_pct=args.deep_otm_pct,
            force=args.force,
            dry_run=args.dry_run,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "status": run.status,
                        "session": run.session,
                        "summary": run.summary,
                        "iv_rows": run.iv_rows,
                        "iv_skipped_expiries": run.iv_skipped_expiries,
                        "iv_skipped_bands": run.iv_skipped_bands,
                        "details": run.details,
                        "row": run.flow_row.model_dump(mode="json") if run.flow_row else None,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(run.summary)
        return 0
    except Exception as exc:  # noqa: BLE001 - a cron job prints the failure, it does not traceback
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


__all__ = [
    "ARM_IV_BANDS",
    "BAR_FEED",
    "CHAIN_FEED",
    "CHAIN_FEED_IS_DELAYED",
    "DEFAULT_DEEP_OTM_PCT",
    "DISTANCE_BUCKET_PCT",
    "FlowAggregate",
    "IV_MAX_DTE",
    "SoakData",
    "SoakError",
    "SoakRun",
    "aggregate_flow",
    "arm_contract_for_band",
    "arm_iv_observations",
    "bucket_key",
    "build_flow_row",
    "build_parser",
    "is_weekend",
    "iv_observations",
    "load_fixture_source",
    "main",
    "newest_bar_day",
    "otm_distance_pct",
    "resolve_session",
    "run_soak",
    "threshold_from_buckets",
    "trailing_baseline",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
