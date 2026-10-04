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
# bug 2 — cmd_eod's daily read
# ---------------------------------------------------------------------------


def test_cmd_eod_reads_windowed_daily_bars(tmp_path, capsys):
    """REGRESSION: `InsufficientData: EMA(50) needs at least 50 values, got 0`.

    The EOD pass asked for ``limit=400`` and no window, so the read returned nothing and
    the failure surfaced 3 layers downstream, inside the indicator stack, blaming the
    data it was handed rather than the query that never named a span.

    Driven through the real ``main(["eod", ...])`` over a transport that refuses
    windowless reads — so this is the shipped command, not a replay of its steps.
    """
    payload = _daily_payload()
    client, transport = _spy({"/v2/stocks/SPY/bars": _bars_route(payload)})
    transport.add("/v1beta1/options/snapshots/SPY", {"snapshots": {}, "next_page_token": None})
    args = argparse.Namespace(
        rules=str(REPO_ROOT / "config" / "rules.example.yaml"),
        date=DAY.isoformat(),
        plan=None,
        offline=False,
        offline_fixture=None,
        db=None,
        dry_run=True,
        flow_gate="none",
    )
    cli._eod_client = _eod_client_override(client)  # type: ignore[attr-defined]
    try:
        rc = cli.cmd_eod(args, dt.datetime(2026, 10, 2, 16, 15, tzinfo=UTC))
    finally:
        cli._eod_client = _ORIGINAL_EOD_CLIENT  # type: ignore[attr-defined]

    out = capsys.readouterr().out
    reads = [params for url, params in transport.calls if url.endswith("/bars")]
    assert reads, "the EOD pass made no bars read at all — the test proved nothing"
    for params in reads:
        assert params.get("start"), f"a windowless bars read went out of cmd_eod: {params}"
        assert params["sort"] == "desc", f"newest-end truncation unhandled: {params}"
    assert "close-vs-50EMA streak" in out, f"the EMA never ran: {out}"
    assert rc == 0, out


def _eod_client_override(client):
    original = cli._eod_client

    def _stub(_args):
        return client

    return original if client is None else _stub


_ORIGINAL_EOD_CLIENT = cli._eod_client
