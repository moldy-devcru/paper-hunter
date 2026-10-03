"""European Black-Scholes pricer and implied-volatility solver (net-new, stdlib only).

Why this module exists
----------------------
Alpaca's historical options route serves OHLCV bars and **no implied volatility field**
— confirmed live, not cited: the raw envelope's keys are ``['c','h','l','n','o','t','v','vw']``
and ``"impliedVolatility" in json.dumps(raw)`` is ``False``
(``docs/reviews/2026-10-03-iv-backfill-depth-probe.md``). So there are exactly two ways
to get an IV for a past session: a vendor's Greeks/IV history (which nobody sells for
SPY at a price this project will pay — see ``docs/research/2026-10-02-options-data-sources.md``
§1) or **invert the price we do have**. This module is the second one.

The repo previously had **no** Black-Scholes code at all — the only mentions were prose in
the outsider review (F9, an unimplemented grid) and a one-line description of Alpaca's IV
field. So this is genuinely net-new, including the rate and dividend sources, and both of
those are stated rather than hidden.

The two inputs we are guessing, stated plainly
------------------------------------------------

**Risk-free rate — a fixed documented constant, not a fetched curve.**
:data:`DEFAULT_RISK_FREE_RATE`
is a single annualised continuously-compounded rate used for every tenor. Why a constant
rather than fetching the Treasury curve: the inversion's sensitivity to ``r`` is second
order next to its sensitivity to the input price, a fetched curve would add a network
dependency and a second failure mode to a backfill that must be reproducible offline, and
at 90-180 DTE the entire 1-month Treasury bill and 1-year Treasury curve sat inside a
range that moves the inverted IV by well under a tenth of a vol point. The cost is
stated rather than buried: over a multi-year backfill spanning a full rate cycle, a
constant rate is wrong by more than it was in any single month, and the error is
*systematic* — it does not average out the way price noise does. It biases the level of
every backfilled IV by the same small amount. A caller who cares passes a curve-derived
``r`` per observation; the solver takes it as a parameter precisely so that upgrade is a
one-line change at the call site and not a rewrite.

**Dividend yield — SPY's trailing yield as a constant, and it is a real approximation.**
:data:`DEFAULT_DIVIDEND_YIELD` is SPY's annual trailing distribution yield, applied
continuously with no ex-dividend calendar. That is wrong in two specific ways, both
named here because they are the kind of thing that is invisible until it is expensive:
(1) SPY pays on a quarterly schedule, not continuously, so between ex-dates the
continuously-compounded model drifts a little against the true forward price; (2) at long
horizons the cumulative dividend is not a constant *yield* of spot — it is a cash amount,
and modelling it as a yield compounds that error. Both errors are small at the 90-180 DTE
the gate reads, and both are larger for the deep-ITM arm C actually trades. The IV error
they induce is small relative to the one-cent close error already quantified in
``docs/reviews/2026-10-03-iv-backfill-feasibility.md`` §3b, which is the honest reason this
is acceptable *and* the honest reason it is not free.

What this module will not do
-----------------------------
It will not return a number when there is no number. A price below the no-arbitrage
intrinsic floor, a non-positive price, or a zero-to-expiry contract all raise
:class:`IvNotInvertible` rather than returning a plausible-looking vol. That matters more
than it looks: at ``T = 0`` the Black-Scholes vega is exactly zero, so an "inverted" IV
would be the solver's own arbitrary stopping point wearing a decimal point. Arm B's DTE
band is ``0`` (``config/rules.example.yaml``), which makes this the *common* case for one
of the three arms and not an edge case at all.

Python 3.12+, stdlib ``math`` only — the executor keeps working with no third-party
packages, and a pricer is exactly the kind of thing that must not be the thing that adds
one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

#: Risk-free rate, annualised and continuously compounded, used when a caller supplies
#: none. See the module docstring for what this costs and why it is a constant.
DEFAULT_RISK_FREE_RATE = 0.0425

#: SPY's trailing annual distribution yield, applied continuously. See module docstring.
DEFAULT_DIVIDEND_YIELD = 0.013

#: Solver bounds. The floor exists so the bisection cannot walk into the region where
#: ``d1``/``d2`` overflow; the ceiling is generous — 500% vol is far past anything SPY
#: options have printed, and a price that needs a higher vol to explain is a *bad price*,
#: not a real one.
MIN_VOL = 1e-4
MAX_VOL = 5.0

#: Convergence: price tolerance in dollars, and volatility tolerance. Both are far below
#: anything that matters for a percentile rank (a one-cent close error is worth ~0.05 vol
#: points at these tenors) and are set so the solver terminates deterministically.
PRICE_TOL = 1e-8
VOL_TOL = 1e-9
MAX_ITERATIONS = 200

Right = Literal["call", "put"]


class BsError(ValueError):
    """Base for this module's refusals."""


class IvNotInvertible(BsError):
    """No volatility in ``[MIN_VOL, MAX_VOL]`` explains this price.

    Raised rather than clamped. A clamped answer is a fabricated one, and the caller's
    correct response to "this contract's price is not an option price" is to skip the
    observation, not to store a number that will later be ranked against real ones.
    """


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via ``erf``.

    ``math.erf`` rather than a rational approximation: it is stdlib, it is accurate to
    full double precision in the tails that matter here, and a hand-rolled approximation
    is exactly the kind of subtle error an IV series should not inherit.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _validate(right: str) -> str:
    side = right.strip().lower()
    if side not in ("call", "put"):
        raise BsError(f"right must be 'call' or 'put', got {right!r}")
    return side


def _d1_d2(
    spot: float,
    strike: float,
    t: float,
    rate: float,
    sigma: float,
    dividend: float,
) -> tuple[float, float]:
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * sigma * sigma) * t) / vol_t
    return d1, d1 - vol_t


def black_scholes_price(
    *,
    spot: float,
    strike: float,
    dte: float,
    rate: float = DEFAULT_RISK_FREE_RATE,
    sigma: float,
    right: str,
    dividend: float = DEFAULT_DIVIDEND_YIELD,
) -> float:
    """European option price under Black-Scholes-Merton with a continuous dividend yield.

    ``dte`` is in **calendar days** and is converted internally to years on a 365-day
    basis, because that is the unit the repo already speaks (``iv_rank`` keys on calendar
    DTE) and a caller passing "45" should not have to know whether we divide by 252 or
    365. One year of calendar time is the right year for an option that expires on a
    calendar date, which is the only kind of expiry that exists here.

    At ``dte == 0`` the price is exactly the discounted intrinsic value: there is no time
    value and there never was. Returning a limit rather than raising is deliberate — the
    *price* is well defined at zero time; only the *volatility* is not (see
    :func:`implied_volatility`).
    """
    side = _validate(right)
    if spot <= 0:
        raise BsError(f"spot must be > 0, got {spot}")
    if strike <= 0:
        raise BsError(f"strike must be > 0, got {strike}")
    if sigma < 0:
        raise BsError(f"sigma must be >= 0, got {sigma}")
    if dte < 0:
        raise BsError(f"dte must be >= 0, got {dte}")

    t = dte / 365.0
    if t == 0.0 or sigma == 0.0:
        discounted_spot = spot * math.exp(-dividend * t)
        discounted_strike = strike * math.exp(-rate * t)
        forward_intrinsic = discounted_spot - discounted_strike
        return max(forward_intrinsic, 0.0) if side == "call" else max(-forward_intrinsic, 0.0)

    d1, d2 = _d1_d2(spot, strike, t, rate, sigma, dividend)
    disc_q = math.exp(-dividend * t)
    disc_r = math.exp(-rate * t)
    if side == "call":
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)


def vega(
    *,
    spot: float,
    strike: float,
    dte: float,
    rate: float = DEFAULT_RISK_FREE_RATE,
    sigma: float,
    dividend: float = DEFAULT_DIVIDEND_YIELD,
) -> float:
    """Sensitivity of the price to volatility, per 1.00 of vol (not per vol point).

    Positive for both rights and independent of ``right``, which is the standard result.
    Exposed because it is the *conditioning check* for an inversion: vega near zero is
    the classical reason an IV is not recoverable from a price, and the backfill asserts a
    vega floor rather than discovering the problem as a wrong number.
    """
    t = dte / 365.0
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, t, rate, sigma, dividend)
    return spot * math.exp(-dividend * t) * _norm_pdf(d1) * math.sqrt(t)


def intrinsic_and_floor(
    *,
    spot: float,
    strike: float,
    dte: float,
    rate: float = DEFAULT_RISK_FREE_RATE,
    right: str,
    dividend: float = DEFAULT_DIVIDEND_YIELD,
) -> tuple[float, float]:
    """``(intrinsic_now, no_arbitrage_floor)`` — the two prices an option cannot undercut.

    ``intrinsic_now`` is the undiscounted ``max(S-K, 0)`` style value today;
    ``no_arbitrage_floor`` is the discounted forward intrinsic, which is the *correct*
    lower bound under a carry model. A European call cannot be worth less than
    ``max(S*e^{-qT} - K*e^{-rT}, 0)``; a European put cannot be worth less than
    ``max(K*e^{-rT} - S*e^{-qT}, 0)``. A price below that is a stale or indicative-feed
    artefact, and inverting it would return a number rather than the refusal the data
    deserves.
    """
    side = _validate(right)
    t = max(dte, 0) / 365.0
    forward = spot * math.exp(-dividend * t) - strike * math.exp(-rate * t)
    intrinsic_now = spot - strike if side == "call" else strike - spot
    floor = forward if side == "call" else -forward
    return max(intrinsic_now, 0.0), max(floor, 0.0)


def put_call_parity_residual(
    *,
    spot: float,
    strike: float,
    dte: float,
    call_price: float,
    put_price: float,
    rate: float = DEFAULT_RISK_FREE_RATE,
    dividend: float = DEFAULT_DIVIDEND_YIELD,
) -> float:
    """``C - P - (S·e^{-qT} - K·e^{-rT})``. Zero for a correct European pair.

    Exposed so the test suite can assert parity against the *implementation* rather than
    against a hand-computed constant — a parity assertion is the cheapest possible proof
    that the ``d1``/``d2`` signs and the discount factors are right in all four places,
    and it holds for every input triple instead of the two in a table.
    """
    t = dte / 365.0
    forward = spot * math.exp(-dividend * t) - strike * math.exp(-rate * t)
    return (call_price - put_price) - forward


@dataclass(frozen=True, slots=True)
class IvResult:
    """A solved implied volatility plus the conditioning that makes it trustworthy."""

    iv: float
    #: Vega at the solved vol, per 1.00 of vol. The caller asserts a floor on this.
    vega: float
    #: Model price at the solved vol — the residual the solver achieved, in dollars.
    price_residual: float
    #: Solver iterations used. Reported so a slow solve is visible rather than assumed.
    iterations: int

    def __float__(self) -> float:
        return self.iv


def implied_volatility(
    *,
    price: float,
    spot: float,
    strike: float,
    dte: float,
    right: str,
    rate: float = DEFAULT_RISK_FREE_RATE,
    dividend: float = DEFAULT_DIVIDEND_YIELD,
    min_vol: float = MIN_VOL,
    max_vol: float = MAX_VOL,
    max_iterations: int = MAX_ITERATIONS,
    vol_tol: float = VOL_TOL,
    price_tol: float = PRICE_TOL,
) -> IvResult:
    """Solve for the volatility that reproduces ``price``, or refuse.

    The solver is **bisection with a vega-weighted Newton step**, in that order of
    responsibility. Black-Scholes price is strictly increasing in ``sigma`` for a
    non-degenerate contract, so bisection is unconditionally convergent and cannot run
    away — it is the floor under the answer, not the clever part. The Newton step uses
    the analytic vega, which is accurate to machine precision here and turns a
    ~45-iteration bisection into a handful of steps.

    Convergence accepts on **either** a price residual inside ``price_tol`` or a vol
    interval inside ``vol_tol``, and refuses with :class:`IvNotInvertible` when neither is
    reached or when the price is not an option price at all.

    ``dte == 0`` always raises. Vega is identically zero there, so the model cannot
    distinguish a $5 option priced by 8% vol from one priced by 80%, and any returned
    number would be the solver's own history rather than the market's. This is not a rare
    guard: arm B's frozen DTE band is ``0``, so this is the *expected* case for one of the
    three arms.
    """
    side = _validate(right)
    if spot <= 0:
        raise BsError(f"spot must be > 0, got {spot}")
    if strike <= 0:
        raise BsError(f"strike must be > 0, got {strike}")
    if price is None or not math.isfinite(price) or price <= 0:
        raise IvNotInvertible(f"price must be a positive finite number, got {price!r}")
    if dte <= 0:
        raise IvNotInvertible(
            f"cannot invert vol at dte={dte}: Black-Scholes vega is identically zero at "
            f"expiry, so every volatility prices this contract identically. A returned "
            f"number would be the solver's, not the market's"
        )
    if min_vol <= 0 or max_vol <= min_vol:
        raise BsError(f"require 0 < min_vol < max_vol, got {min_vol}, {max_vol}")

    _, floor = intrinsic_and_floor(
        spot=spot, strike=strike, dte=dte, rate=rate, right=side, dividend=dividend
    )
    if price < floor - price_tol:
        raise IvNotInvertible(
            f"price {price:.6f} is below the no-arbitrage floor {floor:.6f} for "
            f"{side} K={strike} S={spot} dte={dte} — indicative/stale price, not an "
            f"option price; refusing to invert it"
        )

    def price_at(vol: float) -> float:
        return black_scholes_price(
            spot=spot,
            strike=strike,
            dte=dte,
            rate=rate,
            sigma=vol,
            right=side,
            dividend=dividend,
        )

    low, high = min_vol, max_vol
    high_price = price_at(high)
    if price > high_price + price_tol:
        raise IvNotInvertible(
            f"price {price:.6f} exceeds what {max_vol:.0%} vol can explain "
            f"({high_price:.6f}) for {side} K={strike} S={spot} dte={dte} — the price is "
            f"not solvable in [{min_vol}, {max_vol}]"
        )

    vol = low
    iteration = max_iterations
    for _iteration in range(1, max_iterations + 1):
        mid = 0.5 * (low + high)
        model = price_at(mid)
        # Newton step, guarded. Only taken when vega is numerically usable AND the step
        # stays strictly inside the bracket; otherwise plain bisection stands, which is
        # always safe because price is monotone increasing in sigma.
        v = vega(spot=spot, strike=strike, dte=dte, rate=rate, sigma=mid, dividend=dividend)
        if v > 1e-12:
            candidate = mid + (price - model) / v
            if low < candidate < high:
                mid = candidate
                model = price_at(mid)
        if abs(model - price) <= price_tol or (high - low) <= vol_tol:
            vol = mid
            iteration = _iteration
            break
        if model < price:
            low = mid
        else:
            high = mid
        vol = mid
        iteration = _iteration
    else:
        raise IvNotInvertible(
            f"implied vol did not converge in {max_iterations} iterations for "
            f"{side} K={strike} S={spot} dte={dte} price={price}"
        )

    return IvResult(
        iv=vol,
        vega=vega(spot=spot, strike=strike, dte=dte, rate=rate, sigma=vol, dividend=dividend),
        price_residual=price_at(vol) - price,
        iterations=iteration,
    )


def invert_bar_close(
    *,
    close: float,
    volume: float | None = None,
    spot: float,
    strike: float,
    dte: float,
    right: str,
    rate: float = DEFAULT_RISK_FREE_RATE,
    dividend: float = DEFAULT_DIVIDEND_YIELD,
    min_vega: float = 1.0,
    min_volume: float = 1.0,
) -> IvResult:
    """Invert a *daily bar* close into an IV, refusing on thin or unconditioned contracts.

    This is the backfill's actual entry point, and it is deliberately stricter than
    :func:`implied_volatility`: two of the checks belong here rather than in the solver
    because they are facts about the *bar*, not about the model.

    * ``volume < min_volume`` — a contract that did not trade has a close that is a
      quote, a carry-forward, or a single odd lot. Inverting it produces a number that
      looks exactly like an observation and is not one. Absent volume (``None``) is
      treated as **refusing**, not as "unknown, assume fine": an unknown is not evidence.
    * ``vega < min_vega`` — the classical vega-collapse case. Deep ITM puts and deep OTM
      calls at short tenors are priced by vega of essentially zero, and their IV is not
      recoverable from the price at any precision. The default floor of 1.0 (dollars of
      price per 1.00 of vol) is roughly a hundredth of the ATM 90-120-DTE vega measured
      in ``docs/reviews/2026-10-03-iv-backfill-feasibility.md`` §3b (147.18), so it
      refuses the collapsed tail without touching the contracts the gate actually reads.

    Raises :class:`IvNotInvertible` (a :class:`BsError`) on every refusal, so a caller can
    treat "not an observation" as one branch instead of four.
    """
    side = _validate(right)
    if volume is not None and volume < min_volume:
        raise IvNotInvertible(
            f"bar volume {volume} below floor {min_volume} for {side} K={strike} "
            f"dte={dte} — an untraded contract's close is not an observation"
        )
    if volume is None:
        raise IvNotInvertible(
            f"no volume reported for {side} K={strike} dte={dte} — refusing to invert a "
            f"close with unknown liquidity (an unknown is not evidence of liquidity)"
        )
    result = implied_volatility(
        price=close,
        spot=spot,
        strike=strike,
        dte=dte,
        right=side,
        rate=rate,
        dividend=dividend,
    )
    if result.vega < min_vega:
        raise IvNotInvertible(
            f"vega {result.vega:.4f} below floor {min_vega} at solved IV {result.iv:.4f} "
            f"for {side} K={strike} S={spot} dte={dte} — price does not determine IV here"
        )
    return result


__all__ = [
    "BsError",
    "DEFAULT_DIVIDEND_YIELD",
    "DEFAULT_RISK_FREE_RATE",
    "IvNotInvertible",
    "IvResult",
    "MAX_VOL",
    "MIN_VOL",
    "Right",
    "black_scholes_price",
    "implied_volatility",
    "intrinsic_and_floor",
    "invert_bar_close",
    "put_call_parity_residual",
    "vega",
]