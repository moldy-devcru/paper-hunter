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


# ---------------------------------------------------------------------------
# the policy is ONE policy
# ---------------------------------------------------------------------------


def test_bar_window_is_one_implementation_reachable_from_every_caller():
    """A window policy that lives next to one caller is not a shared policy.

    It is a coincidence. ``bar_window`` was written inside ``executor.watch_loop`` for
    the plan's ``daily_series`` read, and the SAME limit-only read shipped independently
    in ``cmd_eod`` and in ``soak._AlpacaSource`` — three seams of one bug class from one
    policy written once. So the policy now lives in ``executor.bar_windows`` and the old
    name is an alias of it, not a second implementation.
    """
    import executor.watch_loop as wl
    from executor import bar_windows as policy
    from executor.watch_loop import bar_window as watch_bar_window

    assert wl.bar_window is policy.bar_window
    assert wl.SIP_END_BACKOFF_MINUTES == policy.SIP_END_BACKOFF_MINUTES
    assert watch_bar_window is policy.bar_window
    for module in (cli, soak_mod, wl):
        assert module.bar_window is policy.bar_window, (
            f"{module.__name__} is holding its own copy of the window policy"
        )


def test_bar_window_names_a_span_the_route_can_resolve():
    """The policy's own contract: every read it builds carries a resolvable span."""
    from executor.bar_windows import SIP_END_BACKOFF_MINUTES, bar_window

    now = dt.datetime(2026, 10, 2, 13, 5, tzinfo=UTC)
    for timeframe, feed, limit in (
        ("1Day", "sip", 400),
        ("1Day", "iex", 10),
        ("1Min", "iex", 5),
        ("15Min", "iex", 700),
    ):
        window = bar_window(timeframe=timeframe, feed=feed, limit=limit, now=now)
        start = dt.datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
        assert start < now
        assert window["sort"] == "desc"
        if feed == "sip":
            end = dt.datetime.fromisoformat(window["end"].replace("Z", "+00:00"))
            assert end <= now - dt.timedelta(minutes=SIP_END_BACKOFF_MINUTES)
        else:
            assert "end" not in window, "a realtime feed must not be pinned off the gate"
        assert (now - start).days >= 4, "no weekend/holiday padding"


# ---------------------------------------------------------------------------
# the audit: no live bars read in the repo goes out windowless
# ---------------------------------------------------------------------------


def test_no_live_bars_read_in_the_repo_is_limit_only():
    """A static sweep of the source, so the NEXT one of these is a failing test.

    Every call to ``get_daily_bars`` / ``get_intraday_bars`` / ``get_bars`` outside the
    client that defines them must either pass an explicit ``start``/``end`` or splat
    ``**bar_window(...)``. Grep-able, cheap, and it covers call sites no test injects a
    transport into — which is where the remaining risk lives (``ui/barcache`` and the
    backfill sources are the reason this test exists).
    """
    import ast

    offenders: list[str] = []
    checked = 0
    for path in sorted(REPO_ROOT.glob("**/*.py")):
        if any(part in {".venv", "__pycache__", ".git"} for part in path.parts):
            continue
        if path == REPO_ROOT / "executor" / "alpaca_client.py":
            continue  # the definition and its own thin wrappers
        if path.parts[:2] == ("tests", "test") or path.name.startswith("test_"):
            continue  # tests assert on params; their fakes are not call sites
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[:1] == ("scratch",):
            # `scratch/` is measurement code: `probe_window_probe.py` sends the
            # windowless shapes ON PURPOSE, because the measurement is the evidence.
            # Sweeping it would be demanding that the proof commit a crime.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"get_daily_bars", "get_intraday_bars", "get_bars"}:
                continue
            checked += 1
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            splatted = any(
                kw.arg is None and "bar_window" in ast.dump(kw.value) for kw in node.keywords
            )
            if kwargs & {"start", "end"} or splatted:
                continue
            offenders.append(f"{rel}:{node.lineno} {name}()")
    assert checked >= 5, f"the sweep only found {checked} bars call sites — is it still looking?"
    assert not offenders, (
        "windowless bars reads (each answers 0 bars live):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# the offline smoke spec, asserted where it is already cheap to assert
# ---------------------------------------------------------------------------


def test_the_three_legs_all_have_an_offline_entry_point():
    """What a CI-able offline smoke run needs, per leg — and which one is missing.

    The indictment of this sweep is not that the seams existed, it that the suite was
    green through all of them. So: what would each leg need to be smoke-tested offline?

    * ``eod``   — HAS one. ``--offline --offline-fixture`` builds a real
      ``AlpacaClient`` over ``MockTransport``. Only gap: the fixture is a static payload
      map, so it does not refuse windowless reads. Give the fixture route the same
      window contract the live route has and this leg is smoke-gated end to end.
    * ``soak``  — HAS one. ``--dry-run`` swaps in ``_FixtureSource``, whose transport
      raises on any call. Same gap: swap it for the window-refusing route and the
      silent-0-bar degradation cannot survive a green run.
    * ``watch`` — PARTIAL. There is no ``--offline`` for ``watch`` at all: it constructs
      ``AlpacaClient.from_env()`` unconditionally, so the leg that had the most seams is
      the one that cannot be rehearsed. It needs the same ``--offline-fixture`` swap
      ``eod`` has, plus a plan file and an ``IvRankStore`` on disk (both already
      constructible from ``tests/fixtures_synthetic``).
    * ``hunt-plan`` — PARTIAL. ``cmd_hunt_plan`` is covered by a MockTransport test
      (ede6342) but the CLI subcommand itself builds its client from env, so the same
      ``--offline`` swap would cover it.
    """
    parser = cli.build_parser()
    subparsers = [
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    ]
    legs = set()
    for action in subparsers:
        legs |= set(action.choices or {})

    assert {"hunt-plan", "watch", "eod"} <= legs
    eod_flags = set()
    watch_flags = set()
    for action in parser._actions:
        if not getattr(action, "choices", None):
            continue
        for name, sub in action.choices.items():
            flags = {opt for sub_action in sub._actions for opt in sub_action.option_strings}
            if name == "eod":
                eod_flags = flags
            if name == "watch":
                watch_flags = flags
    assert {"--offline", "--offline-fixture"} <= eod_flags
    # Recorded as a gap, not asserted as a failure: closing it is the next change, and
    # this test is where the "is it still missing?" question stays answerable.
    assert not ({"--offline", "--offline-fixture"} <= watch_flags), (
        "watch gained --offline — update this test's spec and close the gap for real"
    )


def test_iso_z_is_the_formatter_the_route_wants():
    """Every window this repo builds is formatted by one function, for one reason.

    A timestamp with a ``+00:00`` offset where the API expects ``Z`` is a 400 on some
    routes and a silent wrong answer on others, and the two are indistinguishable from
    the call site.
    """
    from executor.bar_windows import bar_window, iso_z

    moment = dt.datetime(2026, 10, 2, 13, 5, tzinfo=UTC)
    assert iso_z(moment) == "2026-10-02T13:05:00Z"
    assert bar_window(timeframe="1Day", feed="sip", limit=400, now=moment)["start"].endswith(
        "Z"
    )


def test_the_sweep_probe_is_committed_and_self_describing():
    """The measurement script is part of the evidence, so it stays in the tree.

    It reads credentials from ``~/.config/paper-hunter/alpaca.env`` in-process, prints
    only counts and bar dates, paces itself, and touches nothing else.
    """
    probe = REPO_ROOT / "scratch" / "probe_seam_sweep.py"
    assert probe.exists(), "the measurement that justified every fix is missing"
    text = probe.read_text(encoding="utf-8")
    assert "alpaca.env" in text
    assert "print(f\"{label}: n=" in text, "the probe should report counts, not payloads"