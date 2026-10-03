"""Black-Scholes pricer + IV solver: known-value pins, parity, round-trips, refusals.

These tests exist to make the backfill's numbers auditable. An inverted IV that is
subtly wrong does not raise — it produces a plausible percentile rank, which is the worst
possible failure for a gate that reads the number and blocks or passes on it. So the
contract pinned here is deliberately three-layered:

1. **Known values** — textbook numbers with hand-computable answers, so a sign error or
   a wrong discount convention cannot hide behind a self-consistent implementation.
2. **Parity and round-trip as invariants over arbitrary inputs** — put-call parity
   residual ~0 and vol -> price -> vol is the identity, checked across a grid rather than
   at two points. A pricer that is right at one spot and wrong at another fails these.
3. **Refusals** — the inputs that must *not* produce a number. Arm B's 0-DTE band makes
   the zero-time refusal a live path, not a hypothetical.
"""

from __future__ import annotations

import math

import pytest

from executor.black_scholes import (
    DEFAULT_DIVIDEND_YIELD,
    DEFAULT_RISK_FREE_RATE,
    BsError,
    IvNotInvertible,
    black_scholes_price,
    implied_volatility,
    intrinsic_and_floor,
    invert_bar_close,
    put_call_parity_residual,
    vega,
)

SQRT_2PI = math.sqrt(2.0 * math.pi)


def price(**kw):
    kw.setdefault("rate", DEFAULT_RISK_FREE_RATE)
    kw.setdefault("dividend", DEFAULT_DIVIDEND_YIELD)
    kw.setdefault("spot", 650.0)
    kw.setdefault("strike", 650.0)
    kw.setdefault("dte", 120.0)
    kw.setdefault("sigma", 0.16)
    kw.setdefault("right", "call")
    return black_scholes_price(**kw)


# ---------------------------------------------------------------------------
# 1. known values
# ---------------------------------------------------------------------------


def test_norm_cdf_matches_known_points():
    from executor.black_scholes import _norm_cdf, _norm_pdf

    # Standard normal table values.
    for x, expected in [
        (0.0, 0.5),
        (1.0, 0.8413447460685429),
        (-1.0, 0.15865525393145707),
        (1.959963984540054, 0.975),
        (-2.5758293035489004, 0.005),
    ]:
        assert _norm_cdf(x) == pytest.approx(expected, abs=1e-12)
    assert _norm_pdf(0.0) == pytest.approx(1.0 / SQRT_2PI, rel=1e-15)
    assert _norm_pdf(1.0) == pytest.approx(0.24197072451914337, rel=1e-12)


def test_atm_call_matches_hand_computed_black_scholes():
    # S=K=650, T=120/365, r=0.0425, q=0.013, sigma=0.16.
    # d1 = ((r-q+sigma^2/2)*T) / (sigma*sqrt(T)), d2 = d1 - sigma*sqrt(T).
    t = 120.0 / 365.0
    sigma = 0.16
    d1 = ((DEFAULT_RISK_FREE_RATE - DEFAULT_DIVIDEND_YIELD + 0.5 * sigma * sigma) * t) / (
        sigma * math.sqrt(t)
    )
    d2 = d1 - sigma * math.sqrt(t)
    expected = 650.0 * math.exp(-DEFAULT_DIVIDEND_YIELD * t) * (
        0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    ) - 650.0 * math.exp(-DEFAULT_RISK_FREE_RATE * t) * (
        0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    )
    assert price(right="call") == pytest.approx(expected, rel=1e-12)
    # And the number is in the right neighbourhood, so the formula above is not trivially
    # satisfied by a constant.
    assert 25.0 < expected < 30.0


def test_zero_dte_is_discounted_intrinsic_and_not_a_diverging_number():
    # At expiry there is no time value. The undiscounted intrinsic is 20.
    assert price(dte=0.0, strike=630.0, right="call") == pytest.approx(20.0, rel=1e-12)
    assert price(dte=0.0, strike=670.0, right="put") == pytest.approx(20.0, rel=1e-12)
    # OTM at expiry is worth exactly zero, not a small positive number.
    assert price(dte=0.0, strike=700.0, right="call") == pytest.approx(0.0, abs=1e-12)


def test_zero_vol_equals_discounted_intrinsic():
    t = 120.0 / 365.0
    expected = max(
        650.0 * math.exp(-DEFAULT_DIVIDEND_YIELD * t)
        - 640.0 * math.exp(-DEFAULT_RISK_FREE_RATE * t),
        0.0,
    )
    assert price(sigma=0.0, strike=640.0, right="call") == pytest.approx(expected, rel=1e-12)


def test_vega_is_right_side_independent_and_matches_finite_difference():
    analytic = vega(spot=650.0, strike=650.0, dte=120.0, sigma=0.16)
    bumped_up = price(sigma=0.1600001)
    bumped_dn = price(sigma=0.1599999)
    finite = (bumped_up - bumped_dn) / 2e-7
    assert analytic == pytest.approx(finite, rel=1e-6)
    # Vega takes no `right`: it is analytically identical for a call and a put at the same
    # (S, K, t, sigma). Asserted through the price, one line below, rather than by calling
    # a signature that does not exist.
    put_vega = vega(spot=650.0, strike=650.0, dte=120.0, sigma=0.16)
    assert put_vega == pytest.approx(analytic, rel=1e-12)
    put_up = price(sigma=0.1600001, right="put")
    assert (put_up - price(sigma=0.1599999, right="put")) / 2e-7 == pytest.approx(
        analytic, rel=1e-6
    )
    # Feasibility doc §3b measured 147.18 for ATM 120d at spot 650, r=4%, q=1.3%.
    assert 100.0 < analytic < 200.0


def test_vega_is_zero_at_expiry():
    assert vega(spot=650.0, strike=650.0, dte=0.0, sigma=0.16) == 0.0


# ---------------------------------------------------------------------------
# 2. parity + round-trip as invariants over a grid
# ---------------------------------------------------------------------------

#: Round-trip grid. Deliberately excludes corners where the model price underflows to
#: exactly 0.0 in double precision (deep OTM at low vol): there the price genuinely
#: carries no information about vol, the solver refuses on a non-positive price, and
#: asserting `invert(price(v)) == v` there would be asserting an impossibility. Those
#: corners are pinned separately as REFUSALS below, which is the honest contract.
GRID = [
    (spot, strike, dte, vol)
    for spot in (400.0, 650.0, 800.0)
    for strike in (spot * 0.95, spot, spot * 1.05)
    for dte in (30.0, 120.0, 180.0)
    for vol in (0.10, 0.30)
]


def test_put_call_parity_holds_for_every_grid_point():
    for spot, strike, dte, vol in GRID:
        c = price(spot=spot, strike=strike, dte=dte, sigma=vol, right="call")
        p = price(spot=spot, strike=strike, dte=dte, sigma=vol, right="put")
        residual = put_call_parity_residual(
            spot=spot,
            strike=strike,
            dte=dte,
            call_price=c,
            put_price=p,
        )
        assert residual == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize(("spot", "strike", "dte", "vol"), GRID)
def test_round_trip_vol_price_vol_is_the_identity(spot, strike, dte, vol):
    """The load-bearing property for a backfill: invert(price(v)) == v."""
    for right in ("call", "put"):
        forward_price = price(spot=spot, strike=strike, dte=dte, sigma=vol, right=right)
        solved = implied_volatility(
            price=forward_price,
            spot=spot,
            strike=strike,
            dte=dte,
            right=right,
        )
        assert solved.iv == pytest.approx(vol, abs=1e-6)
        assert abs(solved.price_residual) < 1e-6


def test_round_trip_survives_a_one_cent_close_error():
    """A cent of close error is ~0.05 vol points; the rank absorbs that, solver must not throw."""
    forward_price = price(spot=650.0, strike=650.0, dte=120.0, sigma=0.16, right="call")
    for delta in (-0.01, 0.01):
        solved = implied_volatility(
            price=forward_price + delta,
            spot=650.0,
            strike=650.0,
            dte=120.0,
            right="call",
        )
        assert solved.iv == pytest.approx(0.16, abs=0.001)


def test_newton_acceleration_converges_far_inside_the_bisection_bound():
    """Bisection alone needs ~45 iterations for a vol tolerance this tight; Newton needs few."""
    forward_price = price(sigma=0.16)
    solved = implied_volatility(
        price=forward_price, spot=650.0, strike=650.0, dte=120.0, right="call"
    )
    assert solved.iterations <= 12, f"took {solved.iterations} iterations"


def test_solver_is_monotone_and_respects_custom_bounds():
    forward = price(sigma=0.16)
    tight = implied_volatility(
        price=forward,
        spot=650.0,
        strike=650.0,
        dte=120.0,
        right="call",
        min_vol=0.10,
        max_vol=0.30,
    )
    assert tight.iv == pytest.approx(0.16, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. refusals — the cases that must NOT produce a number
# ---------------------------------------------------------------------------


def test_zero_dte_refuses_because_vega_is_zero():
    """Arm B's frozen band is dte 0 — this is the live path, not an edge case."""
    with pytest.raises(IvNotInvertible, match="vega is identically zero"):
        implied_volatility(
            price=0.50, spot=650.0, strike=650.0, dte=0.0, right="call"
        )


def test_price_below_no_arbitrage_floor_refuses():
    _, floor = intrinsic_and_floor(spot=650.0, strike=600.0, dte=120.0, right="call")
    assert floor > 40.0
    with pytest.raises(IvNotInvertible, match="below the no-arbitrage floor"):
        implied_volatility(
            price=floor - 1.0, spot=650.0, strike=600.0, dte=120.0, right="call"
        )


def test_price_beyond_max_vol_refuses():
    with pytest.raises(IvNotInvertible, match="exceeds what"):
        implied_volatility(
            price=500.0, spot=650.0, strike=650.0, dte=30.0, right="call"
        )


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_non_positive_or_non_finite_price_refuses(bad):
    with pytest.raises(IvNotInvertible):
        implied_volatility(
            price=bad, spot=650.0, strike=650.0, dte=120.0, right="call"
        )


def test_double_precision_zero_price_refuses_rather_than_inventing_a_vol():
    """Deep OTM at low vol models to exactly 0.0 — which is information-free, not a vol of 0.

    This is the corner that a naive backfill turns into a fake observation. It refuses.
    """
    assert price(spot=400.0, strike=650.0, dte=120.0, sigma=0.08, right="call") == 0.0
    with pytest.raises(IvNotInvertible, match="positive finite"):
        implied_volatility(
            price=0.0, spot=400.0, strike=650.0, dte=120.0, right="call"
        )


def test_bad_right_and_bad_inputs_raise_bserror():
    with pytest.raises(BsError):
        price(right="straddle")
    with pytest.raises(BsError):
        price(spot=-1.0)
    with pytest.raises(BsError):
        price(strike=0.0)
    with pytest.raises(BsError):
        price(sigma=-0.1)
    with pytest.raises(BsError):
        price(dte=-5.0)


# ---------------------------------------------------------------------------
# 4. invert_bar_close — the backfill's real entry point
# ---------------------------------------------------------------------------


def test_invert_bar_close_round_trips_on_a_realistic_bar():
    forward = price(spot=650.0, strike=650.0, dte=120.0, sigma=0.16, right="call")
    result = invert_bar_close(
        close=forward,
        volume=500.0,
        spot=650.0,
        strike=650.0,
        dte=120.0,
        right="call",
    )
    assert result.iv == pytest.approx(0.16, abs=1e-6)
    assert result.vega > 1.0


def test_invert_bar_close_refuses_an_untraded_contract():
    forward = price(sigma=0.16)
    with pytest.raises(IvNotInvertible, match="below floor"):
        invert_bar_close(
            close=forward, volume=0.0, spot=650.0, strike=650.0, dte=120.0, right="call"
        )


def test_invert_bar_close_refuses_unknown_volume():
    """An unknown is not evidence of liquidity."""
    forward = price(sigma=0.16)
    with pytest.raises(IvNotInvertible, match="unknown liquidity"):
        invert_bar_close(
            close=forward, volume=None, spot=650.0, strike=650.0, dte=120.0, right="call"
        )


def test_invert_bar_close_refuses_vega_collapse():
    """Deep OTM call at short tenor: solvable by bisection, meaningless by vega.

    S=650 K=800, 60 DTE, 16% vol prices at $0.0113 — a real, positive price, and the solver
    recovers 16% from it exactly. But vega there is 0.876, roughly 170x smaller than the
    147.18 the gate's ATM 120-DTE contract carries, which means a rounding-level error in
    the price moves the "IV" by vol points. The bar is invertible and still not an
    observation, which is precisely why the conditioning guard lives in
    ``invert_bar_close`` and not in the solver.
    """
    forward = price(spot=650.0, strike=800.0, dte=60.0, sigma=0.16, right="call")
    assert 0.0 < forward < 0.05
    # It IS a price, so the solver accepts it...
    raw = implied_volatility(
        price=forward, spot=650.0, strike=800.0, dte=60.0, right="call"
    )
    assert raw.iv == pytest.approx(0.16, abs=1e-5)
    assert raw.vega < 1.0
    # ...but the conditioning guard is what the backfill must honour.
    with pytest.raises(IvNotInvertible, match="vega"):
        invert_bar_close(
            close=forward, volume=1000.0, spot=650.0, strike=800.0, dte=60.0, right="call"
        )


def test_vega_floor_does_not_refuse_the_contracts_the_gate_reads():
    """The guard must not eat the ATM 90-180 DTE band arm C actually reads."""
    for dte in (90.0, 120.0, 180.0):
        forward = price(spot=650.0, strike=650.0, dte=dte, sigma=0.16, right="call")
        result = invert_bar_close(
            close=forward, volume=500.0, spot=650.0, strike=650.0, dte=dte, right="call"
        )
        assert result.iv == pytest.approx(0.16, abs=1e-6)


def test_intrinsic_and_floor_agrees_with_the_pricer_at_zero_time():
    intrinsic, floor = intrinsic_and_floor(spot=650.0, strike=640.0, dte=0.0, right="call")
    assert intrinsic == pytest.approx(10.0)
    assert floor == pytest.approx(price(dte=0.0, strike=640.0, right="call"), rel=1e-12)


def test_iv_result_is_float_convertible():
    forward = price(sigma=0.16)
    result = implied_volatility(
        price=forward, spot=650.0, strike=650.0, dte=120.0, right="call"
    )
    assert float(result) == pytest.approx(0.16, abs=1e-6)


def test_default_rate_and_dividend_are_documented_numbers():
    """Pinned so a future edit to either constant is a visible diff, not a silent shift."""
    assert DEFAULT_RISK_FREE_RATE == 0.0425
    assert DEFAULT_DIVIDEND_YIELD == 0.013