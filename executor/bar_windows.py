"""THE bars-window policy — one implementation, every live bars read in the repo.

MEASURED 2026-10-03, paper creds, ``GET /v2/stocks/{symbol}/bars``. A **limit-only**
query — no ``start``, no ``end`` — answers **zero bars with HTTP 200**:

    1Day/sip/limit=400                        -> n=0
    1Min/iex/limit=5                          -> n=0
    1Day/iex/limit=400                        -> n=0
    1Day/sip/end=<now-20m>/limit=400          -> n=0      (end alone is NOT a window)
    1Day/sip/start+end+limit=400/sort=desc    -> n=400, newest 2026-10-02
    1Day/sip/start+end+limit=400/no sort      -> n=400, newest 2026-09-30

``limit`` is a cap on the answer, not a span the server can resolve. Every live read in
this repo has to name a window or it silently reads nothing — and, worse than a crash,
several of those reads swallow the empty result into a *fallback branch*, so the run
looks like it passed.

``sort="desc"`` is load-bearing, not a refinement: the route's default ascending order
truncates from the **newest** end, so a windowed read with a ``limit`` answers with the
oldest N bars in the window and the newest (signal) bar is missing without a word.
``desc`` puts the cap on the oldest end, which is what every caller already assumes a
``limit`` means. Parsing re-sorts ascending regardless, so consumers still see
oldest-first.

Why this module exists: :func:`bar_window` started life inside
:mod:`executor.watch_loop` (commit ede6342) and the *same* limit-only read shipped
independently in ``executor.main.cmd_eod`` and ``executor.soak._AlpacaSource`` —
three seams of one bug class from one policy written once. A window policy that lives
next to one of its callers is not a shared policy; it is a coincidence. Import it here.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping

__all__ = [
    "BARS_PER_SESSION",
    "REALTIME_FEEDS",
    "SESSIONS_PER_CALENDAR_DAY",
    "SIP_END_BACKOFF_MINUTES",
    "WEEKEND_HOLIDAY_PADDING_DAYS",
    "bar_window",
    "iso_z",
]

#: Bars in one US regular session, per timeframe. A ``limit`` is a count of bars and
#: the API needs a span of time, so this is the conversion between the two.
#: An unknown timeframe falls back to the finest (most bars/session), which yields the
#: narrowest window — under-fetching shows up as missing bars, over-fetching costs
#: nothing.
BARS_PER_SESSION: Mapping[str, int] = {
    "1Min": 390,
    "2Min": 195,
    "5Min": 78,
    "15Min": 26,
    "30Min": 13,
    "1Hour": 7,
    "1Day": 1,
}

#: Sessions per calendar day, for turning "N sessions of bars" into a date floor:
#: 5/7 = 0.714 weekdays-per-calendar-day, less ~3.5% of weekdays lost to the ~9 US
#: market holidays a year. Deliberately biased LOW — a too-narrow window costs bars,
#: a too-wide one is free (see :func:`bar_window` on why).
SESSIONS_PER_CALENDAR_DAY = 0.69

#: Extra calendar days on every window so one that opens on a weekend or a closure still
#: contains the session before it. 4 covers an ordinary 3-day weekend; anything longer is
#: a visible gap rather than a silent one.
WEEKEND_HOLIDAY_PADDING_DAYS = 4

#: SIP equities on the free tier refuse a query whose ``end`` is less than 15 minutes old
#: ("subscription does not permit querying recent SIP data"). 20 minutes is the same
#: backoff and the same reasoning as ``EQUITY_END_BACKOFF_MINUTES`` in
#: :mod:`executor.iv_backfill` / :mod:`executor.backfill_flow` — ask for a window that is
#: actually answerable rather than rewriting the caller's timestamp. It costs the last 20
#: minutes of today, which no *completed* daily bar needs.
#:
#: # AUDITED 2026-10-03 (live seam sweep): MEASURED, this clamp is not actually
#: required. A windowed SIP read with **no** ``end`` at all answers with the newest
#: bars (``1Day``/sip/start+sort=desc/limit=400 -> n=400, newest 2026-10-02). It is kept
#: because the 15-minute gate is the documented behaviour and a clamp costs nothing,
#: but nothing in this repo should claim a read failed *because* it omitted it.
SIP_END_BACKOFF_MINUTES = 20

#: Feeds with no recency gate, so they are asked for with no ``end`` at all.
#: MEASURED 2026-10-03: 1Min/iex with a ``start`` and no ``end`` answers with the newest
#: bars, while SIP is treated as gated (see above).
REALTIME_FEEDS = frozenset({"iex"})


def iso_z(moment: dt.datetime) -> str:
    """RFC3339 UTC with a ``Z``, which is what every Alpaca query param wants."""
    return moment.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def bar_window(
    *, timeframe: str, feed: str, limit: int, now: dt.datetime | None = None
) -> dict[str, str]:
    """Query params (``start``/``end``/``sort``) that make a bar read answerable.

    ``start`` is a FLOOR, not the truncator: it must be old enough to contain the
    sessions the caller asked for, and wider is free (because ``sort="desc"`` puts the
    ``limit`` cap on the oldest end). Splat it into the call:

        client.get_daily_bars(sym, feed="sip", limit=400, **bar_window(
            timeframe="1Day", feed="sip", limit=400))

    See this module's docstring for the measurements that make each part load-bearing.
    """
    if now is None:
        now = dt.datetime.now(dt.UTC)
    per_session = BARS_PER_SESSION.get(timeframe, min(BARS_PER_SESSION.values()))
    sessions = max(1, math.ceil(limit / per_session))
    floor = now - dt.timedelta(
        days=math.ceil(sessions / SESSIONS_PER_CALENDAR_DAY) + WEEKEND_HOLIDAY_PADDING_DAYS
    )
    params = {"start": iso_z(floor), "sort": "desc"}
    if feed not in REALTIME_FEEDS:
        params["end"] = iso_z(now - dt.timedelta(minutes=SIP_END_BACKOFF_MINUTES))
    return params