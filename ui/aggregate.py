"""Session-aware timeframe bucketing for the UI chart.

This is **algo-adjacent math**: it decides what a 15m candle on mads's screen is, and
the checklist's own intraday reads will eventually be compared against it. Two
consequences, and they are the reason this file is long and comment-heavy:

1. The merge is exact and boring. ``open = first``, ``high = max``, ``low = min``,
   ``close = last``, ``volume = sum``, ``t = bucket start``, ``feed = worst provenance
   present in the bucket``. Nothing is averaged, nothing is interpolated, and a bucket
   with one bar in it is that bar.
2. The *bucket boundaries* are a trading-calendar fact, not an arithmetic one. A
   1h bucket is 09:30-10:30, not 10:00-11:00; the last 1h bucket of the day is
   15:30-16:00 and is half the size of the others; a 4h bucket is 09:30-13:30 then a
   2.5-hour remainder. Buckets never span the open, never cross the close, and never
   straddle a session boundary. ``floor(unix_time / 3600)`` bucketing — the obvious
   implementation, and the one a charting library would do by default — is wrong here
   in a way that would quietly shift every intraday comparison.

Everything is computed in ``America/New_York`` via :mod:`zoneinfo`, never a fixed
UTC offset. A fixed ``-05:00`` is correct for five months of the year and wrong for
the other seven: on a spring-forward day the 09:30 ET open is 13:30Z, and a hardcoded
offset would file every bar of that session outside regular trading hours.

Edge cases, named
-----------------
**Half sessions (13:00 close).** Day after Thanksgiving and Christmas Eve. The
session is 210 minutes, so a 1h day yields six full buckets plus a 30-minute seventh,
and a 4h day yields one full bucket plus a 30-minute remainder. Handled by
:data:`EARLY_CLOSES` (13:00 ET) and :data:`SESSION_CLOSE`.

**Full closures.** A holiday produces no session, therefore no buckets. The calendar
deliberately does *not* try to be a complete holiday oracle outside the years it knows
(:data:`MARKET_HOLIDAYS`): inventing a closure that did not happen would hide real
bars, and failing to know about a closure merely yields an empty series for that day,
which is the honest shape of "we hold no data for this date".

**DST transitions.** The US changes DST at 02:00 ET, three and a half hours before the
open, so a regular session never contains a transition. Wall-clock arithmetic on
zone-aware datetimes is therefore exact inside a session, and ``zoneinfo`` resolves
the correct offset at the bucket boundary. The DST test pins the two 2026 transition
days precisely because that is where a hardcoded offset would break.

**Pre/post market.** Bars outside 09:30-16:00 ET never enter a bucket — a "pre-market
volume" candle glued onto the regular session would make every intraday indicator
wrong. They are *counted* in :attr:`BucketSeries.excluded` instead, so the UI can say
"I dropped 240 extended-hours bars" rather than silently lying about coverage.

# INTERPRETATION: 4h anchoring. 4h buckets anchor at the 09:30 open (09:30-13:30,
# 13:30-16:00), not at 10:30/14:30 or at the quarter-hour grid every other intraday
timeframe shares. The spec says "4h anchored at open with partial last bucket" and that
is also the only anchoring that makes 4h a whole-number subdivision of the session.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal
from zoneinfo import ZoneInfo

#: US equity regular trading hours. Everything in this module is ET wall-clock.
MARKET_TZ = ZoneInfo("America/New_York")
SESSION_OPEN = dt.time(9, 30)
SESSION_CLOSE = dt.time(16, 0)
#: Early-close session (day after Thanksgiving, Christmas Eve).
EARLY_CLOSE = dt.time(13, 0)
SESSION_MINUTES = 390

#: Bars per intraday timeframe, in minutes. Sizes are chosen to divide the 390-minute
#: session exactly except 4h, which is the documented anchored-with-remainder case.
INTRADAY_MINUTES: dict[str, int] = {
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
}

#: Calendar timeframes, built from daily bars. Case is load-bearing: ``1m`` is thirty
#: minutes of intraday data, ``1M`` is one calendar month.
CALENDAR_TIMEFRAMES: tuple[str, ...] = ("1w", "1M", "1Q")

TIMEFRAMES: tuple[str, ...] = (*INTRADAY_MINUTES, "1d", *CALENDAR_TIMEFRAMES)

#: Ranked worst-last. A bucket inherits the *worst* provenance it contains, because a
#: candle whose high came from one venue and whose volume came from another is not the
#: consolidated tape and must not be stamped as if it were.
PROVENANCE_RANK: dict[str, int] = {
    "sip": 0,
    "consolidated": 0,
    "iex": 1,
    "indicative": 2,
    "opra": 2,
}
#: Anything unrecognised is treated as the worst case rather than the best: an unknown
#: feed must never launder a partial print into a "consolidated" label.
UNKNOWN_PROVENANCE = 3

#: Full closures (market closed all day) the calendar knows about. 2026-2027 NYSE.
#: See the module docstring on why this list is short on purpose.
MARKET_HOLIDAYS: frozenset[dt.date] = frozenset(
    {
        # 2026
        dt.date(2026, 1, 1),
        dt.date(2026, 1, 19),
        dt.date(2026, 2, 16),
        dt.date(2026, 4, 3),
        dt.date(2026, 5, 25),
        dt.date(2026, 6, 19),
        dt.date(2026, 9, 7),
        dt.date(2026, 11, 26),
        dt.date(2026, 12, 25),
        # 2027
        dt.date(2027, 1, 1),
        dt.date(2027, 1, 18),
        dt.date(2027, 2, 15),
        dt.date(2027, 3, 26),
        dt.date(2027, 5, 31),
        dt.date(2027, 6, 18),
        dt.date(2027, 9, 6),
        dt.date(2027, 11, 25),
        dt.date(2027, 12, 24),
    }
)

#: 13:00 ET early closes. 2026-2027 NYSE.
EARLY_CLOSES: frozenset[dt.date] = frozenset(
    {
        # 2026: day after Thanksgiving (Fri Nov 27), Christmas Eve (Thu Dec 24)
        dt.date(2026, 11, 27),
        dt.date(2026, 12, 24),
        # 2027: day after Thanksgiving (Fri Nov 26), Christmas Eve (Fri Dec 24)
        dt.date(2027, 11, 26),
        dt.date(2027, 12, 24),
    }
)


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    """One trading session: an ET open and an ET close."""

    day: dt.date
    open: dt.datetime
    close: dt.datetime

    @property
    def minutes(self) -> int:
        return int((self.close - self.open).total_seconds() // 60)


def is_holiday(day: dt.date) -> bool:
    """Whether the calendar knows ``day`` as a full market closure."""
    return day in MARKET_HOLIDAYS


def session_close_time(day: dt.date) -> dt.time:
    """Regular or early close for ``day``. Unknown years get the full 16:00."""
    return EARLY_CLOSE if day in EARLY_CLOSES else SESSION_CLOSE


def session_for(day: dt.date) -> Session | None:
    """The session for an ET calendar date, or ``None`` on a known holiday."""
    if is_holiday(day):
        return None
    return Session(
        day=day,
        open=dt.datetime.combine(day, SESSION_OPEN, tzinfo=MARKET_TZ),
        close=dt.datetime.combine(day, session_close_time(day), tzinfo=MARKET_TZ),
    )


# ---------------------------------------------------------------------------
# bar model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AggBar:
    """One OHLCV bar with feed provenance.

    ``t`` is the bar/bucket **start**, tz-aware. The UI stamps provenance on every
    candle, so the feed rides on the bar rather than in a side channel that a chart
    series could forget to join.
    """

    t: dt.datetime
    o: float
    h: float
    l: float  # noqa: E741 - matches the Alpaca wire field
    c: float
    v: float
    n: int | None = None
    vw: float | None = None
    feed: str = "unknown"

    def as_dict(self) -> dict[str, object]:
        # Bucket starts are constructed in ET wall-clock, so they are converted
        # here rather than stringified in whatever zone they happen to carry:
        # the API speaks one timestamp format, and the journal's is UTC-with-Z.
        aware = self.t if self.t.tzinfo else self.t.replace(tzinfo=dt.UTC)
        return {
            "t": aware.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
            "o": self.o,
            "h": self.h,
            "l": self.l,
            "c": self.c,
            "v": self.v,
            "feed": self.feed,
        }


def provenance_rank(feed: str | None) -> int:
    """Worse-is-higher rank for a feed label. Public because the UI badges with it."""
    return PROVENANCE_RANK.get((feed or "").lower(), UNKNOWN_PROVENANCE)


def _rank(feed: str | None) -> int:
    return provenance_rank(feed)


def worse_feed(left: str | None, right: str | None) -> str:
    """The less trustworthy of two feed labels; ties keep the left one.

    "SIP consolidated" beats "IEX partial" because consolidated history is the whole
    tape; ``iex`` beats ``unknown``; two unknowns stay unknown. A bucket that mixes a
    SIP bar with an IEX bar is an IEX bucket, full stop.
    """
    if _rank(left) >= _rank(right):
        return left or "unknown"
    return right or "unknown"


def merge_bars(bars: Sequence[AggBar], bucket_start: dt.datetime) -> AggBar:
    """Exact OHLCV merge of one bucket's bars, oldest-first.

    ``open`` is the first bar's open, ``close`` the last bar's close, ``high`` the max
    high, ``low`` the min low, ``volume`` the sum. ``vw`` is volume-weighted when every
    bar carries one, else ``None`` rather than a made-up average of partials.
    """
    if not bars:
        raise ValueError("cannot merge an empty bucket")
    ordered = sorted(bars, key=lambda b: b.t)
    feed = ordered[0].feed
    for bar in ordered[1:]:
        feed = worse_feed(feed, bar.feed)
    n_total = sum(b.n for b in ordered if b.n is not None)
    volumes = [b.v for b in ordered]
    vw: float | None = None
    if all(b.vw is not None for b in ordered) and sum(volumes) > 0:
        vw = sum((b.v * b.vw) for b in ordered) / sum(volumes)  # type: ignore[operator]
    return AggBar(
        t=bucket_start,
        o=ordered[0].o,
        h=max(b.h for b in ordered),
        l=min(b.l for b in ordered),
        c=ordered[-1].c,
        v=sum(volumes),
        n=n_total if any(b.n is not None for b in ordered) else None,
        vw=vw,
        feed=feed,
    )


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------

Exclusion = Literal["pre_market", "post_market", "holiday", "outside_window"]


@dataclass(frozen=True, slots=True)
class BucketSeries:
    """Bucketed bars plus the bookkeeping the UI must not hide.

    ``excluded`` counts input bars that did NOT become part of any bucket, by reason.
    ``partial_buckets`` counts buckets shorter than their nominal size (the last
    bucket of every session, always). ``sessions`` is the number of distinct ET
    trading days represented.
    """

    timeframe: str
    bars: list[AggBar]
    excluded: dict[str, int] = field(default_factory=dict)
    partial_buckets: int = 0
    sessions: int = 0

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "bars": [b.as_dict() for b in self.bars],
            "count": len(self.bars),
            "sessions": self.sessions,
            "partial_buckets": self.partial_buckets,
            "excluded": dict(self.excluded),
            "excluded_total": self.excluded_total,
        }


# ---------------------------------------------------------------------------
# intraday bucketing
# ---------------------------------------------------------------------------


def _et(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None:
        # A naive stamp is assumed UTC (Alpaca's wire format always carries an
        # offset); guessing local time here would move a whole session by hours.
        return moment.replace(tzinfo=dt.UTC).astimezone(MARKET_TZ)
    return moment.astimezone(MARKET_TZ)


def intraday_bucket_start(
    moment_et: dt.datetime,
    minutes: int,
    session: Session,
) -> tuple[dt.datetime, Exclusion | None] | None:
    """Bucket start for an ET bar, or ``None`` when the bar is outside the session.

    Returns ``(bucket_start, None)`` for a regular-hours bar and ``None`` for a bar
    that belongs to no bucket at all; the caller decides how to count the rejection.
    """
    if moment_et < session.open:
        return None
    if moment_et >= session.close:
        return None
    offset = int((moment_et - session.open).total_seconds() // 60)
    index = offset // minutes
    return session.open + dt.timedelta(minutes=index * minutes), None


def aggregate_intraday(
    bars: Iterable[AggBar],
    timeframe: str,
) -> BucketSeries:
    """Bucket 1m bars into ``5m``/``10m``/``15m``/``30m``/``1h``/``4h``, session-aware.

    The first bucket of every session starts at the 09:30 open and the last one ends
    at the close (16:00, or 13:00 on an early-close day), so no bucket ever spans two
    sessions or runs past the close. A partial final bucket is emitted, not dropped:
    the 15:30-16:00 hour exists and hiding it would make a 1h chart disagree with the
    executor's own bar count.
    """
    if timeframe not in INTRADAY_MINUTES:
        raise ValueError(f"{timeframe!r} is not an intraday timeframe")
    minutes = INTRADAY_MINUTES[timeframe]
    ordered = sorted(bars, key=lambda b: b.t)

    grouped: dict[dt.datetime, list[AggBar]] = {}
    excluded: dict[str, int] = {}
    sessions: set[dt.date] = set()
    for bar in ordered:
        local = _et(bar.t)
        session = session_for(local.date())
        if session is None:
            excluded["holiday"] = excluded.get("holiday", 0) + 1
            continue
        placed = intraday_bucket_start(local, minutes, session)
        if placed is None:
            reason: Exclusion = "pre_market" if local < session.open else "post_market"
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        start, _ = placed
        grouped.setdefault(start, []).append(bar)
        sessions.add(local.date())

    merged: list[AggBar] = []
    partial = 0
    for start in sorted(grouped):
        session = session_for(start.date())
        if session is None:  # unreachable: a holiday day never produced a bucket
            continue
        merged.append(merge_bars(grouped[start], start))
        if start + dt.timedelta(minutes=minutes) > session.close:
            partial += 1
    return BucketSeries(
        timeframe=timeframe,
        bars=merged,
        excluded=excluded,
        partial_buckets=partial,
        sessions=len(sessions),
    )


# ---------------------------------------------------------------------------
# daily -> calendar bucketing
# ---------------------------------------------------------------------------


def session_date(moment: dt.datetime) -> dt.date:
    """The ET session date a daily bar belongs to.

    Alpaca has stamped ``1Day`` bars at more than one instant across API versions
    (session open 04:00Z, session close 20:00Z, and a bare 00:00Z in older payloads).
    Taking the ET date is correct for the close stamp, one day early for a 00:00Z
    stamp, so we take the later of the UTC and ET dates: every stamp at or after
    04:00 ET resolves to the session day itself, and the 00:00Z case — which is really
    20:00 ET the previous evening — resolves to the day a reader means.

    # INTERPRETATION: the LATER of (UTC date, ET date). A single documented rule
    # beats a heuristic that special-cases a timestamp format.
    """
    utc_moment = moment.astimezone(dt.UTC) if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
    return max(utc_moment.date(), _et(moment).date())


def calendar_bucket_start(day: dt.date, timeframe: str) -> dt.datetime:
    """ET start of the ``1w``/``1M``/``1Q`` bucket containing ``day``.

    ``1w`` is the ISO week (Monday 00:00 ET — not a Sunday-anchored trading week, so
    the bucket is a real calendar object the operator can point at), ``1M`` is the
    first of the month, ``1Q`` is Jan/Apr/Jul/Oct.
    """
    if timeframe == "1w":
        start_day = day - dt.timedelta(days=day.weekday())
    elif timeframe == "1M":
        start_day = day.replace(day=1)
    elif timeframe == "1Q":
        start_day = day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    else:
        raise ValueError(f"{timeframe!r} is not a calendar timeframe")
    return dt.datetime.combine(start_day, dt.time(0, 0), tzinfo=MARKET_TZ)


def aggregate_daily(bars: Iterable[AggBar], timeframe: str) -> BucketSeries:
    """Bucket daily bars into ``1w``/``1M``/``1Q`` calendar buckets (ET).

    Identical merge semantics to the intraday path — a weekly candle is the same
    arithmetic as an hourly one, only the boundaries come from the calendar.
    """
    if timeframe not in CALENDAR_TIMEFRAMES:
        raise ValueError(f"{timeframe!r} is not a calendar timeframe")
    # Materialised first: the session count at the end needs a second pass, and a
    # one-shot iterator would be empty by then.
    ordered = list(bars)
    grouped: dict[dt.datetime, list[AggBar]] = {}
    for bar in ordered:
        start = calendar_bucket_start(session_date(bar.t), timeframe)
        grouped.setdefault(start, []).append(bar)
    merged = [merge_bars(grouped[start], start) for start in sorted(grouped)]
    return BucketSeries(
        timeframe=timeframe,
        bars=merged,
        excluded={},
        partial_buckets=0,
        sessions=len({session_date(b.t) for b in ordered}),
    )


def aggregate(
    bars: Iterable[AggBar],
    timeframe: str,
) -> BucketSeries:
    """Dispatch on the timeframe string.

    ``1d`` is a pass-through that still stamps provenance and normalises timestamps to
    UTC, so the API has one code path for "give me bars at this timeframe".
    """
    if timeframe == "1d":
        ordered = sorted(bars, key=lambda b: b.t)
        return BucketSeries(
            timeframe="1d",
            bars=[
                AggBar(
                    t=b.t.astimezone(dt.UTC) if b.t.tzinfo else b.t.replace(tzinfo=dt.UTC),
                    o=b.o,
                    h=b.h,
                    l=b.l,
                    c=b.c,
                    v=b.v,
                    n=b.n,
                    vw=b.vw,
                    feed=b.feed,
                )
                for b in ordered
            ],
            sessions=len({session_date(b.t) for b in ordered}),
        )
    if timeframe in INTRADAY_MINUTES:
        return aggregate_intraday(bars, timeframe)
    if timeframe in CALENDAR_TIMEFRAMES:
        return aggregate_daily(bars, timeframe)
    raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {', '.join(TIMEFRAMES)}")


def buckets_per_session(timeframe: str, session_minutes: int = SESSION_MINUTES) -> int:
    """Buckets a full session yields, including the short final one."""
    if timeframe not in INTRADAY_MINUTES:
        raise ValueError(f"{timeframe!r} is not an intraday timeframe")
    minutes = INTRADAY_MINUTES[timeframe]
    return -(-session_minutes // minutes)  # ceiling division


__all__ = [
    "AggBar",
    "BucketSeries",
    "CALENDAR_TIMEFRAMES",
    "EARLY_CLOSE",
    "EARLY_CLOSES",
    "INTRADAY_MINUTES",
    "MARKET_HOLIDAYS",
    "MARKET_TZ",
    "PROVENANCE_RANK",
    "SESSION_CLOSE",
    "SESSION_MINUTES",
    "SESSION_OPEN",
    "Session",
    "TIMEFRAMES",
    "aggregate",
    "aggregate_daily",
    "aggregate_intraday",
    "buckets_per_session",
    "calendar_bucket_start",
    "is_holiday",
    "merge_bars",
    "provenance_rank",
    "session_close_time",
    "session_date",
    "session_for",
    "worse_feed",
]
