"""Aggregation math — the algo-adjacent part of Phase U1.

Every expected value in this file is hand-computed in the comment above it, not
re-derived by the same code under test. A bucketing bug that made the test agree with
itself would be the worst possible outcome here, because the whole point of the UI is
that mads's screen and the executor's arithmetic agree.

Fixtures are built in UTC and read back in ET on purpose: constructing the bars with
``tzinfo=MARKET_TZ`` throughout would hide exactly the class of bug the DST tests
exist to catch.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from ui.aggregate import (
    AggBar,
    aggregate,
    aggregate_daily,
    aggregate_intraday,
    buckets_per_session,
    calendar_bucket_start,
    merge_bars,
    session_close_time,
    session_for,
    worse_feed,
)

UTC = dt.UTC
ET = ZoneInfo("America/New_York")

# A full regular session is 09:30-16:00 ET = 390 one-minute bars.
SESSION_MINUTES = 390
FULL_SESSION_BARS = 390


def minute_bars(
    start_utc: dt.datetime,
    count: int,
    *,
    feed: str = "sip",
    price0: float = 100.0,
) -> list[AggBar]:
    """``count`` one-minute bars starting at ``start_utc``, with a legible price ramp.

    Bar ``i`` is ``o=price0+i, h=+0.5, l=-0.5, c=+0.25, v=10`` so that a merged
    bucket's four prices are checkable in a comment without a spreadsheet.
    """
    out = []
    for i in range(count):
        base = price0 + i
        out.append(
            AggBar(
                t=start_utc + dt.timedelta(minutes=i),
                o=base,
                h=base + 0.5,
                l=base - 0.5,
                c=base + 0.25,
                v=10.0,
                n=1,
                vw=base,
                feed=feed,
            )
        )
    return out


def session_start_utc(day: dt.date, hour: int = 9, minute: int = 30) -> dt.datetime:
    """The 09:30 ET open of ``day`` as a UTC instant."""
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).astimezone(UTC)


# ---------------------------------------------------------------------------
# merge semantics
# ---------------------------------------------------------------------------


def test_merge_is_exact_ohlcv() -> None:
    """open=first, high=max, low=min, close=last, volume=sum, t=bucket start.

    Bars: (10, 12, 9, 11, v=100), (11, 15, 10.5, 14, v=50), (14, 16, 13, 13.5, v=25)
      open  = 10        (first open, NOT the max and NOT the close)
      high  = 16        (max of 12, 15, 16)
      low   = 9         (min of 9, 10.5, 13)
      close = 13.5      (last close)
      volume= 175       (100 + 50 + 25)
    """
    start = dt.datetime(2026, 9, 15, 13, 30, tzinfo=UTC)
    bars = [
        AggBar(t=start, o=10.0, h=12.0, l=9.0, c=11.0, v=100.0, n=3, vw=10.2, feed="sip"),
        AggBar(t=start + dt.timedelta(minutes=1), o=11.0, h=15.0, l=10.5, c=14.0, v=50.0,
               n=2, vw=11.4, feed="sip"),
        AggBar(t=start + dt.timedelta(minutes=2), o=14.0, h=16.0, l=13.0, c=13.5, v=25.0,
               n=1, vw=14.1, feed="sip"),
    ]
    merged = merge_bars(bars, start)
    assert merged.t == start
    assert merged.o == 10.0
    assert merged.h == 16.0
    assert merged.l == 9.0
    assert merged.c == 13.5
    assert merged.v == 175.0
    assert merged.n == 6


def test_merge_of_one_bar_is_that_bar() -> None:
    start = dt.datetime(2026, 9, 15, 13, 30, tzinfo=UTC)
    only = AggBar(t=start, o=1.0, h=2.0, l=0.5, c=1.5, v=7.0, feed="sip")
    assert merge_bars([only], start) == only


def test_merge_of_nothing_raises() -> None:
    with pytest.raises(ValueError):
        merge_bars([], dt.datetime(2026, 9, 15, tzinfo=UTC))


def test_bucket_feed_is_the_worst_in_it() -> None:
    """A bucket mixing consolidated history with an IEX tail is an IEX bucket."""
    start = dt.datetime(2026, 9, 15, 19, 30, tzinfo=UTC)
    old = AggBar(t=start, o=1, h=1, l=1, c=1, v=1, feed="sip")
    fresh = AggBar(t=start + dt.timedelta(minutes=1), o=1, h=1, l=1, c=1, v=1, feed="iex")
    assert merge_bars([old, fresh], start).feed == "iex"
    assert merge_bars([fresh, old], start).feed == "iex"
    assert worse_feed("sip", "iex") == "iex"
    assert worse_feed("iex", "sip") == "iex"
    assert worse_feed("sip", "sip") == "sip"
    # An unrecognised feed never launders itself into a consolidated label.
    assert worse_feed("sip", "something-new") == "something-new"


# ---------------------------------------------------------------------------
# 15m — the default chart timeframe
# ---------------------------------------------------------------------------


def test_15m_buckets_are_exact_and_counted() -> None:
    """390 minutes / 15 = 26 buckets; the first covers 09:30-09:45 (15 bars)."""
    bars = minute_bars(session_start_utc(dt.date(2026, 9, 15)), FULL_SESSION_BARS)
    result = aggregate_intraday(bars, "15m")

    assert len(result.bars) == 26
    assert result.sessions == 1
    assert result.excluded == {}
    # 15 divides 390 exactly, so no bucket in a full session is short.
    assert result.partial_buckets == 0

    first = result.bars[0]
    assert first.t == session_start_utc(dt.date(2026, 9, 15))
    # Bars 0..14 -> o=100+i, h=+0.5, l=-0.5, c=+0.25, v=10 each
    assert first.o == 100.0
    assert first.h == 114.5   # max(100+14+0.5)
    assert first.l == 99.5    # min(100+0-0.5)
    assert first.c == 114.25  # last close: 100+14+0.25
    assert first.v == 150.0

    last = result.bars[-1]
    # 15:45-16:00 is bars 375..389
    assert last.o == 100.0 + 375
    assert last.c == 100.0 + 389 + 0.25
    assert last.v == 150.0
    assert last.t == session_start_utc(dt.date(2026, 9, 15)) + dt.timedelta(minutes=375)


def test_1h_buckets_anchor_at_the_open_and_never_span_it() -> None:
    """390 / 60 = 6 full hours plus a 30-minute remainder: seven buckets."""
    bars = minute_bars(session_start_utc(dt.date(2026, 9, 15)), FULL_SESSION_BARS)
    result = aggregate_intraday(bars, "1h")

    assert len(result.bars) == 7
    assert result.partial_buckets == 1  # only 15:30-16:00 is short
    starts = [b.t.astimezone(ET).strftime("%H:%M") for b in result.bars]
    assert starts == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    # A bucket must never run past the close.
    for bar in result.bars:
        assert bar.t.astimezone(ET).date() == dt.date(2026, 9, 15)
        assert bar.t.astimezone(ET).time() >= dt.time(9, 30)


def test_4h_anchors_at_the_open_with_a_partial_last_bucket() -> None:
    """09:30-13:30 then 13:30-16:00 (150 minutes) — the documented 4h case."""
    bars = minute_bars(session_start_utc(dt.date(2026, 9, 15)), FULL_SESSION_BARS)
    result = aggregate_intraday(bars, "4h")

    assert len(result.bars) == 2
    assert [b.t.astimezone(ET).strftime("%H:%M") for b in result.bars] == ["09:30", "13:30"]
    assert result.partial_buckets == 1
    assert result.bars[0].o == 100.0
    assert result.bars[0].c == 100.0 + 239 + 0.25  # bars 0..239 (09:30 -> 13:29)
    assert result.bars[1].o == 100.0 + 240
    assert result.bars[1].c == 100.0 + 389 + 0.25


def test_5m_divides_the_session_exactly() -> None:
    bars = minute_bars(session_start_utc(dt.date(2026, 9, 15)), FULL_SESSION_BARS)
    result = aggregate_intraday(bars, "5m")
    assert len(result.bars) == 78  # 390 / 5
    assert result.partial_buckets == 0
    assert buckets_per_session("5m") == 78
    assert buckets_per_session("1h") == 7  # ceiling: 6.5 -> 7
    assert buckets_per_session("4h") == 2


def test_two_sessions_never_merge_into_one_bucket() -> None:
    """Consecutive days are bucketed independently — the boundary is a session edge."""
    day_one = minute_bars(session_start_utc(dt.date(2026, 9, 15)), FULL_SESSION_BARS)
    day_two = minute_bars(session_start_utc(dt.date(2026, 9, 16)), FULL_SESSION_BARS)
    result = aggregate_intraday(day_one + day_two, "1h")

    assert result.sessions == 2
    assert len(result.bars) == 14
    days = {b.t.astimezone(ET).date() for b in result.bars}
    assert days == {dt.date(2026, 9, 15), dt.date(2026, 9, 16)}


# ---------------------------------------------------------------------------
# extended hours
# ---------------------------------------------------------------------------


def test_pre_and_post_market_bars_are_excluded_and_counted() -> None:
    """Extended-hours bars never enter a bucket, and are never silently dropped."""
    open_utc = session_start_utc(dt.date(2026, 9, 15))
    regular = minute_bars(open_utc, FULL_SESSION_BARS)
    pre = minute_bars(open_utc - dt.timedelta(minutes=30), 30, price0=90.0)
    post = minute_bars(open_utc + dt.timedelta(minutes=FULL_SESSION_BARS), 30, price0=200.0)

    result = aggregate_intraday(pre + regular + post, "15m")

    assert len(result.bars) == 26
    assert result.excluded == {"pre_market": 30, "post_market": 30}
    assert result.excluded_total == 60
    # None of the extended-hours prices reached a candle.
    assert min(b.l for b in result.bars) >= 99.5
    assert max(b.h for b in result.bars) <= 489.5


# ---------------------------------------------------------------------------
# DST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected_open_utc"),
    [
        # Spring forward 2026-03-08: the US moves 02:00 EST -> 03:00 EDT, so the
        # 09:30 ET open is 13:30Z (UTC-4). A hardcoded -05:00 would read 13:30Z as
        # 08:30 ET and throw the whole session into pre-market.
        (dt.date(2026, 3, 8), dt.datetime(2026, 3, 8, 13, 30, tzinfo=UTC)),
        # Fall back 2026-11-01: 09:30 ET is 14:30Z (UTC-5, EST). A hardcoded -04:00
        # would read 14:30Z as 10:30 ET and drop the first hour of the session.
        (dt.date(2026, 11, 1), dt.datetime(2026, 11, 1, 14, 30, tzinfo=UTC)),
    ],
)
def test_dst_transition_day_buckets_correctly(
    day: dt.date, expected_open_utc: dt.datetime
) -> None:
    """Both 2026 transition days bucket a full session into 78 five-minute candles."""
    # The offset really is the one we claim, or the rest of the test is theatre.
    local_open = expected_open_utc.astimezone(ET)
    assert local_open.date() == day
    assert (local_open.hour, local_open.minute) == (9, 30)

    bars = minute_bars(expected_open_utc, FULL_SESSION_BARS)
    result = aggregate_intraday(bars, "5m")

    assert result.excluded == {}
    assert len(result.bars) == 78
    assert result.partial_buckets == 0
    assert result.bars[0].t == expected_open_utc
    assert result.bars[-1].t == expected_open_utc + dt.timedelta(minutes=385)
    # Every bar really landed inside the ET session.
    last_local = result.bars[-1].t.astimezone(ET)
    assert last_local.date() == day
    assert last_local.time() == dt.time(15, 55)


@pytest.mark.parametrize(
    ("day", "stamp_utc", "week_start"),
    [
        (dt.date(2026, 3, 8), dt.datetime(2026, 3, 8, 14, 30, tzinfo=UTC),
         dt.date(2026, 3, 2)),
        (dt.date(2026, 11, 1), dt.datetime(2026, 11, 1, 14, 30, tzinfo=UTC),
         dt.date(2026, 10, 26)),
    ],
)
def test_dst_day_daily_equivalents_are_unaffected(
    day: dt.date, stamp_utc: dt.datetime, week_start: dt.date
) -> None:
    """Calendar bucketing keys on the ET date, which is why it survives a transition."""
    bar = AggBar(t=stamp_utc, o=1, h=1, l=1, c=1, v=1, feed="sip")
    result = aggregate_daily([bar], "1w")
    assert len(result.bars) == 1
    # Both days are Sundays, so they belong to the ISO week that began the PREVIOUS
    # Monday (an ISO week runs Monday..Sunday; it does not start on the Sunday).
    assert day.strftime("%A") == "Sunday"
    assert result.bars[0].t.astimezone(ET).date() == week_start


# ---------------------------------------------------------------------------
# holidays and half sessions
# ---------------------------------------------------------------------------


def test_full_holiday_produces_no_buckets() -> None:
    """2026-12-25 is a known closure: the bars exist, the buckets do not."""
    bars = minute_bars(session_start_utc(dt.date(2026, 12, 25)), FULL_SESSION_BARS)
    result = aggregate_intraday(bars, "15m")
    assert result.bars == []
    assert result.excluded == {"holiday": FULL_SESSION_BARS}
    assert result.sessions == 0


def test_half_session_closes_at_1300() -> None:
    """Day after Thanksgiving 2026 (Fri Nov 27): a 210-minute session."""
    assert session_close_time(dt.date(2026, 11, 27)) == dt.time(13, 0)
    session = session_for(dt.date(2026, 11, 27))
    assert session is not None
    assert session.minutes == 210

    bars = minute_bars(session_start_utc(dt.date(2026, 11, 27)), 210)
    result = aggregate_intraday(bars, "15m")
    # 210 / 15 = 14 exact buckets; nothing is short on this particular day.
    assert len(result.bars) == 14
    assert result.partial_buckets == 0
    assert result.bars[-1].t.astimezone(ET).strftime("%H:%M") == "12:45"

    hourly = aggregate_intraday(bars, "1h")
    # 09:30, 10:30, 11:30, 12:30 — and the last is cut at the 13:00 early close.
    assert [b.t.astimezone(ET).strftime("%H:%M") for b in hourly.bars] == [
        "09:30",
        "10:30",
        "11:30",
        "12:30",
    ]
    assert hourly.partial_buckets == 1

    four_hourly = aggregate_intraday(bars, "4h")
    assert len(four_hourly.bars) == 1
    assert four_hourly.partial_buckets == 1


def test_unknown_year_defaults_to_a_full_session() -> None:
    """A date outside the known tables is a normal session, not a guessed closure.

    Inventing a holiday that did not happen would hide real bars; failing to know
    about one merely yields an empty series for that date, which is honest.
    """
    assert session_close_time(dt.date(2031, 11, 28)) == dt.time(16, 0)
    assert session_for(dt.date(2031, 11, 28)) is not None


# ---------------------------------------------------------------------------
# calendar buckets
# ---------------------------------------------------------------------------


def daily_bars(days: list[dt.date], *, price0: float = 500.0) -> list[AggBar]:
    """Daily bars stamped at the 20:00Z close, the shape Alpaca actually returns."""
    out = []
    for i, day in enumerate(days):
        stamp = dt.datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC)
        base = price0 + i
        out.append(
            AggBar(t=stamp, o=base, h=base + 2, l=base - 2, c=base + 1, v=1_000_000.0,
                   feed="sip")
        )
    return out


def test_weekly_buckets_are_iso_weeks_starting_monday() -> None:
    days = [dt.date(2026, 3, 2) + dt.timedelta(days=i) for i in range(14)]
    result = aggregate_daily(daily_bars(days), "1w")
    starts = [b.t.astimezone(ET).date().isoformat() for b in result.bars]
    assert starts == ["2026-03-02", "2026-03-09"]  # Mondays
    assert result.bars[0].o == 500.0
    assert result.bars[0].c == 501.0 + 6  # 7 sessions, last close = 500+6+1
    assert result.bars[0].v == 7_000_000.0
    assert result.bars[0].t.astimezone(ET).strftime("%H:%M") == "00:00"


def test_monthly_and_quarterly_buckets_are_calendar() -> None:
    days = [dt.date(2026, 1, 30), dt.date(2026, 2, 2), dt.date(2026, 3, 31), dt.date(2026, 4, 1)]
    monthly = aggregate_daily(daily_bars(days), "1M")
    assert [b.t.astimezone(ET).date().isoformat() for b in monthly.bars] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
        "2026-04-01",
    ]
    quarterly = aggregate_daily(daily_bars(days), "1Q")
    assert [b.t.astimezone(ET).date().isoformat() for b in quarterly.bars] == [
        "2026-01-01",
        "2026-04-01",
    ]
    assert quarterly.bars[0].v == 3_000_000.0  # Jan 30 + Feb 2 + Mar 31


def test_calendar_bucket_start_boundaries() -> None:
    day = dt.date(2026, 5, 14)  # a Thursday
    assert calendar_bucket_start(day, "1w").date() == dt.date(2026, 5, 11)
    assert calendar_bucket_start(day, "1M").date() == dt.date(2026, 5, 1)
    assert calendar_bucket_start(day, "1Q").date() == dt.date(2026, 4, 1)
    assert calendar_bucket_start(dt.date(2026, 1, 1), "1Q").date() == dt.date(2026, 1, 1)
    with pytest.raises(ValueError):
        calendar_bucket_start(day, "1h")


def test_daily_session_date_survives_both_stamp_shapes() -> None:
    """Alpaca has stamped 1Day bars at 04:00Z, 20:00Z and 00:00Z across versions."""
    expected = calendar_bucket_start(dt.date(2026, 9, 15), "1M").date()
    for hour in (0, 4, 20):
        bar = AggBar(
            t=dt.datetime(2026, 9, 15, hour, 0, tzinfo=UTC), o=1, h=1, l=1, c=1, v=1,
            feed="sip",
        )
        result = aggregate_daily([bar], "1M")
        # 04:00Z is midnight ET on the 15th and 20:00Z is the 15th's close; a bare
        # 00:00Z stamp is 20:00 ET on the 14th, which still means the 15th's session.
        assert result.bars[0].t.astimezone(ET).date() == expected == dt.date(2026, 9, 1)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_1d_is_a_passthrough_that_normalises_timestamps() -> None:
    bars = daily_bars([dt.date(2026, 9, 15), dt.date(2026, 9, 16)])
    result = aggregate(bars, "1d")
    assert result.timeframe == "1d"
    assert [b.t.tzinfo for b in result.bars] == [UTC, UTC]
    assert result.sessions == 2
    assert [b.c for b in result.bars] == [501.0, 502.0]


def test_dispatch_rejects_an_unknown_timeframe() -> None:
    with pytest.raises(ValueError, match="unknown timeframe"):
        aggregate([], "3m")
    with pytest.raises(ValueError):
        aggregate_intraday([], "1d")
    with pytest.raises(ValueError):
        aggregate_daily([], "1h")
