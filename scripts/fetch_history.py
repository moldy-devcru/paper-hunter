"""Fetch the historical bars this measurement needs — and nothing else.

Two series, both free, both fetched with stdlib ``urllib`` so the analysis path adds no
third-party dependency (the executor is stdlib-only by design and this script keeps that
property).

* **SPY daily OHLCV** from Yahoo Finance's chart endpoint. This is the signal-bar source
  for T1-T4. The repo's own ``data/barcache.db`` is the production cache and it is
  **empty** (zero rows) — the soak has not run yet — so there is nothing local to reuse.
  Yahoo is not the production feed (Alpaca SIP is), and the difference matters for
  volume: Yahoo reports consolidated volume while Alpaca's free tier is IEX-only
  realtime. The measurement therefore reports T4's rate under a consolidated-volume
  baseline and flags it, rather than implying it is the number production will see.
* **VIX daily close** from Cboe, the same URL ``executor.iv_rank.VIX_CSV_URL`` names and
  the same file ``scripts/seed_ivrank.py`` already downloads for its warmup proxy.

Both are cached under ``data/historical/`` as CSV so a re-run is reproducible and
offline. The cache is committed deliberately: the numbers in
``docs/reviews/2026-10-03-gate-base-rates.md`` have to be re-derivable by a reader
without network access and without trusting that Yahoo still serves the same numbers.

Adjustments: Yahoo returns raw or split/dividend-adjusted depending on the ``events``
parameter. This fetch requests raw OHLC with ``includeAdjustedClose`` untouched, and
records which. T1-T4 read closes, highs, lows and volumes; the one adjustment that would
matter is a split, and SPY has had none in this window — the script asserts the bar count
and date range rather than assuming.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

from analysis.gate_baserate import BarRow, VixRow

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "historical"
SPY_CSV = CACHE_DIR / "spy_daily.csv"
VIX_CSV = CACHE_DIR / "vix_daily.csv"

YAHOO_CHART = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={start}&period2={end}&interval=1d&events=div%2Csplit"
)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) paper-hunter-gate-baserate/1.0"

#: Yahoo daily bars are stamped at the exchange open in UTC. Converted to a naive
#: datetime at the session's own date so ``Bar.t.date()`` is the trading day and the
#: cross-age arithmetic (which the repo does on tz-aware timestamps) stays in whole days.
_EXCHANGE_TZ = dt.timezone(dt.timedelta(hours=-5))


def _get(url: str, *, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def fetch_spy_csv(
    *,
    symbol: str = "SPY",
    start: dt.date = dt.date(2004, 1, 1),
    end: dt.date | None = None,
    refresh: bool = False,
) -> Path:
    """Download SPY daily OHLCV to ``data/historical/spy_daily.csv`` and return the path."""
    if SPY_CSV.exists() and not refresh:
        return SPY_CSV
    end = end or dt.date.today()
    url = YAHOO_CHART.format(
        symbol=symbol,
        start=int(dt.datetime.combine(start, dt.time(), tzinfo=dt.UTC).timestamp()),
        end=int(
            dt.datetime.combine(
                end + dt.timedelta(days=2), dt.time(), tzinfo=dt.UTC
            ).timestamp()
        ),
    )
    try:
        payload = json.loads(_get(url))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"could not fetch {symbol} daily bars from Yahoo ({exc}). The cached CSV at "
            f"{SPY_CSV} is the intended offline path — if it is missing, download "
            f"{url} manually into that path."
        ) from exc
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    stamps = result["timestamp"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with SPY_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for i, stamp in enumerate(stamps):
            close = quote["close"][i]
            if close is None:
                continue  # a session Yahoo has no print for is skipped, not zero-filled
            day = dt.datetime.fromtimestamp(stamp, dt.UTC).astimezone(_EXCHANGE_TZ).date()
            writer.writerow(
                [
                    day.isoformat(),
                    quote["open"][i],
                    quote["high"][i],
                    quote["low"][i],
                    close,
                    quote["volume"][i],
                ]
            )
    return SPY_CSV


def fetch_vix_csv(*, refresh: bool = False) -> Path:
    """Download the Cboe VIX daily CSV to ``data/historical/vix_daily.csv``."""
    if VIX_CSV.exists() and not refresh:
        return VIX_CSV
    # Imported rather than hardcoded: the URL is the executor's own, so a change there
    # moves this fetch with it instead of forking.
    from executor.iv_rank import VIX_CSV_URL

    try:
        text = _get(VIX_CSV_URL).decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"could not fetch the Cboe VIX CSV ({exc}). The cached copy at {VIX_CSV} is "
            "the intended offline path — download it manually into that path if missing."
        ) from exc
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or "CLOSE" not in (rows[0] or []):
        raise RuntimeError(f"Cboe VIX CSV did not have a CLOSE column; got {rows[0]!r}")
    keep = [rows[0]] + [r for r in rows[1:] if len(r) >= 5]
    with VIX_CSV.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(keep)
    return VIX_CSV


def load_bars(path: Path | str = SPY_CSV) -> list[BarRow]:
    """Read the cached SPY CSV into ``BarRow``s, oldest-first.

    ``t`` is a naive datetime at 09:30 New York on the session date — the same moment
    the exchange stamps a daily bar — so cross-age arithmetic between consecutive
    sessions is exactly one day, which is what the T2b guard's 24h threshold assumes.
    """
    out: list[BarRow] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = dt.date.fromisoformat(row["date"])
            out.append(
                BarRow(
                    t=dt.datetime.combine(day, dt.time(9, 30)),
                    o=float(row["open"]),
                    h=float(row["high"]),
                    l=float(row["low"]),
                    c=float(row["close"]),
                    v=float(row["volume"] or 0.0),
                )
            )
    out.sort(key=lambda b: b.t)
    return out


def load_vix(path: Path | str = VIX_CSV) -> list[VixRow]:
    """Read the cached Cboe VIX CSV into ``VixRow``s, oldest-first.

    Parsed with ``executor.iv_rank.parse_vix_csv`` — the executor's own parser, including
    its MM/DD/YYYY handling and its holiday-padding skip — then unpacked to ``VixRow``.
    Reusing the parser means a malformed or padded row is dropped by exactly the code
    that would drop it in production.
    """
    from executor.iv_rank import parse_vix_csv

    text = Path(path).read_text(encoding="utf-8")
    out: list[VixRow] = []
    for observation in parse_vix_csv(text):
        if observation.iv is None or observation.iv <= 0:
            continue
        # ``IvObservation.as_of`` is typed as ``DateLike`` and the CSV path fills it with
        # the normalised ISO string, so coerce rather than assume a ``date``.
        as_of = observation.as_of
        day = (
            dt.date.fromisoformat(as_of)
            if isinstance(as_of, str)
            else as_of.date()
            if isinstance(as_of, dt.datetime)
            else as_of
        )
        out.append(VixRow(day=day, close=float(observation.iv)))
    out.sort(key=lambda r: r.day)
    return out


__all__ = [
    "CACHE_DIR",
    "SPY_CSV",
    "VIX_CSV",
    "fetch_spy_csv",
    "fetch_vix_csv",
    "load_bars",
    "load_vix",
]