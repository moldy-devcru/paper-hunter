"""Smoke tests for the gate base-rate measurement.

Deliberately narrow. The expensive correctness check — that the O(n) fast path agrees with
the production ``snapshot_builder.build_snapshot`` — already runs inside
``scripts/measure_gate_baserates.py`` on every invocation via ``verify_against_builder``,
and it is what produced the numbers in ``docs/reviews/2026-10-03-gate-base-rates.md``.
Duplicating it here would double a multi-minute test for no extra coverage.

What these cover instead are the cheap invariants that would silently corrupt a base-rate
table if they broke: the T3 OR-group resolution, the rulebook-granularity collapsing that
keeps T3 from being double-counted, the fact that a gate the arm does not carry is
excluded rather than counted as a veto, and the window projection's algebra. Each of
those is a place where a plausible-looking implementation returns a confidently wrong
table, and none of them needs market data to catch.
"""

from __future__ import annotations

import datetime as dt

import pytest

from analysis.gate_baserate import (
    SessionOutcome,
    effective_blocking,
    expected_per_year,
    group_status,
    joint_rate,
    joint_rate_excluding,
    marginal_contribution,
)


def _outcome(
    session: str = "2026-01-05",
    *,
    arm: str = "C",
    statuses: dict[str, str] | None = None,
    blocking: set[str] | None = None,
) -> SessionOutcome:
    """A hand-built session, so these tests never touch market data.

    ``blocking`` is the arm's carry set from ``checklist.for_arm`` — the gates this arm
    evaluates. Anything absent from it is SKIPPED for that arm, which is exactly the
    distinction ``effective_blocking`` has to preserve.
    """
    carry = blocking if blocking is not None else set()
    resolved = {cid: ("SKIPPED" if cid not in carry else "PASS") for cid in carry}
    resolved.update(statuses or {})
    return SessionOutcome(
        session=dt.date.fromisoformat(session),
        signal_bar=dt.date.fromisoformat(session),
        arm=arm,
        direction="call",
        statuses=resolved,
        blocking={cid: True for cid in carry},
        fire=False,
    )


def test_group_status_or_semantics() -> None:
    """T3 passes if *either* arm passes — the evaluator ORs them, and T3 as a group is
    not an id the evaluator emits, so reporting it per-arm would understate the gate."""
    assert group_status({"T3a": "PASS", "T3b": "FAIL"}) == "PASS"
    assert group_status({"T3a": "FAIL", "T3b": "PASS"}) == "PASS"
    assert group_status({"T3a": "FAIL", "T3b": "FAIL"}) == "FAIL"
    assert group_status({"T3a": "FAIL", "T3b": "PENDING"}) == "FAIL"
    assert group_status({"T3a": "PENDING", "T3b": "PENDING"}) == "PENDING"


def test_effective_blocking_collapses_t3_and_drops_uncarried() -> None:
    """The T3 arms become one entry, and a gate the arm does not carry is excluded
    rather than reported as a veto — 'arm C is blocked by T6' is a category error."""
    outcome = _outcome(
        statuses={"T1": "PASS", "T2": "PASS", "T3a": "FAIL", "T3b": "PASS", "T6": "SKIPPED"},
        blocking={"T1", "T2", "T3a", "T3b"},
    )
    # T3b passed, so the OR group resolved to PASS and is not a blocker — it still
    # appears as a carried gate via T3a/T3b in the carry set, but not in the blockers.
    assert effective_blocking(outcome) == ["T1", "T2"]

    both_fail = _outcome(
        session="2026-01-06",
        statuses={"T1": "PASS", "T3a": "FAIL", "T3b": "FAIL"},
        blocking={"T1", "T3a", "T3b"},
    )
    assert effective_blocking(both_fail) == ["T1", "T3"]  # collapsed to one entry

    dropped = _outcome(
        arm="B",
        statuses={"T1": "PASS", "T6": "PENDING"},
        blocking={"T1", "T6"},
    )
    assert effective_blocking(dropped) == ["T1", "T6"]
    assert effective_blocking(dropped, drop=("T6",)) == ["T1"]


def test_marginal_excludes_uncarried_condition() -> None:
    """T6 is not carried by arm C, so it must report zero rather than appearing as the
    sole blocker on every session — which would read as 'T6 is the tightest gate'."""
    outcomes = [
        _outcome(statuses={"T1": "PASS", "T6": "SKIPPED"}, blocking={"T1"}),
        _outcome(session="2026-01-06", statuses={"T1": "FAIL", "T6": "SKIPPED"}, blocking={"T1"}),
    ]
    marginal = marginal_contribution(outcomes, ("T1", "T6"))
    assert marginal["T6"]["not_applicable"] == 1.0
    assert marginal["T6"]["sole_veto_sessions"] == 0.0
    assert marginal["T1"]["sole_veto_sessions"] == 1.0
    assert marginal["T1"]["fires_if_removed"] == 1.0


def test_marginal_flags_always_pending() -> None:
    """A gate that blocks for lack of data must be flagged, so a reader sorting by
    sole-veto count cannot mistake 'uncalibrated' for 'tightest constraint'."""
    outcomes = [
        _outcome(arm="B", statuses={"T1": "PASS", "T6": "PENDING"}, blocking={"T1", "T6"}),
        _outcome(
            session="2026-01-06", arm="B",
            statuses={"T1": "FAIL", "T6": "PENDING"}, blocking={"T1", "T6"},
        ),
    ]
    marginal = marginal_contribution(outcomes, ("T1", "T6"), exclude=("T6",))
    assert marginal["T6"]["not_applicable"] == 1.0  # dropped from the counterfactual

    included = marginal_contribution(outcomes, ("T6",))
    # On the only session where T1 passed, T6 was pending — so every evaluated session
    # was blocked by missing data. That is the flag's job: it separates 'blocked for lack
    # of data' from 'evaluated and said no'.
    assert included["T6"]["always_pending"] == 1.0
    assert included["T6"]["pending_sole_veto_sessions"] == 1.0
    assert included["T6"]["sole_veto_sessions"] == 1.0


def test_marginal_detects_genuinely_always_pending() -> None:
    """With only T6 carrying, every session is 'always pending' — the case that must be
    flagged rather than silently counted as a tight constraint."""
    outcomes = [
        _outcome(arm="B", statuses={"T6": "PENDING"}, blocking={"T6"}),
        _outcome(session="2026-01-06", arm="B", statuses={"T6": "PENDING"}, blocking={"T6"}),
    ]
    marginal = marginal_contribution(outcomes, ("T6",))
    assert marginal["T6"]["always_pending"] == 1.0
    assert marginal["T6"]["sole_veto_sessions"] == 2.0


def test_joint_rate_and_exclusion() -> None:
    """Dropping a gate may only ever *raise* the joint rate, and never above 1."""
    outcomes = [
        _outcome(statuses={"T1": "PASS", "T6": "PENDING"}, blocking={"T1", "T6"}),
        _outcome(
            session="2026-01-06",
            statuses={"T1": "FAIL", "T6": "PASS"},
            blocking={"T1", "T6"},
        ),
    ]
    assert joint_rate(outcomes)["joint_rate"] == 0.0  # T6 pending blocks the first
    assert joint_rate_excluding(outcomes, ("T6",))["fires"] == 1.0
    assert joint_rate_excluding(outcomes, ())["joint_rate"] == 0.0


def test_expected_per_year_algebra() -> None:
    """Projections are simple products, and the empty-window probability is the
    complement of at-least-one — they must not disagree."""
    out = expected_per_year(0.001, sessions_per_year=252, target_sessions=60, target_months=3)
    assert out["expected_days_per_year"] == pytest.approx(0.252)
    assert out["expected_days_per_window"] == pytest.approx(0.06)
    assert out["expected_trades_per_window"] == pytest.approx(0.06)
    assert out["p_zero_in_window"] == pytest.approx(0.999**60)
    assert out["p_zero_in_window"] + out["p_at_least_one_in_window"] == pytest.approx(1.0)

    empty = expected_per_year(0.0, target_sessions=60)
    assert empty["p_zero_in_window"] == pytest.approx(1.0)
    assert empty["p_at_least_one_in_window"] == pytest.approx(0.0)


def test_expected_per_year_handles_certainty() -> None:
    """Degenerate rates must not blow up ``(1 - p) ** n`` with a negative base."""
    assert expected_per_year(1.0)["p_zero_in_window"] == pytest.approx(0.0)