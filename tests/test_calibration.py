"""The T6 calibration reader (`executor/calibration.py`).

The reader's whole job is to be the ratified formula and nothing else, so these tests
pin the three things that are easy to get wrong and expensive to discover later:

1. the proposed ``N`` is **dimensionless** (a ratio), not a contract count;
2. a session whose ``baseline_days`` is short is **excluded**, with the date and reason
   recorded, because the store is append-only and carries pre-backfill rows;
3. a threshold change restarts the count instead of mixing quantities.

No network, no credentials, no ``/opt`` access: the rows are dicts built here.
"""

from __future__ import annotations

import pytest

from executor.calibration import (
    CalibrationError,
    flow_calibration,
    percentile_linear,
)


def row(
    date: str,
    *,
    call: float = 100.0,
    put: float = 100.0,
    ratio_call: float | None = None,
    ratio_put: float | None = None,
    baseline_days: int = 20,
    lookback: int = 20,
    threshold: float = 3.0,
    total: float | None = None,
) -> dict:
    return {
        "date": date,
        "deep_otm_call_volume": call,
        "deep_otm_put_volume": put,
        "deep_otm_total_volume": total if total is not None else call + put,
        "ratio_call": ratio_call,
        "ratio_put": ratio_put,
        "baseline_days": baseline_days,
        "baseline_lookback_days": lookback,
        "deep_otm_threshold_pct": threshold,
    }


def twenty(ratios: list[float], *, start_day: int = 1) -> list[dict]:
    """20 clean sessions: every ratio equal on both sides, full 20-session baseline."""
    return [
        row(
            f"2026-08-{start_day + i:02d}",
            ratio_call=r,
            ratio_put=r,
            total=1_000.0 + i,
        )
        for i, r in enumerate(ratios)
    ]


# ---------------------------------------------------------------------------
# percentile convention
# ---------------------------------------------------------------------------


def test_percentile_is_the_linear_interpolation_convention() -> None:
    # numpy.percentile([1, 2, 3, 4], 90) == 3.7
    assert percentile_linear([1, 2, 3, 4], 0.90) == pytest.approx(3.7)
    assert percentile_linear([5.0], 0.90) == 5.0


def test_percentile_of_nothing_raises_rather_than_returning_a_number() -> None:
    with pytest.raises(CalibrationError):
        percentile_linear([], 0.9)
    with pytest.raises(CalibrationError):
        percentile_linear([1.0], 1.5)


# ---------------------------------------------------------------------------
# the proposal
# ---------------------------------------------------------------------------


def test_proposed_n_is_the_p90_of_the_ratios_not_of_the_volumes() -> None:
    """A volume P90 would be ~1,000 contracts; the ratio P90 is ~2.2x. They are not
    interchangeable, and T6 multiplies a baseline mean, so only the ratio can be N."""
    rows = twenty([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9,
                   2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.0])
    for r in rows:
        r["deep_otm_total_volume"] = 1_000_000.0  # volume says nothing about the ratio

    proposal = flow_calibration(rows)

    assert proposal.status == "sufficient"
    assert proposal.n == pytest.approx(2.71)  # P90 of the 20 ratios, not of 1e6
    assert proposal.n_call == proposal.n_put == pytest.approx(2.71)
    assert proposal.raw_total_volume_p90 == pytest.approx(1_000_000.0)
    assert proposal.sessions_qualified == 20
    assert "PROPOSAL" in proposal.summary_line()


def test_sides_are_reported_separately_because_the_gate_is_trade_direction() -> None:
    rows = twenty([1.0] * 20)
    for r in rows:
        r["ratio_put"] = 3.0
    proposal = flow_calibration(rows)
    assert proposal.n_call == pytest.approx(1.0)
    assert proposal.n_put == pytest.approx(3.0)
    assert proposal.n == pytest.approx(3.0)  # pooled P90 of a bimodal set
    assert any("trade_direction" in n for n in proposal.notes)


def test_short_baseline_sessions_are_excluded_with_date_and_reason() -> None:
    rows = twenty([1.5] * 20)
    # The pre-backfill rows an append-only store really contains.
    rows.insert(
        0,
        row("2026-07-06", ratio_call=6.0, ratio_put=6.0, baseline_days=0, total=10.0),
    )
    rows.insert(
        1,
        row("2026-07-07", ratio_call=4.0, ratio_put=4.0, baseline_days=3, total=10.0),
    )

    proposal = flow_calibration(rows)

    assert proposal.n == pytest.approx(1.5), "pre-backfill spikes must not move N"
    assert proposal.sessions_considered == 22
    assert proposal.sessions_qualified == 20
    short = [e for e in proposal.excluded if e.reason == "short_baseline"]
    assert {e.date for e in short} == {"2026-07-06", "2026-07-07"}
    assert all(e.side in {"call", "put"} for e in proposal.excluded)


def test_null_ratios_are_excluded_as_no_ratio() -> None:
    rows = twenty([1.5] * 20)[:3] + [row("2026-07-06"), row("2026-07-07")]
    proposal = flow_calibration(rows)
    assert proposal.samples == 6  # 3 sessions x 2 sides
    assert {e.reason for e in proposal.excluded} == {"no_ratio"}
    assert proposal.status == "forming"


def test_a_threshold_change_restarts_the_count_and_is_explained() -> None:
    rows = twenty([1.5] * 20)
    rows.append(
        row("2026-09-25", ratio_call=9.0, ratio_put=9.0, threshold=5.0, total=10.0)
    )
    proposal = flow_calibration(rows)
    # The newest row is the reference threshold, so the 3% rows drop out.
    assert proposal.threshold_pct == 5.0
    assert proposal.n == pytest.approx(9.0)
    assert {e.reason for e in proposal.excluded} == {"threshold_mismatch"}


def test_status_is_forming_until_the_ratified_twenty_sessions_exist() -> None:
    proposal = flow_calibration(twenty([1.5] * 12))
    assert proposal.status == "forming"
    assert not proposal.sufficient
    assert any("of 20 qualifying sessions" in n for n in proposal.notes)


def test_no_rows_is_honest_no_data_not_a_zero() -> None:
    proposal = flow_calibration([])
    assert proposal.status == "no_data"
    assert proposal.n is None
    assert "no N proposed" in proposal.summary_line()


def test_every_stored_session_excluded_says_so_rather_than_guessing() -> None:
    rows = [row("2026-07-06", baseline_days=0), row("2026-07-07", baseline_days=2)]
    proposal = flow_calibration(rows)
    assert proposal.status == "no_qualifying_sessions"
    assert proposal.n is None
    assert proposal.sessions_considered == 2


def test_reader_accepts_pydantic_like_objects_not_just_dicts() -> None:
    class R:
        def __init__(self, d: dict) -> None:
            self.__dict__.update(d)

        def __getitem__(self, name: str):  # sqlite3.Row-alike
            return self.__dict__[name]

    proposal = flow_calibration([R(r) for r in twenty([1.5] * 20)])
    assert proposal.n == pytest.approx(1.5)


def test_outliers_are_surfaced_with_their_volumes_not_filtered_away() -> None:
    rows = twenty([1.5] * 19)
    rows.append(row("2026-09-16", call=20_897.0, ratio_call=61.0, ratio_put=2.0,
                    baseline_days=20, lookback=20))
    # the baseline mean behind that 61x is the tell
    rows[-1]["baseline_call_mean"] = 341.0
    rows[-1]["baseline_put_mean"] = 24_303.0

    proposal = flow_calibration(rows)

    hits = proposal.samples_above(5.0)
    assert [(s.date, s.side, round(s.ratio, 1)) for s in hits] == [("2026-09-16", "call", 61.0)]
    assert hits[0].today_volume == 20_897.0
    assert hits[0].baseline_mean == 341.0
    # Surfaced, not removed: the row is in the sample the percentile is read from.
    assert any(s.ratio == 61.0 for s in proposal.samples_detail)
    # ...and one 61x in 40 samples does not by itself move a P90 (position 35 of 40).
    # The point of showing the volumes is that a reader can see WHICH rows decide the
    # number, which a bare P90 cannot show.
    assert proposal.n == pytest.approx(1.5)
    assert any("completeness question" in n for n in proposal.notes)
