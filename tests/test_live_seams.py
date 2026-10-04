"""The live seam sweep of 2026-10-03 — every live read path, audited once.

Three consecutive live seams shipped from one bug class (ede6342 fixed the first; this
file fixes the next three and pins the class shut):

  * ``cmd_hunt_plan``   — plan provider read ``limit`` and no window → 0 bars.
  * ``watch_snapshot``  — intraday spot read ``limit`` and no window → 0 bars.
  * ``_run_rate_series``— run-rate read ``limit`` and no window → 0 bars, swallowed.
  * ``cmd_eod``         — daily read ``limit`` and no window → 0 bars → EMA50 starved.
  * ``soak._AlpacaSource`` — daily read ``end` and no ``start`` → 0 bars → "no close".
  * ``_watch_iv_rank``  — read ``.rank`` off an ``IvObservation``, which has no such
    attribute; ``rank`` lives on ``IvRankResult``, the thing ``iv_rank()`` RETURNS.

The two halves of the class, and both are worth naming because they fail differently:

  (a) a bars read that names no resolvable span answers **HTTP 200 with zero bars** —
      and several of those reads swallow the emptiness into a *fallback*, so the run
      reads like a pass. Measured 2026-10-03, paper creds, ``/v2/stocks/SPY/bars``:
      ``1Day``/sip ``limit=400`` → 0; ``1Day``/iex ``limit=400`` → 0;
      ``1Day``/sip ``end=<now-20m> limit=400`` → 0 (an end alone is NOT a window);
      ``1Day``/sip ``bar_window(400)`` → 400, newest 2026-10-02.
  (b) a store accessor returns a *different type* than the reader assumes —
      ``observations()`` yields ``IvObservation``, not ``IvRankResult``.

Offline by construction. Every test here injects a
:class:`~executor.alpaca_client.MockTransport` or an on-disk temp SQLite store; the
mock deliberately refuses windowless bars reads the way the live route does, because a
mock that always answers bars is precisely the fidelity gap that let all of this ship
against a green suite. No test here reads a credential or opens a socket.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

from executor import main as cli
from executor import soak as soak_mod
from executor.iv_rank import MIN_OBSERVATIONS, IvRankStore

# NOTE: `executor.bar_windows` is imported INSIDE the tests that need it, not here.
# This file is a regression suite for seams that shipped on top of a green suite, so
# every one of its tests has to be runnable — and FAILING — against the parent commit,
# where the module does not exist yet. A module-level import of the fix turns
# "8 tests fail on ede6342" into "1 collection error", which proves much less.

UTC = dt.UTC
DAY = dt.date(2026, 10, 2)
LONG = 260

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# fidelity helpers — a bars route that behaves like the live one
# ---------------------------------------------------------------------------


def _bars_route(payload: dict, *, require: str = "start"):
    """The live route's contract: a query with no resolvable span answers zero bars.

    ``require="start"`` is the measured rule (``end`` alone is not enough either).
    """

    def route(params: dict) -> dict:
        if not params.get(require):
            return {"bars": [], "symbol": "SPY", "next_page_token": None}
        return payload

    return route


def _spy(routes: dict):
    from executor.alpaca_client import AlpacaClient, MockTransport

    transport = MockTransport(routes)
    client = AlpacaClient(transport=transport, key="k-not-real", secret="s-not-real")
    return client, transport


def _daily_payload(count: int = LONG) -> dict:
    from tests.fixtures_synthetic import synthetic_daily_payload

    return synthetic_daily_payload(count=count)

# ---------------------------------------------------------------------------
# bug 1 — the store-object seam: `.rank` read off an IvObservation
# ---------------------------------------------------------------------------


def _backfilled_store(path: Path, *, rows: int = MIN_OBSERVATIONS + 5) -> Path:
    """A store with the shape the live ``ivrank.db`` grew after the backfill ran.

    source=bars_bs_inversion, origin=backfill, one tenor with enough rows to rank.
    The old code never crashed on an EMPTY store, so the regression has to build a
    populated one.
    """
    store = IvRankStore.open(path)
    base = dt.date(2025, 11, 3)
    for i in range(rows):
        store.record(
            underlying="SPY",
            as_of=base + dt.timedelta(days=i),
            tenor="mte91-put-mny+0.00",
            iv=0.10 + i * 0.001,
            source="bars_bs_inversion",
            origin="backfill",
        )
    store.close()
    return path


def _empty_plan():
    from executor.hunt_plan import HuntPlan

    return HuntPlan(
        day=DAY,
        symbol="SPY",
        generated_ts="2026-10-03T12:30:00Z",
        strategy_version="test",
        config_sha256="test",
        arms=(),
        event_day_veto=False,
        event_reasons=(),
    )


def test_watch_iv_rank_reads_a_populated_store_without_raising(tmp_path):
    """REGRESSION: `AttributeError: 'IvObservation' object has no attribute 'rank'`.

    ``store.observations()`` returns ``IvObservation`` — a stored *reading*. ``rank``
    lives on ``IvRankResult``, what ``store.iv_rank()`` RETURNS. The watch leg read
    ``o.rank`` off the former, so it raised on every store that had rows and passed on
    every empty one — which is precisely the population the tests were written against.
    """
    from config.loader import load_rules

    db = _backfilled_store(tmp_path / "ivrank.db")
    args = argparse.Namespace(iv_rank=None, ivrank=str(db))
    value, source = cli._watch_iv_rank(args, _empty_plan(), load_rules())

    assert value is not None, f"T5 stayed PENDING against a warm store: {source}"
    assert 0.0 <= float(value) <= 100.0
    assert "mte91-put-mny+0.00" in source, f"the tenor is not auditable from the note: {source}"


def test_watch_iv_rank_says_why_when_the_store_is_cold(tmp_path):
    """The other half: a store that cannot rank must say so, not raise and not invent.

    Before this fix both states were indistinguishable — the warm store raised, the cold
    store "passed". Both had to be pinned.
    """
    from config.loader import load_rules

    cold = tmp_path / "cold.db"
    store = IvRankStore.open(cold)
    store.record(
        underlying="SPY",
        as_of=DAY,
        tenor="mte91-put-mny+0.00",
        iv=0.2,
        source="bars_bs_inversion",
        origin="backfill",
    )
    store.close()

    args = argparse.Namespace(iv_rank=None, ivrank=str(cold))
    value, source = cli._watch_iv_rank(args, _empty_plan(), load_rules())
    assert value is None
    assert "MIN_OBSERVATIONS" in source, source

    absent = argparse.Namespace(iv_rank=None, ivrank=str(tmp_path / "absent.db"))
    value, source = cli._watch_iv_rank(absent, _empty_plan(), load_rules())
    assert value is None and "no IV store" in source


def test_every_attribute_a_live_reader_touches_exists_on_a_store_row(tmp_path):
    """The class, stated as a protocol: materialise from a ROW, read what readers read.

    ``_observation_from_row`` is the only constructor on this path, and every live
    reader of an observation (``executor.main._watch_iv_rank``,
    ``executor.soak._collect_chain_iv``/``_iv_series``, ``executor.iv_backfill``,
    ``scripts/verify_t5_warm``, ``scripts/backfill_iv_rank``) reads the attributes
    listed here. A row that omits one — a backfill writing fewer columns than a live
    poll, a migration adding one — fails HERE, at the boundary, instead of at the far
    end of a trading session.
    """
    db = _backfilled_store(tmp_path / "ivrank.db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    store = IvRankStore(conn)
    try:
        rows = store.observations("SPY")
    finally:
        store.close()
    assert rows, "the fixture wrote nothing"

    touched = (
        "underlying",
        "as_of",
        "tenor_key",
        "iv",
        "source",
        "is_proxy",
        "expiry",
        "right",
        "strike",
        "created_at",
        "origin",
    )
    for obs in rows:
        for name in touched:
            assert hasattr(obs, name), f"IvObservation from a row lacks .{name}"
    # And the attribute the watch leg reached for that no observation has: this is the
    # distinction the fix turns on, so it is asserted rather than left in a comment.
    assert not hasattr(rows[0], "rank"), (
        "IvObservation grew a `rank` — if it is ever meant to carry one, the watch leg's "
        "store.iv_rank() call is now computing a rank the type already had"
    )


def test_store_iv_rank_is_the_only_thing_that_makes_a_rank(tmp_path):
    """The two types, kept apart: a reading is not a score, and a score is not a reading."""
    db = _backfilled_store(tmp_path / "ivrank.db")
    store = IvRankStore.open(db)
    try:
        obs = store.latest("SPY", "mte91-put-mny+0.00")
        result = store.iv_rank(obs.iv, "SPY", obs.tenor_key, as_of=obs.as_of)
        assert result.rank is not None
        assert result.rank != obs.iv, "a rank and a reading are different numbers"
        assert result.to_dict()["iv_rank"] == result.rank
    finally:
        store.close()
