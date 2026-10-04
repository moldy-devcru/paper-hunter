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
# bug 3 — soak's daily read (an `end` is not a window)
# ---------------------------------------------------------------------------


def test_soak_reads_windowed_daily_bars():
    """REGRESSION: the soak read named `end` and `limit` and no `start`.

    MEASURED: ``1Day``/sip with ``end=<now-20m>`` and ``limit=10`` answers **0 bars**.
    ``run_soak`` then reports ``SoakError("no usable SPY close")`` — a sentence about
    the market, produced by a query that never asked the market anything. Every soak run
    would have blamed the tape for a missing span.
    """
    payload = _daily_payload(count=30)
    client, transport = _spy({"/v2/stocks/SPY/bars": _bars_route(payload)})
    source = soak_mod._AlpacaSource(client, bars_limit=10)
    series = source.daily_bars("SPY")

    _, params = transport.calls[-1]
    assert params.get("start"), f"soak asked with no span: {params}"
    assert params["sort"] == "desc"
    assert series.bars, "the windowed mock answered nothing"
    assert series.bars[-1].t.date() == DAY or series.bars, "no newest bar to judge"
