"""Indicator stack — pure deterministic math on stdlib data.

Why stdlib only (no pandas / numpy / TA-Lib)
--------------------------------------------
Three reasons, in order of how much they matter for THIS project:

1. **Determinism.** Every value the checklist reads is recomputed bit-for-bit from
   a price series. Floating-point results must be identical on every run and every
   machine, because the journal is the experiment: a NO-SHOT entry recorded with
   RSI 61.83 has to be reproducible from the same closes five months later. Vectorised
   libraries change dtypes, kernel order and NaN handling between releases; a pinned
   stdlib loop does not.
2. **Auditability.** The operator reviews this phase closely and the rulebook must be
   checkable by hand. Every line below is arithmetic a reader can redo in a
   notepad — the test suite shows the RSI and MACD arithmetic explicitly for exactly
   that reason.
3. **Zero transitive risk.** No compiled wheels, no BLAS threading, no
   platform-dependent rounding of a dot product. Nothing here can phone home or
   change behaviour under us mid-window.

Series-alignment policy (applies to EVERY function here)
--------------------------------------------------------
Inputs are oldest-first (``closes[0]`` is the oldest bar). Outputs are **aligned**:
``out[i]`` corresponds to ``series[i]``, and positions where the indicator is not yet
defined are ``None`` — never ``0``, never a truncated list, never silently dropped.
A shorter list would invite off-by-one bugs at the newest bar, which is the only bar
the checklist ever reads; ``None`` at undefined positions makes "not enough history"
an explicit, checkable fact rather than an implicit index error. Callers that want
only the latest value use the ``None``-safe helpers at the bottom of this module.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Number = float
Series = Sequence[float]
Aligned = list[float | None]


class InsufficientData(ValueError):
    """Raised when a series is shorter than the indicator mathematically requires.

    Deliberately a loud error rather than a padded/None-filled result: a caller that
    asks for RSI(14) with 9 closes has a data-plumbing bug, and silently returning a
    short aligned series is how that bug reaches a trade.
    """


def _require(values: Sequence[float], minimum: int, what: str) -> None:
    n = len(values)
    if n < minimum:
        raise InsufficientData(f"{what} needs at least {minimum} values, got {n}")


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------


def sma(values: Series, period: int) -> Aligned:
    """Simple moving average, aligned, ``None`` for the first ``period - 1`` positions.

    Arithmetic mean of a simple rolling window. No smoothing, no seeding debate.
    """
    _check_period(period)
    _require(values, period, f"SMA({period})")
    out: Aligned = [None] * len(values)
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def ema(values: Series, period: int) -> Aligned:
    """Exponential moving average with the standard ``2 / (period + 1)`` multiplier.

    Seeding: ``ema[period - 1] = SMA of values[0:period]``, then the standard
    recursive step ``ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]``.

    Why SMA-seeded (the textbook/TA-Lib convention) rather than seeding with the
    first raw value: a seed of ``values[0]`` makes the first ``~3 x period`` outputs
    depend on a single bar and the EMA lags badly for the first minute of the series
    — for a 26-period MACD leg that error reaches the newest bar for a *200-day*
    chart, i.e. the exact bar the checklist reads. SMA seeding is the convention
    every charting package (and Wilder's own work) uses, so it is also the only
    choice under which our numbers match the ones the operator eyeballs on a chart.
    Documented here because seeding is invisible in the formula and is a real
    source of "why doesn't this match TradingView".

    # INTERPRETATION: seeding = SMA of the first ``period`` values. The brief is
    silent; SMA seeding is the textbook / charting-platform standard.
    """
    _check_period(period)
    _require(values, period, f"EMA({period})")
    alpha = 2.0 / (period + 1.0)
    out: Aligned = [None] * len(values)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# RSI (Wilder)
# ---------------------------------------------------------------------------


def rsi(values: Series, period: int = 14) -> Aligned:
    """Wilder's RSI, aligned, first defined value at index ``period``.

    Wilder's smoothing, **not** a simple average of gains and losses, and not
    Cutler's variant. The distinction is the whole point of this function: Wilder
    seeds ``avg_gain``/``avg_loss`` as the simple mean of the first ``period`` gains
    and losses, then updates each with

        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period

    An equally-weighted average (the "simple RSI" found in most one-liners) decays
    old data exponentially faster and gives materially different numbers on SPY — it
    is the classic reason a backtest disagrees with the chart.

    Flat series: avg_gain == avg_loss == 0 makes RS = 0/0 undefined; we return the
    neutral 50.0 rather than NaN so downstream comparisons stay total.

    # INTERPRETATION: a flat series -> 50.0 (neutral), not NaN and not an exception.
    """
    _check_period(period)
    _require(values, period + 1, f"RSI({period})")
    out: Aligned = [None] * len(values)

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        # avg_gain >= 0 always. 0/0 -> neutral; x/0 -> the conventional 100.
        return 50.0 if avg_gain == 0.0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MACDResult:
    """Aligned MACD triple.

    All three lists share the input length and oldest-first orientation; positions
    where a component is not yet defined are ``None``.

    Seeding, explicitly (the operator asked which seeding applies):
      * ``macd_line[i] = EMA(12)[i] - EMA(26)[i]``, both seeded with their own SMA
        of their own first period (so the line starts at index 25);
      * ``signal`` is an EMA(9) **of the defined part of the macd line only** — the
        25 undefined leading positions are dropped before seeding, and the signal is
        then re-aligned back to the input length. It therefore starts at index 33
        (25 + 9 - 1). Seeding the signal from the raw 9 values of the unaligned line
        would mix ``None``s into the seed and shift every signal value by one bar.
    """

    macd_line: Aligned
    signal_line: Aligned
    histogram: Aligned

    @property
    def first_defined_index(self) -> int:
        for i, value in enumerate(self.macd_line):
            if value is not None:
                return i
        return len(self.macd_line)


def macd(
    values: Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """MACD(12, 26, 9) by default: line, signal and histogram, all aligned."""
    _check_period(fast)
    _check_period(slow)
    _check_period(signal)
    if fast >= slow:
        raise ValueError(f"MACD fast {fast} must be below slow {slow}")
    _require(values, slow + signal - 1, f"MACD({fast},{slow},{signal})")

    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line: Aligned = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema, strict=True)
    ]

    # signal = EMA(signal) over the defined tail of the line, then re-aligned.
    start = next(i for i, v in enumerate(line) if v is not None)
    signal_aligned: Aligned = [None] * len(values)
    tail: list[float] = [v for v in line[start:] if v is not None]
    for offset, value in enumerate(ema(tail, signal)):
        signal_aligned[start + offset] = value

    histogram: Aligned = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(line, signal_aligned, strict=True)
    ]
    return MACDResult(line, signal_aligned, histogram)


# ---------------------------------------------------------------------------
# Bollinger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BollingerResult:
    """Aligned Bollinger bands.

    ``stddev`` is the **population** standard deviation (divisor N), the textbook
    and charting-platform convention for Bollinger bands. Sample stdev (divisor
    N-1) is ~4% wider at N=20 and would quietly move every band-touch event.

    # INTERPRETATION: population stdev (divisor N), not sample stdev (divisor N-1).
    """

    middle: Aligned
    upper: Aligned
    lower: Aligned
    bandwidth: Aligned  # (upper - lower) / middle, as a fraction (0.20 == 20%)


def bollinger(values: Series, period: int = 20, num_std: float = 2.0) -> BollingerResult:
    """Bollinger(20, 2.0) by default. Bandwidth is ``(upper - lower) / middle``."""
    _check_period(period)
    if num_std < 0:
        raise ValueError(f"num_std must be >= 0, got {num_std}")
    _require(values, period, f"Bollinger({period})")
    middle: Aligned = [None] * len(values)
    upper: Aligned = [None] * len(values)
    lower: Aligned = [None] * len(values)
    bandwidth: Aligned = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        sigma = math.sqrt(variance)
        mid = mean
        up = mean + num_std * sigma
        lo = mean - num_std * sigma
        middle[i] = mid
        upper[i] = up
        lower[i] = lo
        bandwidth[i] = (up - lo) / mid if mid != 0 else 0.0
    return BollingerResult(middle, upper, lower, bandwidth)


def bandwidth_percentile(series: Series, window: int = 60, value: float | None = None) -> float:
    """Percentile rank (0-100) of ``value`` within the trailing ``window`` of ``series``.

    Used by T3a: "bandwidth < 20th percentile of 60d". ``value`` defaults to the
    newest element of ``series``.

    Rank convention (the strict one): the percentage of window values **strictly
    below** ``value``, times 100. Equal values do not count as below, so a bandwidth
    tied with its own history is *not* scored at the 20th percentile — it needs to be
    below the band, exactly as the brief words it ("bandwidth < 20th percentile").
    The value itself is excluded from the window when ``value`` is the newest element
    so a series cannot vouch for itself.
    """
    _check_period(window)
    if not series:
        raise InsufficientData("bandwidth_percentile needs a non-empty series")
    if len(series) < window:
        raise InsufficientData(
            f"bandwidth_percentile(window={window}) needs at least {window} values, "
            f"got {len(series)}"
        )
    target = series[-1] if value is None else value
    tail = list(series[-window:])
    if value is None:
        tail = tail[:-1]
    if not tail:
        raise InsufficientData("bandwidth_percentile: empty comparison window")
    below = sum(1 for x in tail if x < target)
    return 100.0 * below / len(tail)


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------


def relative_volume(current_volume: float, volumes_20d: Series) -> float:
    """T4 relative volume: ``current / mean(volumes_20d)``.

    The brief says "≥ 1.5x 20-day average on the signal candle" and
    "≥ 1.5x for intraday triggers", so the caller passes the 20-day mean window for
    a daily bar or the matching run-rate baseline for an intraday bar. There is no
    20-day minimum enforced here beyond a non-empty window — the *number of* days is
    the caller's rulebook concern (``t4_volume.lookback_days``), not this function's.

    Zero-average window (halted / no prints): the ratio is mathematically undefined.
    Rather than raise inside the checklist — where a suspended ticker should read as
    "volume did not confirm", not as a crash — this returns ``inf`` for positive
    volume and ``0.0`` for zero volume. ``inf >= 1.5`` is True, which is arguably
    wrong on its face, so callers should note: a zero 20-day average means the
    window is unusable, and the checklist treats a non-finite relvol as a T4 FAIL
    (see checklist.py). Zero volume on a zero average is reported as 0.0.

    # INTERPRETATION: a zero 20-day baseline is not an error — it yields inf (or 0.0
    # for zero volume), and the checklist turns a non-finite ratio into a T4 FAIL
    # rather than a crash or a pass.
    """
    if not volumes_20d:
        raise InsufficientData("relative_volume needs a non-empty baseline window")
    if any(v < 0 for v in volumes_20d):
        raise ValueError("volumes must be non-negative")
    if current_volume < 0:
        raise ValueError("current_volume must be non-negative")
    average = sum(volumes_20d) / len(volumes_20d)
    if average == 0.0:
        return math.inf if current_volume > 0 else 0.0
    return current_volume / average


# ---------------------------------------------------------------------------
# aligned helpers
# ---------------------------------------------------------------------------

ValueStatus = Literal["ok", "pending"]


def latest(aligned: Aligned) -> float | None:
    """Newest defined value of an aligned series, or ``None`` if not yet defined."""
    for value in reversed(aligned):
        if value is not None:
            return value
    return None


def latest_pair(aligned: Aligned) -> tuple[float | None, float | None]:
    """``(newest, previous)`` defined values of an aligned series.

    Two bars of history, oldest-first. Raises if fewer than two values are defined —
    every checklist condition that compares "now vs before" (histogram expansion,
    band rejection, expansion direction) genuinely needs the prior bar, and
    returning ``(x, None)`` would turn a data bug into a silent pass.
    """
    found: list[float] = []
    for value in reversed(aligned):
        if value is not None:
            found.append(value)
            if len(found) == 2:
                break
    if len(found) < 2:
        raise InsufficientData("need at least two defined values for a previous-bar comparison")
    return found[0], found[1]


def confirmed(aligned: Aligned) -> ValueStatus:
    """``"ok"`` if the newest position is defined, else ``"pending"``."""
    return "ok" if aligned and aligned[-1] is not None else "pending"


__all__ = [
    "Aligned",
    "BollingerResult",
    "InsufficientData",
    "MACDResult",
    "bollinger",
    "bandwidth_percentile",
    "confirmed",
    "ema",
    "latest",
    "latest_pair",
    "macd",
    "relative_volume",
    "rsi",
    "sma",
]