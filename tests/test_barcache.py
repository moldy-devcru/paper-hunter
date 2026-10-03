"""Bar cache: cache-first reads, gap-only fetching, provenance, rate limiting.

Every test here is offline. The transport is ``executor.alpaca_client.MockTransport``,
which records each call, so "did we re-pull history we already hold?" is a question
about a list length rather than about a bill. Nothing in this file opens a socket.

The transport is also *strict* by default: it enforces Alpaca's documented query
contract per route, so a cache that invented a parameter would fail here rather than
with a live HTTP 400.
"""

from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytest

from executor.alpaca_client import (
    AlpacaClient,
    MockTransport,
    RateLimiter,
    stock_bars_path,
)
from ui.aggregate import AggBar
from ui.barcache import BarCache

UTC = dt.UTC
ET = "America/New_York"
# 2026-09-15 is an ordinary Tuesday: 09:30 ET == 13:30Z (EDT).
SESSION_OPEN = dt.datetime(2026, 9, 15, 13, 30, tzinfo=UTC)
SESSION_END = dt.datetime(2026, 9, 15, 20, 0, tzinfo=UTC)
SYMBOL = "SPY"


def payload(start: dt.datetime, count: int, *, timeframe: str = "1Min") -> dict[str, Any]:
    """A doc-shaped single-symbol bars envelope: ``{"bars": [...]}``, a flat list."""
    minutes = 1 if timeframe == "1Min" else 1440
    bars = []
    for i in range(count):
        stamp = start + dt.timedelta(minutes=minutes * i)
        base = 500.0 + i
        bars.append(
            {
                "t": stamp.isoformat().replace("+00:00", "Z"),
                "o": base,
                "h": base + 1,
                "l": base - 1,
                "c": base + 0.5,
                "v": 1000 + i,
                "n": 10,
                "vw": base + 0.25,
            }
        )
    return {"symbol": SYMBOL, "bars": bars, "next_page_token": None}


def make_client(
    payload_for: dict[str, Any], *, limiter: RateLimiter | None = None
) -> tuple[AlpacaClient, MockTransport]:
    transport = MockTransport({stock_bars_path(SYMBOL): payload_for})
    client = AlpacaClient(
        transport=transport,
        key="test-key",
        secret="test-secret",
        rate_limiter=limiter,
    )
    return client, transport


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "barcache.db"


# ---------------------------------------------------------------------------
# cache-first
# ---------------------------------------------------------------------------


def test_second_call_hits_cache_and_makes_no_request(cache_path: Path) -> None:
    """The headline property: a repeated read issues zero HTTP requests."""
    client, transport = make_client(payload(SESSION_OPEN, 390))
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        first = cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)
        assert len(transport.calls) == 1
        assert len(first) == 390

        second = cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)
        assert len(transport.calls) == 1, "re-pulled history we already hold"
        assert [b.t for b in second] == [b.t for b in first]
        assert cache.fetch_calls == 1


def test_gap_fetch_requests_only_the_gap(cache_path: Path) -> None:
    """The second request starts one step past the newest cached bar."""
    first_payload = payload(SESSION_OPEN, 100)
    second_payload = payload(SESSION_OPEN + dt.timedelta(minutes=100), 20)

    def answer(params: dict[str, Any]) -> dict[str, Any]:
        # Answer the full-window request with 100 bars and the follow-up gap request
        # with 20 more, so a correct implementation has to stitch them back together.
        return first_payload if params["start"] == "2026-09-15T13:30:00Z" else second_payload

    transport = MockTransport({stock_bars_path(SYMBOL): answer})
    client = AlpacaClient(transport=transport, key="k", secret="s")
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)
        merged = cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)

    assert len(transport.calls) == 2
    assert transport.calls[0][1]["start"] == "2026-09-15T13:30:00Z"
    # One step past the newest cached bar (13:30 + 100 minutes).
    assert transport.calls[1][1]["start"] == "2026-09-15T15:10:00Z"
    assert len(merged) == 120


def test_cache_only_mode_makes_no_request(cache_path: Path) -> None:
    """No client means cache-only, which is the normal state on a keyless box."""
    with BarCache(cache_path, client=None) as cache:
        assert cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END) == []
        assert cache.row_count() == 0
        assert cache.exists


def test_fetch_false_is_a_pure_cache_read(cache_path: Path) -> None:
    client, transport = make_client(payload(SESSION_OPEN, 10))
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        assert cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END, fetch=False) == []
    assert transport.calls == []


def test_sip_window_is_clamped_to_fifteen_minutes_old(cache_path: Path) -> None:
    """The free tier rejects a SIP ``end`` younger than 15 minutes; we do not ask."""
    client, transport = make_client(payload(SESSION_OPEN, 5))
    now = dt.datetime(2026, 9, 15, 21, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=now)
    params = transport.calls[0][1]
    assert params["feed"] == "sip"
    assert params["end"] == "2026-09-15T20:45:00Z"  # now - 15m exactly
    assert params["timeframe"] == "1Min"


def test_iex_feed_is_not_clamped(cache_path: Path) -> None:
    """IEX is the realtime feed; the 15-minute SIP floor does not apply to it."""
    client, transport = make_client(payload(SESSION_OPEN, 5))
    now = dt.datetime(2026, 9, 15, 21, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=now, feed="iex")
    assert transport.calls[0][1]["end"] == "2026-09-15T21:00:00Z"
    assert transport.calls[0][1]["feed"] == "iex"


def test_rate_limiter_is_honoured_across_fetches(cache_path: Path) -> None:
    """The cache does not build its own budget — it goes through the client's limiter."""
    limiter = RateLimiter(max_calls=5, window_seconds=60.0, sleep=lambda _s: None,
                          clock=lambda: 0.0)
    client, _ = make_client(payload(SESSION_OPEN, 10), limiter=limiter)
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)
        cache.bars(SYMBOL, "1Day", start=dt.datetime(2026, 1, 1, tzinfo=UTC),
                   end=dt.datetime(2026, 9, 1, tzinfo=UTC))
    # Two fetches, two limiter acquisitions. (_stamps is the limiter's own bookkeeping;
    # there is no public counter, and adding one for a test would be worse.)
    assert cache.fetch_calls == 2
    assert len(limiter._stamps) == 2


# ---------------------------------------------------------------------------
# daily bars
# ---------------------------------------------------------------------------


def test_daily_bars_round_trip_with_a_day_step(cache_path: Path) -> None:
    day_one = dt.datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    client, transport = make_client(payload(day_one, 10, timeframe="1Day"), limiter=None)
    now = dt.datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        first = cache.bars(SYMBOL, "1Day", start=day_one, end=dt.datetime(2026, 9, 11, tzinfo=UTC))
        assert len(first) == 10
        assert transport.calls[0][1]["timeframe"] == "1Day"
        # The day step, not the minute step, drives the gap.
        cache.bars(SYMBOL, "1Day", start=day_one, end=dt.datetime(2026, 9, 11, tzinfo=UTC))
        assert len(transport.calls) == 1


def test_unknown_timeframe_is_refused_by_the_cache(cache_path: Path) -> None:
    """Derived timeframes are aggregated, never cached."""
    with BarCache(cache_path, client=None) as cache:
        with pytest.raises(ValueError, match="barcache stores"):
            cache.bars(SYMBOL, "15Min", start=SESSION_OPEN, end=SESSION_END)


# ---------------------------------------------------------------------------
# storage, provenance, diagnostics
# ---------------------------------------------------------------------------


def test_put_upserts_and_upgrades_provenance(cache_path: Path) -> None:
    """A bar that becomes consolidated replaces its IEX row instead of duplicating it."""
    stamp = SESSION_OPEN
    with BarCache(cache_path, client=None) as cache:
        from executor.alpaca_client import Bar

        cache.put(SYMBOL, "1Min", [Bar(t=stamp, o=1, h=2, l=0.5, c=1.5, v=10)], feed="iex")
        assert cache.row_count(symbol=SYMBOL) == 1
        assert cache.stored(SYMBOL, "1Min")[0].feed == "iex"

        cache.put(SYMBOL, "1Min", [Bar(t=stamp, o=1, h=2, l=0.5, c=1.6, v=11)], feed="sip")
        assert cache.row_count(symbol=SYMBOL) == 1, "provenance upgrade created a second row"
        upgraded = cache.stored(SYMBOL, "1Min")[0]
        assert upgraded.feed == "sip"
        assert upgraded.c == 1.6
        assert upgraded.v == 11


def test_windows_are_clipped_by_the_stored_bounds(cache_path: Path) -> None:
    client, _ = make_client(payload(SESSION_OPEN, 390))
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)
        narrow = cache.stored(
            SYMBOL, "1Min", start=SESSION_OPEN + dt.timedelta(minutes=10),
            end=SESSION_OPEN + dt.timedelta(minutes=19),
        )
    assert len(narrow) == 10
    assert narrow[0].t == SESSION_OPEN + dt.timedelta(minutes=10)


def test_provenance_summary_and_fetch_log(cache_path: Path) -> None:
    client, _ = make_client(payload(SESSION_OPEN, 30))
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)
        summary = cache.provenance_summary()
        log = cache.fetch_log()

    assert summary["series"][0]["symbol"] == SYMBOL
    assert summary["series"][0]["feeds"] == {"sip": 30}
    assert summary["series"][0]["first"] == "2026-09-15T13:30:00Z"
    assert summary["series"][0]["last"] == "2026-09-15T13:59:00Z"
    assert len(log) == 1
    assert json.loads(json.dumps(log[0]))["bars"] == 30


def test_last_cached_and_row_count_are_per_series(cache_path: Path) -> None:
    client, _ = make_client(payload(SESSION_OPEN, 5))
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    with BarCache(cache_path, client=client, now=lambda: now) as cache:
        assert cache.last_cached(SYMBOL, "1Min") is None
        cache.bars(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END)
        assert cache.last_cached(SYMBOL, "1Min") == SESSION_OPEN + dt.timedelta(minutes=4)
        assert cache.row_count(symbol=SYMBOL) == 5
        assert cache.row_count(symbol="QQQ") == 0


# ---------------------------------------------------------------------------
# thread safety — the shared connection is a server-wide object
# ---------------------------------------------------------------------------


def test_concurrent_reads_return_the_whole_row_set(cache_path: Path) -> None:
    """One connection, many worker threads, no corruption.

    Found by the U2 browser smoke, not by reasoning: the terminal polls every 45s, a
    second browser tab doubles that, and uvicorn runs these sync endpoints on a thread
    pool. A sqlite3 ``Connection`` is not safe for concurrent use even when the C
    library is SERIALIZED (this one is — ``sqlite3.threadsafety == 3``): SERIALIZED
    guards the database handle, while CPython's Connection object still shares one
    statement cache across threads, so interleaved ``execute()``/``fetchall()`` hands
    a reader another thread's half-stepped rows. Measured before the fix: 24 HTTP 500s
    per 1200 concurrent requests, and short reads (138 rows where 300 were stored).

    Reads alone are enough to break it, so this test interleaves readers with a writer
    (the fetch log) and asserts every single read is complete. If this ever fails, the
    fix is the lock in ``BarCache._db`` — do not "optimize" it away.
    """
    count = 300
    with BarCache(cache_path, client=None) as cache:
        cache.put(
            SYMBOL,
            "1Min",
            [
                AggBar(
                    t=SESSION_OPEN + dt.timedelta(minutes=i),
                    o=1.0,
                    h=2.0,
                    l=0.5,
                    c=1.5,
                    v=100.0,
                    n=1,
                    vw=1.0,
                    feed="sip",
                )
                for i in range(count)
            ],
            feed="sip",
        )

        def read() -> int:
            return len(cache.stored(SYMBOL, "1Min", start=SESSION_OPEN, end=SESSION_END))

        def write() -> None:
            cache._log_fetch(SYMBOL, "1Min", "sip", SESSION_OPEN, SESSION_END, count)

        with ThreadPoolExecutor(max_workers=16) as pool:
            reads = [pool.submit(read) for _ in range(400)]
            writes = [pool.submit(write) for _ in range(100)]
            for future in as_completed(writes):
                future.result()  # a writer that raised is a failure too
            lengths = [f.result() for f in as_completed(reads)]

    assert all(n == count for n in lengths), (
        f"short/corrupt reads under concurrency: {sorted({n for n in lengths if n != count})[:5]}"
    )
