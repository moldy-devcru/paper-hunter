"""The FastAPI app: read-only enforcement, indicator dogfooding, honest empty states.

Three things this module is really asserting, in order of how much they matter:

1. **The app cannot write.** Zero non-GET routes, every database opened ``mode=ro``,
   and a test that tries an ``INSERT`` through the same connection factory the app
   uses and watches SQLite refuse it. "We were careful" is not a property a research
   tool should be trusted on.
2. **The indicators are the executor's indicators.** ``/api/indicators`` is compared
   value-for-value against a direct call to :mod:`executor.indicators` on the same
   closes. A second implementation drifting in is the failure this pins shut.
3. **An empty journal is a page, not a stack trace.** Every endpoint answers 200 with
   a structured body before the trading window opens, because that is the state the
   terminal will be in for the first 537 tests' worth of nothing.

Offline throughout: a tmpdir journal built from the repo's own schema, a tmpdir bar
cache, and a fixture transport. No network, no real ``data/`` files, no long-running
server (FastAPI's TestClient drives the ASGI app in-process).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from executor import indicators
from executor.alpaca_client import MockTransport, stock_bars_path
from journal.store import FlowBaselineRow, append_flow_baseline, init_db
from tests.fixtures_journal import build_journal
from ui.aggregate import AggBar
from ui.api import UISettings, app, configure, open_ro, ro_uri
from ui.barcache import BarCache

UTC = dt.UTC
ET = ZoneInfo("America/New_York")
SYMBOL = "SPY"
SESSION_OPEN = dt.datetime(2026, 9, 15, 13, 30, tzinfo=UTC)  # 09:30 ET, a Tuesday


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _session_bars(count: int = 390, *, start: dt.datetime = SESSION_OPEN) -> list[AggBar]:
    """A full 09:30-16:00 ET session of one-minute bars with a linear price ramp."""
    out = []
    for i in range(count):
        base = 500.0 + i * 0.1
        out.append(
            AggBar(
                t=start + dt.timedelta(minutes=i),
                o=base,
                h=base + 0.2,
                l=base - 0.2,
                c=base + 0.1,
                v=1000.0,
                n=5,
                vw=base + 0.05,
                feed="sip",
            )
        )
    return out


def _multi_session_bars(sessions: int = 2) -> list[AggBar]:
    """Consecutive full sessions of one-minute bars, one session per trading day."""
    out: list[AggBar] = []
    for d in range(sessions):
        day = SESSION_OPEN + dt.timedelta(days=d)  # 15th, 16th — both trading days
        out.extend(_session_bars(start=day.replace(hour=13, minute=30)))
    return out


def _test_client(journal: Path, cache: Path, tmp: Path) -> Iterator[TestClient]:
    """Build a TestClient for an explicit journal/cache pair and tear it down."""
    ctx = configure(
        UISettings(
            journal_path=journal,
            ivrank_path=tmp / "missing-ivrank.db",
            barcache_path=cache,
        )
    )
    with TestClient(app) as test_client:
        yield test_client
    ctx.close()


@pytest.fixture
def deep_client(tmp_path: Path, journal_path: Path) -> Iterator[TestClient]:
    """Two sessions cached, so 15m yields 52 candles — enough for a 50-period EMA."""
    cache_path = tmp_path / "barcache-deep.db"
    seed_cache(cache_path, _multi_session_bars())
    yield from _test_client(journal_path, cache_path, tmp_path)


def seed_cache(path: Path, bars: list[AggBar], *, timeframe: str = "1Min") -> None:
    with BarCache(path, client=None) as cache:
        cache.put(SYMBOL, timeframe, bars, feed="sip")


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    """The repo's canned two-week journal on disk, built from the real schema."""
    path = tmp_path / "journal.db"
    conn = init_db(path)
    build_journal(conn)
    conn.close()
    return path


@pytest.fixture
def empty_journal_path(tmp_path: Path) -> Path:
    """Schema present, no rows — the state before the window opens."""
    path = tmp_path / "empty-journal.db"
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def client(tmp_path: Path, journal_path: Path) -> Iterator[TestClient]:
    """A TestClient wired to a tmp journal + tmp bar cache, torn down afterwards."""
    cache_path = tmp_path / "barcache.db"
    seed_cache(cache_path, _session_bars())
    ctx = configure(
        UISettings(
            journal_path=journal_path,
            ivrank_path=tmp_path / "missing-ivrank.db",
            barcache_path=cache_path,
        )
    )
    with TestClient(app) as test_client:
        yield test_client
    ctx.close()


@pytest.fixture
def empty_client(tmp_path: Path, empty_journal_path: Path) -> Iterator[TestClient]:
    """A TestClient with an empty journal and a populated bar cache."""
    cache_path = tmp_path / "barcache.db"
    seed_cache(cache_path, _session_bars())
    ctx = configure(
        UISettings(
            journal_path=empty_journal_path,
            ivrank_path=tmp_path / "missing-ivrank.db",
            barcache_path=cache_path,
        )
    )
    with TestClient(app) as test_client:
        yield test_client
    ctx.close()


@pytest.fixture
def bare_client(tmp_path: Path) -> Iterator[TestClient]:
    """No journal file at all, no bars cached — the fresh-checkout state."""
    ctx = configure(
        UISettings(
            journal_path=tmp_path / "nope.db",
            ivrank_path=tmp_path / "nope-ivrank.db",
            barcache_path=tmp_path / "empty-barcache.db",
        )
    )
    with TestClient(app) as test_client:
        yield test_client
    ctx.close()


def _write_flow_rows(path: Path, count: int) -> None:
    """``count`` flow sessions with a hand-chosen volume ramp for the P90 line."""
    conn = init_db(path)
    for i in range(count):
        day = dt.date(2026, 8, 3) + dt.timedelta(days=i)
        append_flow_baseline(
            conn,
            FlowBaselineRow(
                date=day.isoformat(),
                underlying=SYMBOL,
                feed="indicative",
                is_delayed=True,
                session_spot=500.0 + i,
                deep_otm_threshold_pct=10.0,
                deep_otm_call_volume=1000.0 + 100 * i,
                deep_otm_put_volume=500.0 + 50 * i,
                deep_otm_total_volume=1500.0 + 150 * i,
                call_volume_by_distance={"1": 400.0 + 10 * i, "2": 600.0 + 90 * i},
                put_volume_by_distance={"1": 200.0 + 5 * i, "2": 300.0 + 45 * i},
                baseline_lookback_days=20,
                baseline_days=min(i, 20),
                strategy_version="v1.0.0+test",
                ts=f"{day.isoformat()}T21:00:00Z",
            ),
        )
    conn.close()


# ---------------------------------------------------------------------------
# 1. read-only by construction
# ---------------------------------------------------------------------------


def test_app_has_zero_non_get_routes() -> None:
    """The load-bearing assertion: the UI cannot write, because it has no route to."""
    allowed = {"GET", "HEAD", "OPTIONS"}
    offenders = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            # Mounts/Starlette primitives have no method set; they serve files, not
            # writes, and a static mount would still be read-only. There are none.
            offenders.append((getattr(route, "path", route), "no method set"))
            continue
        extra = set(methods) - allowed
        if extra:
            offenders.append((route.path, sorted(extra)))
    assert offenders == [], f"non-GET routes exist: {offenders}"


def test_every_api_route_is_get() -> None:
    paths = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")}
    assert paths == {
        "/api/health",
        "/api/bars",
        "/api/indicators",
        "/api/signals",
        "/api/arms",
        "/api/trades",
        "/api/huntplan",
        "/api/noshots",
        "/api/histogram",
        "/api/calibration",
        "/api/rollup",
    }


def test_post_to_an_api_path_is_rejected(client: TestClient) -> None:
    """Even a POST to a read path is a 405: the method does not exist on the route."""
    for path in ("/api/bars", "/api/trades", "/api/arms"):
        response = client.post(path, json={})
        assert response.status_code == 405, path


def test_ro_uri_carries_mode_ro(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    conn = init_db(path)
    conn.close()
    assert ro_uri(path).endswith("?mode=ro")
    assert ro_uri(path).startswith("file:")


def test_readonly_connection_refuses_writes(tmp_path: Path) -> None:
    """A write through the app's own connection factory raises, in SQLite itself."""
    path = tmp_path / "journal.db"
    conn = init_db(path)
    build_journal(conn)
    conn.close()

    ro = open_ro(path)
    assert ro is not None
    try:
        # Reads work.
        assert ro.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES ('x', '1', 'now')"
            )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("DELETE FROM decisions")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("UPDATE decisions SET reasoning = 'edited'")
    finally:
        ro.close()

    # And the file is genuinely untouched by the failed writes.
    check = sqlite3.connect(str(path))
    try:
        assert check.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0
        assert check.execute("SELECT COUNT(*) FROM meta WHERE key = 'x'").fetchone()[0] == 0
    finally:
        check.close()


def test_missing_database_opens_as_none(tmp_path: Path) -> None:
    assert open_ro(tmp_path / "does-not-exist.db") is None


# ---------------------------------------------------------------------------
# 2. indicators are the executor's indicators
# ---------------------------------------------------------------------------


def test_indicator_endpoint_is_identical_to_calling_executor_indicators(
    deep_client: TestClient,
) -> None:
    """Dogfooding, asserted value-for-value. A reimplementation would fail here."""
    response = deep_client.get("/api/bars", params={"tf": "15m", "from": "2026-09-15"})
    assert response.status_code == 200
    bars = response.json()["bars"]
    closes = [b["c"] for b in bars]
    assert len(closes) == 52, "two sessions of 15m is 26 candles each"
    assert len(closes) >= 50, "a 50-period EMA needs exactly this much to be defined"

    payload = deep_client.get(
        "/api/indicators", params={"tf": "15m", "from": "2026-09-15", "set": "ema50,rsi,macd,bb"}
    ).json()
    rows = payload["rows"]
    assert [r["c"] for r in rows] == closes
    assert [r["t"] for r in rows] == [b["t"] for b in bars]

    direct_ema = indicators.ema(closes, 50)
    direct_rsi = indicators.rsi(closes, 14)
    direct_macd = indicators.macd(closes, 12, 26, 9)
    direct_bb = indicators.bollinger(closes, 20, 2.0)

    assert [r["ema50"] for r in rows] == direct_ema
    assert [r["rsi"] for r in rows] == direct_rsi
    assert [r["macd"] for r in rows] == direct_macd.macd_line
    assert [r["macd_signal"] for r in rows] == direct_macd.signal_line
    assert [r["macd_histogram"] for r in rows] == direct_macd.histogram
    assert [r["bb_upper"] for r in rows] == direct_bb.upper
    assert [r["bb_lower"] for r in rows] == direct_bb.lower
    assert payload["latest"]["rsi"] == indicators.latest(direct_rsi)
    assert "executor.indicators" in payload["source"]


def test_indicator_endpoint_reports_insufficient_data_instead_of_crashing(
    client: TestClient,
) -> None:
    """A short series is a status, not an exception: 'not enough history' is normal.

    26 candles is the mixed case that matters — short enough to starve a 50- or
    200-period average, long enough for RSI(14) and Bollinger(20). Both halves of that
    have to come back as one 200, never as a crash or a silently truncated frame.
    """
    payload = client.get("/api/indicators", params={"tf": "15m", "from": "2026-09-15"}).json()
    assert payload["bar_count"] == 26
    assert payload["status"]["rsi"] == "ok"
    assert payload["status"]["bb"] == "ok"
    for starved in ("ema50", "ema200", "macd"):
        assert payload["status"][starved] == "insufficient", starved
    assert "at least 50" in payload["reason"]["ema50"]
    assert "at least 200" in payload["reason"]["ema200"]
    assert payload["latest"]["ema200"] is None
    assert all(row["ema200"] is None for row in payload["rows"])
    assert all(row["ema50"] is None for row in payload["rows"])
    # The series that ARE defined are still real numbers, not placeholders.
    assert payload["latest"]["rsi"] is not None
    assert payload["rows"][-1]["rsi"] == indicators.rsi([r["c"] for r in payload["rows"]], 14)[-1]


def test_indicator_endpoint_on_a_single_hour_candle_is_all_insufficient(
    empty_client: TestClient,
) -> None:
    payload = empty_client.get(
        "/api/indicators", params={"tf": "4h", "from": "2026-09-15"}
    ).json()
    assert payload["bar_count"] == 2  # 09:30-13:30 full, 13:30-16:00 partial
    assert set(payload["status"].values()) == {"insufficient"}
    assert payload["rows"][0]["rsi"] is None
    assert payload["latest"]["rsi"] is None
    assert set(payload["latest"]) == set(payload["series"])


def test_indicator_alignment_keeps_every_bar(empty_client: TestClient) -> None:
    """Aligned, not truncated: the newest bar is where a reader looks."""
    payload = empty_client.get(
        "/api/indicators", params={"tf": "30m", "from": "2026-09-15"}
    ).json()
    assert payload["bar_count"] == 13
    assert len(payload["rows"]) == payload["bar_count"]
    assert payload["rows"][-1]["t"] == empty_client.get(
        "/api/bars", params={"tf": "30m", "from": "2026-09-15"}
    ).json()["bars"][-1]["t"]
    assert payload["alignment"].startswith("aligned to input length")


def test_unknown_indicator_name_is_a_400(client: TestClient) -> None:
    response = client.get("/api/indicators", params={"tf": "15m", "set": "ema50,ema999"})
    assert response.status_code == 400
    assert "ema999" in response.json()["detail"]


def test_unknown_timeframe_is_a_422(client: TestClient) -> None:
    assert client.get("/api/bars", params={"tf": "3m"}).status_code == 422
    assert client.get("/api/bars", params={"tf": "15m", "from": "not-a-date"}).status_code == 422


# ---------------------------------------------------------------------------
# 3. bars
# ---------------------------------------------------------------------------


def test_bars_are_session_aware_and_report_exclusions(client: TestClient) -> None:
    payload = client.get("/api/bars", params={"tf": "1h", "from": "2026-09-15"}).json()
    assert payload["timeframe"] == "1h"
    assert payload["source_timeframe"] == "1Min"
    assert payload["count"] == 7
    assert payload["partial_buckets"] == 1
    assert payload["sessions"] == 1
    assert payload["excluded"] == {}
    assert payload["feed_provenance"]["worst"] == "sip"
    assert payload["feed_provenance"]["label"] == "SIP consolidated"
    first = payload["bars"][0]
    assert set(first) == {"t", "o", "h", "l", "c", "v", "feed"}
    assert first["feed"] == "sip"
    # 09:30 ET == 13:30Z on this date.
    assert first["t"] == "2026-09-15T13:30:00Z"


def test_bars_report_dropped_extended_hours_bars(tmp_path: Path) -> None:
    """Pre/post-market bars are excluded from candles and counted, not hidden."""
    cache_path = tmp_path / "barcache.db"
    pre = [
        AggBar(t=SESSION_OPEN - dt.timedelta(minutes=5 * i), o=1, h=1, l=1, c=1, v=1, feed="iex")
        for i in range(1, 6)
    ]
    seed_cache(cache_path, pre + _session_bars())
    ctx = configure(
        UISettings(
            journal_path=tmp_path / "nope.db",
            ivrank_path=tmp_path / "nope2.db",
            barcache_path=cache_path,
        )
    )
    try:
        with TestClient(app) as client:
            payload = client.get("/api/bars", params={"tf": "15m", "from": "2026-09-15"}).json()
        assert payload["count"] == 26
        assert payload["excluded"] == {"pre_market": 5}
        assert payload["excluded_total"] == 5
        # The mixed series reports the worse provenance honestly.
        assert payload["feed_provenance"]["by_bar"] == {"sip": 26}
    finally:
        ctx.close()


def test_bars_never_trigger_a_live_fetch_by_default(client: TestClient) -> None:
    """U1 ships cache-only; a request must not reach Alpaca on its own."""
    payload = client.get("/api/bars", params={"tf": "15m", "from": "2026-09-15"}).json()
    assert "cache-only" in payload["source"]


def test_fetch_flag_on_a_credential_free_host_still_serves_the_cache(
    client: TestClient,
) -> None:
    payload = client.get(
        "/api/bars", params={"tf": "15m", "from": "2026-09-15", "fetch": "true"}
    ).json()
    assert payload["status_code"] if False else payload["count"] == 26


def test_calendar_timeframes_are_served_from_daily_bars(tmp_path: Path) -> None:
    cache_path = tmp_path / "barcache.db"
    days = [dt.date(2026, 3, 2) + dt.timedelta(days=i) for i in range(14)]
    daily = [
        AggBar(
            t=dt.datetime(d.year, d.month, d.day, 20, 0, tzinfo=UTC),
            o=500.0 + i, h=502.0 + i, l=498.0 + i, c=501.0 + i, v=1_000_000.0, feed="sip",
        )
        for i, d in enumerate(days)
    ]
    seed_cache(cache_path, daily, timeframe="1Day")
    ctx = configure(
        UISettings(
            journal_path=tmp_path / "nope.db",
            ivrank_path=tmp_path / "nope2.db",
            barcache_path=cache_path,
        )
    )
    try:
        with TestClient(app) as client:
            weekly = client.get("/api/bars", params={"tf": "1w"}).json()
            monthly = client.get("/api/bars", params={"tf": "1M"}).json()
        assert weekly["count"] == 2
        assert weekly["source_timeframe"] == "1Day"
        assert monthly["count"] == 1
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 4. journal-backed endpoints
# ---------------------------------------------------------------------------


def test_health_reports_counts_and_read_only(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    # The canned journal has no IV store beside it, so health says "degraded" and
    # names the file. "ok" would be a claim the box cannot back up.
    assert payload["status"] == "degraded"
    assert payload["read_only"] is True
    assert payload["dbs"]["journal"]["mode"] == "ro"
    assert payload["dbs"]["journal"]["present"] is True
    assert payload["dbs"]["journal"]["decisions"] > 0
    assert payload["dbs"]["journal"]["noshots"] > 0
    assert payload["dbs"]["ivrank"]["present"] is False
    assert "no IV store" in payload["notes"]["ivrank"]
    assert payload["dbs"]["barcache"]["mode"].startswith("rw (cache")
    assert "15 minutes" in payload["feed_provenance"]["note"]
    assert payload["feed_provenance"]["live_client"] is False


def test_trades_paginates_searches_and_sorts(client: TestClient) -> None:
    first = client.get("/api/trades", params={"per_page": 3, "page": 1}).json()
    assert first["total"] > 3
    assert first["pages"] == -(-first["total"] // 3)
    assert len(first["rows"]) == 3
    assert first["order"] == "desc"
    assert first["rows"][0]["ts"] >= first["rows"][-1]["ts"]

    page_two = client.get("/api/trades", params={"per_page": 3, "page": 2}).json()
    assert {r["id"] for r in page_two["rows"]}.isdisjoint({r["id"] for r in first["rows"]})

    ascending = client.get(
        "/api/trades", params={"per_page": 100, "order": "asc", "sort": "ts"}
    ).json()
    assert ascending["rows"][0]["ts"] <= ascending["rows"][-1]["ts"]

    filtered = client.get("/api/trades", params={"arm": "C", "kind": "TRADE"}).json()
    assert filtered["rows"]
    assert {r["arm"] for r in filtered["rows"]} == {"C"}
    assert {r["kind"] for r in filtered["rows"]} == {"TRADE"}

    searched = client.get("/api/trades", params={"q": "time-stopped"}).json()
    assert searched["total"] >= 1
    assert all("time-stopped" in r["reasoning"].lower() for r in searched["rows"])


def test_trades_page_size_bounds_are_validated(client: TestClient) -> None:
    assert client.get("/api/trades", params={"per_page": 0}).status_code == 422
    assert client.get("/api/trades", params={"per_page": 10_000}).status_code == 422
    assert client.get("/api/trades", params={"page": 0}).status_code == 422
    assert client.get("/api/trades", params={"arm": "Z"}).status_code == 422
    assert client.get("/api/trades", params={"sort": "nope"}).status_code == 422


def test_arms_report_pnl_positions_and_equity_curve(client: TestClient) -> None:
    payload = client.get("/api/arms").json()
    arms = {card["arm"]: card for card in payload["arms"]}
    assert set(arms) == {"A", "B", "C", "EXCEPTION"}
    # The canned journal's hand-computed P&L (tests/fixtures_journal.py).
    assert arms["A"]["realized_pnl"] == 200.00
    assert arms["B"]["realized_pnl"] == -5.00
    assert arms["C"]["realized_pnl"] == 0.00
    assert arms["A"]["bankroll"] == 10_000.0
    assert arms["A"]["cash"] == 10_000.0  # nothing deployed in arm A
    assert len(arms["C"]["open_positions"]) == 1
    assert arms["C"]["open_positions"][0]["status"] == "OPEN"
    # The canned journal has no shadow-roll legs, so arm C's comparison card is
    # absent rather than fabricated as an empty curve with a made-up zero.
    assert arms["C"]["shadow_roll"] is None
    # A is the control, so A is its own baseline.
    assert arms["A"]["vs_control"] == 0.00
    assert payload["control_arm"] == "A"


def test_arms_surface_the_shadow_roll_comparison(tmp_path: Path) -> None:
    """The shadow sim shows up on arm C's card only, and never as a position."""
    from analysis import shadow_roll

    journal = tmp_path / "journal.db"
    conn = init_db(journal)
    build_journal(conn)
    leg = shadow_roll.open_leg(
        conn,
        opened_on=dt.date(2026, 3, 2),
        underlying_close=500.0,
        qty=2.0,
        strategy_version="v1.0.0+test",
    )
    # (510 - 500) * 100 * 2 contract-units = +2000.00, priced on the underlying.
    shadow_roll.mark_leg(conn, leg, date=dt.date(2026, 3, 13), underlying_close=510.0)
    conn.close()

    ctx = configure(
        UISettings(
            journal_path=journal,
            ivrank_path=tmp_path / "nope-ivrank.db",
            barcache_path=tmp_path / "barcache.db",
        )
    )
    try:
        with TestClient(app) as client:
            cards = {c["arm"]: c for c in client.get("/api/arms").json()["arms"]}
    finally:
        ctx.close()

    shadow = cards["C"]["shadow_roll"]
    assert shadow is not None
    assert shadow["total_pnl"] == 2000.0
    assert shadow["legs"][0]["marks"] == 1
    assert shadow["legs"][0]["opened_on"] == "2026-03-02"
    assert "no order exists" in shadow["note"]
    for other in ("A", "B", "EXCEPTION"):
        assert cards[other]["shadow_roll"] is None


def test_signals_carry_price_provenance_and_a_kind_filter(client: TestClient) -> None:
    payload = client.get("/api/signals", params={"from": "2026-03-01"}).json()
    assert payload["count"] > 0
    kinds = {row["kind"] for row in payload["rows"]}
    assert "NO_TRADE" not in kinds, "a plan-time veto is not a trade marker"
    assert {"TRADE"} <= kinds
    for row in payload["rows"]:
        assert row["journal_id"] > 0
        assert row["price"] is None or isinstance(row["price"], float)
        assert row["price_source"] in ("bar", "bar(1Day)", "none") or row[
            "price_source"
        ].startswith("snapshot.")

    only_trades = client.get("/api/signals", params={"kind": "TRADE", "arm": "B"}).json()
    assert {r["kind"] for r in only_trades["rows"]} == {"TRADE"}
    assert {r["arm"] for r in only_trades["rows"]} == {"B"}

    # An unknown csv value is refused before it reaches a query.
    assert client.get("/api/signals", params={"kind": "NOT_A_KIND"}).status_code == 400


def test_huntplan_reads_the_plan_back_out_of_the_journal(client: TestClient) -> None:
    """The plan is journaled decisions, so the endpoint returns the executor's own rows."""
    payload = client.get("/api/huntplan", params={"date": "2026-03-02"}).json()
    assert payload["date"] == "2026-03-02"
    assert payload["count"] > 0
    for cell in payload["cells"]:
        assert cell["kind"] in ("PROPOSAL", "NO_TRADE")
        assert cell["arm"] in ("A", "B", "C", "EXCEPTION")
        assert isinstance(cell["conditions"], dict)
        assert "journal decisions" in payload["source"]


def test_noshots_expose_failed_conditions_and_counterfactuals(client: TestClient) -> None:
    payload = client.get("/api/noshots", params={"from": "2026-03-01"}).json()
    assert payload["count"] > 0
    with_outcome = [r for r in payload["rows"] if r["has_outcome"]]
    assert with_outcome, "the canned ledger has tracked counterfactuals"
    for row in payload["rows"]:
        assert "T" in "".join(row["failed_conditions"].keys())
    assert payload["counterfactual"] is not None
    assert "may be filled in" in payload["outcome_note"]


def test_histogram_weekly_groups_and_totals_match_the_rollup(client: TestClient) -> None:
    payload = client.get("/api/histogram", params={"veto": "weekly"}).json()
    assert payload["mode"] == "weekly"
    assert len(payload["weeks"]) >= 2
    assert all(w["week_start"] <= w["week_end"] for w in payload["weeks"])
    assert payload["totals"]["sufficient"] is True
    # T3a/T3b collapse to one T3 count per row — the rollup's grouping, not ours.
    assert "T3" in payload["totals"]["by_condition"]

    total = client.get("/api/histogram", params={"veto": "total"}).json()
    assert total["mode"] == "total"
    assert total["totals"]["by_condition"] == payload["totals"]["by_condition"]


def test_rollup_returns_the_weekly_markdown(client: TestClient) -> None:
    payload = client.get("/api/rollup", params={"week": "2026-03-13"}).json()
    assert payload["week"] == "2026-03-13"
    assert payload["window"] == {"from": "2026-03-07", "to": "2026-03-13"}
    assert "#" in payload["markdown"]
    assert "analysis.rollup.weekly_rollup_text" in payload["source"]


def test_calibration_reports_flow_p90_and_pending_values(tmp_path: Path) -> None:
    journal = tmp_path / "journal.db"
    _write_flow_rows(journal, 20)
    cache_path = tmp_path / "barcache.db"
    ctx = configure(
        UISettings(
            journal_path=journal,
            ivrank_path=tmp_path / "nope-ivrank.db",
            barcache_path=cache_path,
        )
    )
    try:
        with TestClient(app) as client:
            payload = client.get("/api/calibration").json()
    finally:
        ctx.close()

    assert payload["flow_sessions"] == 20
    assert payload["sufficient"] is True
    # Sessions 0..19 -> total volume 1500 + 150i, so the top of the series is 4350
    # and the interpolated P90 sits at index 0.9 * 19 = 17.1 -> 1500 + 150*17.1.
    assert payload["p90"]["value"] == pytest.approx(1500.0 + 150 * 17.1)
    assert payload["p90"]["status"] == "defined"
    assert payload["p90"]["sessions_needed"] == 20
    # The 1pp distance buckets the soak stores are summed for the threshold histogram.
    assert payload["distance_histogram"]["call"]["1"] == pytest.approx(
        sum(400.0 + 10 * i for i in range(20))
    )
    assert payload["pending_calibrations"], "the shipped rulebook has uncalibrated gates"
    assert payload["strategy_version"], "the example rulebook still fingerprints"
    assert payload["ivrank"]["count"] == 0


def test_calibration_p90_is_labelled_forming_before_the_baseline_is_full(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal.db"
    _write_flow_rows(journal, 5)
    ctx = configure(
        UISettings(
            journal_path=journal,
            ivrank_path=tmp_path / "nope-ivrank.db",
            barcache_path=tmp_path / "barcache.db",
        )
    )
    try:
        with TestClient(app) as client:
            payload = client.get("/api/calibration").json()
    finally:
        ctx.close()
    assert payload["p90"]["status"] == "forming"
    assert payload["p90"]["sessions"] == 5
    assert payload["p90"]["sessions_needed"] == 20


# ---------------------------------------------------------------------------
# 5. honest empty states
# ---------------------------------------------------------------------------

ALL_ENDPOINTS = [
    "/api/health",
    "/api/bars",
    "/api/indicators",
    "/api/signals",
    "/api/arms",
    "/api/trades",
    "/api/huntplan",
    "/api/noshots",
    "/api/histogram",
    "/api/calibration",
    "/api/rollup",
]


def test_every_endpoint_answers_200_with_an_empty_journal(empty_client: TestClient) -> None:
    """Before the window opens, every page renders. None of them is a 500."""
    for path in ALL_ENDPOINTS:
        response = empty_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert isinstance(response.json(), dict), path


def test_empty_journal_shapes_are_explicit(empty_client: TestClient) -> None:
    trades = empty_client.get("/api/trades").json()
    assert trades["rows"] == [] and trades["total"] == 0 and trades["pages"] == 0

    arms = empty_client.get("/api/arms").json()
    assert arms["sufficient"] is False
    assert arms["reason"]
    for card in arms["arms"]:
        assert card["bankroll"] is None
        assert card["realized_pnl"] == 0.0
        assert card["open_positions"] == []

    histogram = empty_client.get("/api/histogram").json()
    assert histogram["weeks"] == []
    assert histogram["totals"] is None

    rollup = empty_client.get("/api/rollup", params={"week": "2026-10-02"}).json()
    assert isinstance(rollup["markdown"], str)

    calibration = empty_client.get("/api/calibration").json()
    assert calibration["sufficient"] is False
    assert "no flow_baseline rows" in calibration["reason"]
    assert calibration["p90"]["status"] == "no_data"
    assert calibration["p90"]["value"] is None

    noshots = empty_client.get("/api/noshots").json()
    assert noshots["rows"] == []
    # The counterfactual delta is computed, not skipped — it just says so.
    assert noshots["counterfactual"]["sufficient"] is False
    assert "counterfactual_outcome" in noshots["counterfactual"]["reason"]

    plan = empty_client.get("/api/huntplan", params={"date": "2026-10-02"}).json()
    assert plan["cells"] == [] and plan["count"] == 0

    signals = empty_client.get("/api/signals").json()
    assert signals["rows"] == []


def test_no_database_files_at_all_is_degraded_not_500(bare_client: TestClient) -> None:
    for path in ALL_ENDPOINTS:
        assert bare_client.get(path).status_code == 200, path
    health = bare_client.get("/api/health").json()
    assert health["status"] == "degraded"
    assert health["dbs"]["journal"]["present"] is False
    assert "window has not opened" in health["notes"]["journal"]
    assert bare_client.get("/api/bars").json()["count"] == 0


# ---------------------------------------------------------------------------
# 6. the static surface
# ---------------------------------------------------------------------------


def test_index_serves_the_terminal_shell(client: TestClient) -> None:
    """U2 replaced the placeholder page; the shell is what ships now.

    (``tests/test_ui_static.py`` owns the deep static/vendoring assertions. This one
    stays here because it is the same "/" the API tests have been asserting on since
    U1, and a page that stops rendering is a page the API tests would not notice.)
    """
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "paper-hunter terminal" in response.text
    assert 'src="app.js"' in response.text
    assert 'id="page-terminal"' in response.text
    for page in ("terminal", "arms", "ledger", "hunt", "calibration"):
        assert f'data-page="{page}"' in response.text, page


def test_openapi_schema_generates(client: TestClient) -> None:
    """A schema that fails to generate would mean the app cannot even be documented."""
    schema = client.get("/openapi.json").json()
    for path in ALL_ENDPOINTS:
        assert path in schema["paths"], path
        assert set(schema["paths"][path]) == {"get"}, path


# ---------------------------------------------------------------------------
# 7. the transport seam is still the only way out
# ---------------------------------------------------------------------------


def test_cache_client_is_only_built_when_fetch_is_allowed(tmp_path: Path) -> None:
    """A context with allow_fetch=False never constructs an Alpaca client."""
    transport = MockTransport({stock_bars_path(SYMBOL): {"bars": [], "symbol": SYMBOL}})
    ctx = configure(
        UISettings(journal_path=tmp_path / "n.db", barcache_path=tmp_path / "bc.db")
    )
    try:
        assert ctx.live_barcache() is None
        assert ctx.barcache.client is None
        assert transport.calls == []
    finally:
        ctx.close()


def test_bars_endpoint_never_uses_the_network_under_test(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: a socket during an API test fails the test, loudly."""

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the UI API opened a socket during an offline test")

    monkeypatch.setattr("socket.socket", explode)
    for path in ALL_ENDPOINTS:
        assert client.get(path).status_code == 200, path
