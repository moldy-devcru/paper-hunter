"""Indicator tests — hand-computable references, not golden files.

Every expected value here is derived in a comment, because the whole point of this
stack being stdlib-only and hand-readable is that the operator can check the
arithmetic. Golden-file tests ("compare to whatever the code produced") would pin a
bug just as faithfully as they pin a fix.
"""

from __future__ import annotations

import math

import pytest

from executor.indicators import (
    InsufficientData,
    bandwidth_percentile,
    bollinger,
    confirmed,
    ema,
    latest,
    latest_pair,
    macd,
    relative_volume,
    rsi,
    sma,
)

# ---------------------------------------------------------------------------
# SMA / EMA
# ---------------------------------------------------------------------------


def test_sma_aligned_and_values():
    # windows: [1,2,3]=2 at index 2, then 3, 4 ... up to [8,9,10]=8 at index 9.
    assert sma([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3) == [
        None, None, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0,
    ]


def test_sma_min_length_and_period():
    with pytest.raises(InsufficientData):
        sma([1, 2], 3)
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)


def test_ema_sma_seeding_and_multiplier():
    # period=3 -> alpha = 2/(3+1) = 0.5.
    # Seed: ema[2] = SMA(1,2,3) = 2.0
    # ema[3] = 0.5*4 + 0.5*2 = 3.0
    # ema[4] = 0.5*5 + 0.5*3 = 4.0
    # ... the ramp is self-sustaining, so ema[i] == i for every i >= 2.
    assert ema([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3) == [
        None, None, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0,
    ]


def test_ema_seeding_is_not_first_value():
    # The alternative (bad) seeding, ema[2] = values[0] = 1.0, would give
    # ema[3] = 0.5*4 + 0.5*1 = 2.5 instead of 3.0. Pin the difference.
    result = ema([1, 2, 3, 4], 3)
    assert result[2] == pytest.approx(2.0)
    assert result[3] == pytest.approx(3.0)
    assert result[3] != pytest.approx(2.5)


def test_ema_alpha_matches_textbook_for_period_50():
    alpha = 2.0 / 51.0
    values = [100.0 + i for i in range(60)]
    result = ema(values, 50)
    seed = sum(values[:50]) / 50
    expected = alpha * values[50] + (1 - alpha) * seed
    assert result[50] == pytest.approx(expected)
    assert result[:49] == [None] * 49


def test_ema_min_length():
    with pytest.raises(InsufficientData):
        ema([1, 2], 3)


# ---------------------------------------------------------------------------
# RSI — Wilder
# ---------------------------------------------------------------------------


def test_rsi_wilder_arithmetic_shown_by_hand():
    # closes 10, 11, 12, 13, 12, 11 with period=3.
    # deltas: +1, +1, +1, -1, -1   gains: 1,1,1,0,0   losses: 0,0,0,1,1
    #
    # bar 3 (index 3): Wilder SEEDS with the simple mean of the first 3:
    #     avg_gain = (1+1+1)/3 = 1        avg_loss = (0+0+0)/3 = 0
    #     avg_loss == 0 and avg_gain > 0  ->  RSI = 100
    # bar 4 (index 4): Wilder smoothing, avg = (prev*(period-1) + current)/period:
    #     avg_gain = (1*2 + 0)/3 = 2/3
    #     avg_loss = (0*2 + 1)/3 = 1/3
    #     RS = (2/3)/(1/3) = 2   ->  RSI = 100 - 100/(1+2) = 66.6667
    # bar 5 (index 5): delta -1 again
    #     avg_gain = ((2/3)*2 + 0)/3 = 4/9
    #     avg_loss = ((1/3)*2 + 1)/3 = 5/9
    #     RS = (4/9)/(5/9) = 0.8 ->  RSI = 100 - 100/1.8 = 44.4444
    result = rsi([10, 11, 12, 13, 12, 11], 3)
    assert result[:3] == [None, None, None]
    assert result[3] == pytest.approx(100.0)
    assert result[4] == pytest.approx(200.0 / 3.0)
    assert result[5] == pytest.approx(44.4444444, abs=1e-6)


def test_rsi_is_wilder_not_simple_average():
    # The classic bug: an equally-weighted average of all 5 bars instead of Wilder's
    # recursive smoothing. Simple variant at the last bar:
    #     avg_gain = 3/5 = 0.6, avg_loss = 2/5 = 0.4, RS = 1.5 -> RSI = 60.0
    # Wilder gives 44.4444 on the same data. If these ever converge, Wilder's
    # smoothing has been lost.
    result = rsi([10, 11, 12, 13, 12, 11], 3)
    assert result[-1] == pytest.approx(44.4444444, abs=1e-6)
    assert result[-1] != pytest.approx(60.0)


def test_rsi_flat_series_is_neutral_50():
    # No gains and no losses: RS is 0/0, undefined. Policy: neutral 50.0, not NaN.
    assert rsi([50.0] * 20, 14)[-1] == pytest.approx(50.0)


def test_rsi_monotonic_up_is_100():
    assert rsi([float(i) for i in range(1, 21)], 14)[-1] == pytest.approx(100.0)


def test_rsi_monotonic_down_is_0():
    assert rsi([float(i) for i in range(20, 0, -1)], 14)[-1] == pytest.approx(0.0)


def test_rsi_min_length_is_period_plus_one():
    with pytest.raises(InsufficientData):
        rsi([1.0] * 14, 14)  # 14 values -> 13 deltas, one short
    assert len(rsi([1.0] * 15, 14)) == 15


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


def _ramp(n: int) -> list[float]:
    return [100.0 + i for i in range(n)]


def test_macd_line_is_ema_difference_and_alignment():
    values = _ramp(60)
    result = macd(values)
    fast, slow = ema(values, 12), ema(values, 26)
    for i in range(len(values)):
        if fast[i] is None or slow[i] is None:
            assert result.macd_line[i] is None
            assert result.signal_line[i] is None
            assert result.histogram[i] is None
        else:
            assert result.macd_line[i] == pytest.approx(fast[i] - slow[i])
    assert result.first_defined_index == 25


def test_macd_signal_seeding_shown_by_hand():
    values = _ramp(60)
    result = macd(values)
    # The line is defined from index 25. The signal is EMA(9) over the DEFINED TAIL
    # only (25 values), so it is defined from index 25 + 9 - 1 = 33, seeded with the
    # SMA of macd_line[25:34]. Seeding from raw unaligned values would shift the
    # whole signal by one bar.
    defined = [result.macd_line[i] for i in range(25, len(values))]
    seed = sum(defined[:9]) / 9
    assert result.signal_line[33] == pytest.approx(seed)
    assert result.signal_line[32] is None
    assert result.histogram[33] == pytest.approx(result.macd_line[33] - seed)


def test_macd_signal_recursion_step_shown_by_hand():
    values = _ramp(60)
    result = macd(values)
    # signal period 9 -> alpha = 2/10 = 0.2. One explicit step at index 34:
    #     signal[34] = 0.2*macd[34] + 0.8*signal[33]
    alpha = 0.2
    expected = alpha * result.macd_line[34] + (1 - alpha) * result.signal_line[33]
    assert result.signal_line[34] == pytest.approx(expected)


def test_macd_histogram_is_line_minus_signal():
    result = macd(_ramp(60))
    assert result.histogram[-1] == pytest.approx(
        result.macd_line[-1] - result.signal_line[-1]
    )


def test_macd_min_length_and_parameter_validation():
    # slow + signal - 1 = 34 values minimum.
    with pytest.raises(InsufficientData):
        macd(_ramp(33))
    assert len(macd(_ramp(34)).macd_line) == 34
    with pytest.raises(ValueError):
        macd(_ramp(60), fast=26, slow=12)
    with pytest.raises(ValueError):
        macd(_ramp(60), fast=12, slow=26, signal=0)


# ---------------------------------------------------------------------------
# Bollinger
# ---------------------------------------------------------------------------


def test_bollinger_hand_computed_on_1_to_20():
    values = [float(i) for i in range(1, 21)]
    result = bollinger(values)
    # mean = 10.5. Sum of squared deviations for 1..20 = 665, /20 = 33.25,
    # sigma = sqrt(33.25) = 5.766281...
    sigma = math.sqrt(33.25)
    assert result.middle[19] == pytest.approx(10.5)
    assert result.upper[19] == pytest.approx(10.5 + 2 * sigma)
    assert result.lower[19] == pytest.approx(10.5 - 2 * sigma)
    # bandwidth = (upper - lower)/middle = 4*sigma/10.5
    assert result.bandwidth[19] == pytest.approx((4 * sigma) / 10.5)
    assert result.middle[:19] == [None] * 19


def test_bollinger_zero_volatility_bands_collapse():
    result = bollinger([100.0] * 25)
    assert result.upper[-1] == pytest.approx(100.0)
    assert result.lower[-1] == pytest.approx(100.0)
    assert result.bandwidth[-1] == pytest.approx(0.0)


def test_bollinger_min_length_and_num_std():
    with pytest.raises(InsufficientData):
        bollinger([1.0] * 19)
    with pytest.raises(ValueError):
        bollinger([1.0] * 20, num_std=-1)


# ---------------------------------------------------------------------------
# bandwidth percentile
# ---------------------------------------------------------------------------


def test_bandwidth_percentile_counts_strictly_below():
    # 60 values 0..59. The newest (59) is excluded from its own comparison window,
    # so the window is 0..58 (59 values) and 59 is above every one of them -> 100%.
    series = [float(i) for i in range(60)]
    assert bandwidth_percentile(series, window=60) == pytest.approx(100.0)


def test_bandwidth_percentile_zero_rank_at_bottom():
    # A value below the entire window sits at rank 0.
    series = [float(i) for i in range(60)]
    assert bandwidth_percentile(series, window=60, value=-1.0) == pytest.approx(0.0)


def test_bandwidth_percentile_midpoint():
    # series = 0..59, value = 30 -> strictly below are 0..29 (30 values) out of a
    # 60-value window -> 50%. (Below the 20th percentile -> squeeze.)
    series = [float(i) for i in range(60)]
    assert bandwidth_percentile(series, window=60, value=30.0) == pytest.approx(50.0)


def test_bandwidth_percentile_excludes_the_newest_value_from_its_own_window():
    # 59 bars at 1.0 then a newest 5.0. If the newest were included in its own
    # window the rank would be 59/60; excluding it gives 100% (all 59 others below).
    series = [1.0] * 59 + [5.0]
    assert bandwidth_percentile(series, window=60) == pytest.approx(100.0)


def test_bandwidth_percentile_ties_do_not_count_as_below():
    # series 0..59. value == 5.0: strictly below are 0..4 (5 values) -> 8.33%.
    # The two 5s in the window are ties, not "below", so they do not lift the rank —
    # a bandwidth tied with its own history is NOT scored at the 20th percentile.
    series = [float(i) for i in range(60)]
    assert bandwidth_percentile(series, window=60, value=5.0) == pytest.approx(100.0 * 5 / 60)
    # a hair above the tie: now 0..5 (6 values) count -> 10%
    assert bandwidth_percentile(series, window=60, value=5.0000001) == pytest.approx(
        100.0 * 6 / 60
    )


def test_bandwidth_percentile_min_length():
    with pytest.raises(InsufficientData):
        bandwidth_percentile([1.0] * 59, window=60)
    with pytest.raises(InsufficientData):
        bandwidth_percentile([], window=10)


# ---------------------------------------------------------------------------
# relative volume
# ---------------------------------------------------------------------------


def test_relative_volume_basic():
    assert relative_volume(3.0, [2.0, 2.0, 2.0, 2.0]) == pytest.approx(1.5)


def test_relative_volume_below_threshold():
    assert relative_volume(1.0, [2.0, 2.0]) == pytest.approx(0.5)


def test_relative_volume_zero_baseline():
    # Undefined ratio. Positive volume on a dead baseline -> inf (and the checklist
    # treats a non-finite relvol as a FAIL); zero on zero -> 0.0.
    assert relative_volume(1000.0, [0.0, 0.0]) == math.inf
    assert relative_volume(0.0, [0.0, 0.0]) == pytest.approx(0.0)


def test_relative_volume_rejects_empty_and_negative():
    with pytest.raises(InsufficientData):
        relative_volume(100.0, [])
    with pytest.raises(ValueError):
        relative_volume(100.0, [1.0, -1.0])
    with pytest.raises(ValueError):
        relative_volume(-1.0, [1.0, 1.0])


# ---------------------------------------------------------------------------
# aligned helpers
# ---------------------------------------------------------------------------


def test_latest_and_latest_pair():
    aligned: list[float | None] = [None, None, 1.0, 2.0, 3.0]
    assert latest(aligned) == 3.0
    assert latest_pair(aligned) == (3.0, 2.0)
    assert latest([None, None]) is None
    with pytest.raises(InsufficientData):
        latest_pair([None, 1.0])


def test_confirmed_reports_pending_at_the_newest_bar():
    # ``confirmed`` is about the NEWEST bar only: leading Nones are expected
    # alignment padding and do not make the snapshot pending.
    assert confirmed([1.0, 2.0]) == "ok"
    assert confirmed([None, None, 1.0]) == "ok"
    assert confirmed([1.0, None]) == "pending"
    assert confirmed([]) == "pending"