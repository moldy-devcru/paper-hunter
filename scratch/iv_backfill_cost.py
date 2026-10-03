"""Request-volume + vega/recoverability arithmetic for the T5 IV backfill spike.

Research scratch. Two questions:
 1. How many symbols/requests does a per-day arm-C band backfill cost?
 2. Is IV actually recoverable from a daily bar CLOSE for the contracts arm C reads
    (ATM-in-band) and for the contract it *trades* (delta>=0.80 deep-ITM)?
"""

from __future__ import annotations

import math
from datetime import date, timedelta

MAX_SYMBOLS = 100  # MAX_OPTION_SYMBOLS_PER_REQUEST, alpaca_client.py:110
HIST_CALLS_PER_MIN = 200  # Basic tier


def band_expiries(session: date, dte_min=90, dte_max=180) -> list[date]:
    """Weekly (Fridays) + 3rd-Friday monthlies landing inside the band, as SPY lists them."""
    out = []
    d = session
    while d <= session + timedelta(days=dte_max):
        if d >= session + timedelta(days=dte_min):
            if d.weekday() == 4 or d.day in (19, 20, 21):
                out.append(d)
        d += timedelta(days=1)
    return out


def symbols_per_day(strikes_around_atm: int) -> tuple[int, int, int]:
    session = date(2026, 10, 5)
    expiries = band_expiries(session)
    symbols = len(expiries) * 2 * strikes_around_atm
    calls = math.ceil(symbols / MAX_SYMBOLS)
    return len(expiries), symbols, calls


def session_count(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def vega(spot: float, strike: float, days: int, iv: float, r=0.04, q=0.013) -> float:
    """dV/d(sigma) per 1.00 of vol — Black-Scholes, continuous q."""
    T = days / 365.0
    if T <= 0:
        return 0.0
    sd = iv * math.sqrt(T)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * T) / sd
    return spot * math.sqrt(T) * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)


def delta_call(spot: float, strike: float, days: int, iv: float, r=0.04, q=0.013) -> float:
    T = days / 365.0
    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    return math.exp(-q * T) * 0.5 * (1 + math.erf(d1 / math.sqrt(2)))


def iv_from_vol_error(spot, strike, days, iv, price_err, **kw) -> float:
    return price_err / vega(spot, strike, days, iv, **kw)


def main() -> None:
    print("== per-day symbol/request cost, arm C band 90-180 DTE ==")
    for k in (1, 3, 5, 11):
        n_exp, syms, calls = symbols_per_day(k)
        days = session_count(date(2024, 2, 1), date(2026, 11, 2))
        total = calls * days
        print(
            f"  {k} strike(s) either side of ATM: {n_exp} expiries x 2 rights x {k} "
            f"= {syms:>3} symbols -> {calls} call(s)/day; "
            f"{days} sessions (2024-02-01..2026-11-02) -> {total} calls "
            f"= {total / HIST_CALLS_PER_MIN:.1f} min of Basic-tier budget"
        )

    print("\n== sessions available ==")
    print(f"  2024-02-01 (Alpaca options floor) -> 2026-11-02 window open: "
          f"{session_count(date(2024, 2, 1), date(2026, 11, 2))}")
    print(f"  2026-09-03 (today) -> 2026-11-02 window open: "
          f"{session_count(date(2026, 9, 3), date(2026, 11, 2))}")

    print("\n== IV recoverability from a daily close (spot 650, r=4%, q=1.3%) ==")
    print("  contract                       delta  dte  vega/$   IV vol-pts per close error")
    cases = (
        ("ATM 120d (what the gate reads)", 650.0, 120, 0.05),
        ("delta-0.80 120d (what C trades)", 604.0, 120, 0.05),
        ("delta-0.80 165d (band top)", 584.0, 165, 0.05),
        ("ATM 45d (arm B band top)", 650.0, 45, 0.05),
        ("delta-0.80 120d, $0.25 err", 604.0, 120, 0.25),
        ("delta-0.80 120d, $1.00 err", 604.0, 120, 1.00),
    )
    for label, strike, days, perr in cases:
        iv = 0.16
        v = vega(650.0, strike, days, iv)
        d = delta_call(650.0, strike, days, iv)
        pts = 100.0 * iv_from_vol_error(650.0, strike, days, iv, perr)
        print(f"  {label:<32} {d:.2f}  {days:>3}  {v:>7.2f}   ${perr:.2f} -> {pts:.3f} pts")


if __name__ == "__main__":
    main()