"""T6 calibration reader — the ratified formula, in one place.

`docs/ratification.md` §(c) rules (2026-10-02) that the flow multiplier is calibrated
from the soak's own output and frozen at the first monthly review:

> Given `r_1 … r_20`, the daily ratios of trade-side deep-OTM volume to the trailing
> 20-day baseline mean: `N = P90(r_1 … r_20)`.

Two properties of that sentence are load-bearing, and both were violated before this
module existed:

1. **`N` is dimensionless.** T6 evaluates ``today_volume >= N * baseline_mean``. A
   percentile of *raw contract volume* is a count of contracts; dividing a count by a
   count of contracts is how you get a real number, and comparing a contract count
   against a multiplier is how you get a gate that is trivially passable. The first
   implementation of the calibration panel took the P90 of
   `deep_otm_total_volume`; that number is a good volume chart line and the wrong
   constant. This module computes the ratio, and keeps the volume statistic only as a
   labelled, non-authoritative extra.
2. **The ratio is only the ratified ratio when the baseline behind it is 20 sessions.**
   `flow_baseline` is append-only, so the store contains sessions written before the
   historical backfill existed — their `baseline_days` is short, and their ratio is
   "today vs a 3-day mean", a different quantity from "today vs a 20-day mean". Those
   sessions are *excluded* from the proposal and listed by date and reason, never
   silently averaged in.

Nothing here writes, and nothing here freezes. The output is a **proposal**: the
operator/lead reviews the numbers and the algo mechanics, and the freeze is a
rulebook edit made at the monthly review, not a side effect of running this module.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Sequence
from typing import Any

#: The ratified sample size (`docs/ratification.md` §(c): "PROPOSAL — 20 recorded
#: sessions"). Also the gate's own baseline window, so the calibration is taken against
#: the same distribution the gate is evaluated on.
DEFAULT_SESSIONS_NEEDED = 20

#: Percentile T6's N is read at (ratified: the 90th percentile).
N_PERCENTILE = 0.90

#: A qualified ratio at or above this multiple of its own baseline is surfaced as an
#: outlier in the notes. Not a filter — see :meth:`CalibrationProposal.samples_above`.
OUTLIER_RATIO = 5.0

METHOD = (
    "P90 of per-session trade-side deep-OTM 0DTE volume divided by that session's "
    "trailing 20-session baseline mean (dimensionless, docs/ratification.md §(c)); "
    "linear-interpolation percentile, numpy convention"
)

_RATIO_FIELDS = {"call": "ratio_call", "put": "ratio_put"}


class CalibrationError(ValueError):
    """The reader was handed something it cannot honestly summarise."""


def percentile_linear(values: Sequence[float], q: float = N_PERCENTILE) -> float:
    """Linear-interpolation percentile (the ``numpy.percentile`` convention).

    The two common conventions disagree by a visible amount on a 20-point series, and
    the ratified document names "P90" without naming a convention, so the convention is
    pinned here and stated in :data:`METHOD` rather than left to whichever caller runs
    first. (The stricter "percentage strictly below" rank used by
    :func:`executor.indicators.bandwidth_percentile` is the right rule for *scoring a
    value against a band*; this one is the right rule for *reading a line through a
    distribution*.)
    """
    if not values:
        raise CalibrationError("percentile of an empty series is undefined")
    if not 0.0 <= q <= 1.0:
        raise CalibrationError(f"q must be in [0, 1], got {q}")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


@dataclasses.dataclass(frozen=True, slots=True)
class ExcludedSession:
    """A stored session that is not part of the sample, and why."""

    date: str
    side: str
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "side": self.side,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class Sample:
    """One qualified per-side ratio, with the two volumes it came from.

    The volumes are carried because a ratio on its own cannot be sanity-checked: a
    60x call ratio is either a spectacular squeeze or a session whose chain snapshot
    was truncated, and the volume pair (20,897 today against a 341-contract baseline
    mean) is what tells the two apart.
    """

    date: str
    side: str
    ratio: float
    today_volume: float
    baseline_mean: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "side": self.side,
            "ratio": round(self.ratio, 6),
            "today_volume": self.today_volume,
            "baseline_mean": self.baseline_mean,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class CalibrationProposal:
    """The proposed ``N``, the sample behind it, and every session left out.

    ``n`` is the proposal for the single multiplier the rulebook stores
    (``t6_flow.multiplier.value``). ``n_call`` / ``n_put`` are the same percentile
    computed per side, shown because ``t6_flow.side`` is ``trade_direction``: the rule
    applies whichever side the trade is on, and a single pooled number hides whether
    the two sides behave the same. Pooling the two sides is
    :data:`POOLING_NOTE`'s question for the lead, not a settled mechanic.
    """

    status: str
    n: float | None
    n_call: float | None
    n_put: float | None
    samples: int
    sessions_considered: int
    sessions_qualified: int
    sessions_needed: int
    threshold_pct: float | None
    ratios: tuple[float, ...]
    call_ratios: tuple[float, ...]
    put_ratios: tuple[float, ...]
    excluded: tuple[ExcludedSession, ...]
    raw_total_volume_p90: float | None
    median_ratio: float | None
    min_ratio: float | None
    max_ratio: float | None
    method: str = METHOD
    notes: tuple[str, ...] = ()
    samples_detail: tuple[Sample, ...] = ()

    @property
    def sufficient(self) -> bool:
        return self.status == "sufficient"

    def samples_above(self, ratio: float) -> tuple[Sample, ...]:
        """Qualified samples at or above ``ratio``x baseline, worst first.

        Reported rather than filtered. Dropping outliers from the distribution before
        reading a percentile off it is a *policy* decision about what counts as a real
        session, and that decision belongs to the operator at the freeze — not to a
        reader. The reader's job is to make the outliers visible enough that the
        decision can be made with the numbers in hand.
        """
        hits = [s for s in self.samples_detail if s.ratio >= ratio]
        return tuple(sorted(hits, key=lambda s: s.ratio, reverse=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sufficient": self.sufficient,
            "value": self.n,
            "n_call": self.n_call,
            "n_put": self.n_put,
            "samples": self.samples,
            "sessions_considered": self.sessions_considered,
            "sessions_qualified": self.sessions_qualified,
            "sessions_needed": self.sessions_needed,
            "threshold_pct": self.threshold_pct,
            "method": self.method,
            "ratios": [round(r, 6) for r in self.ratios],
            "call_ratios": [round(r, 6) for r in self.call_ratios],
            "put_ratios": [round(r, 6) for r in self.put_ratios],
            "excluded": [e.to_dict() for e in self.excluded],
            "samples_detail": [s.to_dict() for s in self.samples_detail],
            "raw_total_volume_p90": self.raw_total_volume_p90,
            "median_ratio": self.median_ratio,
            "min_ratio": self.min_ratio,
            "max_ratio": self.max_ratio,
            "notes": list(self.notes),
        }

    def summary_line(self) -> str:
        """One line, the way stdout wants it."""
        if self.n is None:
            return (
                f"T6 calibration: no qualifying sessions "
                f"({self.sessions_considered} stored, {len(self.excluded)} sample(s) "
                f"excluded) — no N proposed"
            )
        return (
            f"T6 calibration: proposed N = {self.n:.3f} "
            f"(call {self.n_call:.3f} / put {self.n_put:.3f}) from {self.samples} ratio(s) "
            f"over {self.sessions_qualified}/{self.sessions_considered} stored sessions "
            f"at {self.threshold_pct:g}% deep OTM; status={self.status} "
            f"(need {self.sessions_needed} sessions) — PROPOSAL, nothing frozen"
        )


POOLING_NOTE = (
    "t6_flow.side is trade_direction, so one multiplier is applied to whichever side the "
    "trade is on; the pooled P90 assumes the two sides share a distribution, which is "
    "shown (n_call / n_put) rather than assumed."
)


def _row_get(row: Any, name: str, default: Any = None) -> Any:
    """Read a field from a journal row, a pydantic model, or a dict.

    The reader is fed from three places — the rehearsal (pydantic
    :class:`~journal.store.FlowBaselineRow`), the UI API (sqlite3.Row), and tests (dicts)
    — so it accepts all three instead of forcing one caller to convert.
    """
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]
    except (TypeError, KeyError, IndexError):
        return getattr(row, name, default)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def flow_calibration(
    rows: Iterable[Any],
    *,
    sessions_needed: int = DEFAULT_SESSIONS_NEEDED,
    percentile: float = N_PERCENTILE,
) -> CalibrationProposal:
    """Propose T6's ``N`` from stored ``flow_baseline`` rows.

    Qualifying criteria, per session and per side:

    * the session is aggregated at the **reference threshold** — the threshold of the
      most recent stored row. A threshold change restarts the count, exactly as
      ``executor.soak.trailing_baseline`` restarts the baseline, because rows at
      different thresholds are a different quantity rather than a noisier one;
    * the side's ratio is not ``NULL`` (no baseline to divide by);
    * ``baseline_days >= baseline_lookback_days`` (the ratio is the ratified 20-session
      ratio, not a short-window one).

    ``rows`` may be in any order. Only the sample is used; nothing is written.
    """
    stored = list(rows)
    excluded: list[ExcludedSession] = []
    if not stored:
        return CalibrationProposal(
            status="no_data",
            n=None,
            n_call=None,
            n_put=None,
            samples=0,
            sessions_considered=0,
            sessions_qualified=0,
            sessions_needed=sessions_needed,
            threshold_pct=None,
            ratios=(),
            call_ratios=(),
            put_ratios=(),
            excluded=(),
            raw_total_volume_p90=None,
            median_ratio=None,
            min_ratio=None,
            max_ratio=None,
            notes=("no flow_baseline rows exist yet; N stays calibration_pending",),
        )

    newest = max(stored, key=lambda r: str(_row_get(r, "date", "")))
    reference = _f(_row_get(newest, "deep_otm_threshold_pct"))
    ordered = sorted(stored, key=lambda r: str(_row_get(r, "date", "")))

    call_ratios: list[float] = []
    put_ratios: list[float] = []
    samples: list[Sample] = []
    qualified_dates: set[str] = set()

    for row in ordered:
        date = str(_row_get(row, "date", "?"))
        threshold = _f(_row_get(row, "deep_otm_threshold_pct"))
        lookback = _f(_row_get(row, "baseline_lookback_days"))
        baseline_days = _f(_row_get(row, "baseline_days")) or 0.0
        needed = lookback or float(sessions_needed)
        for side, field in sorted(_RATIO_FIELDS.items()):
            ratio = _f(_row_get(row, field))
            if ratio is None:
                excluded.append(
                    ExcludedSession(
                        date=date,
                        side=side,
                        reason="no_ratio",
                        detail=(
                            f"{field} is NULL — the session was written before a baseline "
                            f"existed, so there is no ratio to take a percentile of"
                        ),
                    )
                )
                continue
            if reference is not None and threshold is not None and threshold != reference:
                excluded.append(
                    ExcludedSession(
                        date=date,
                        side=side,
                        reason="threshold_mismatch",
                        detail=(
                            f"aggregated at {threshold:g}% deep OTM, reference is "
                            f"{reference:g}% — ratios at different thresholds are a "
                            f"different quantity, not a noisier one"
                        ),
                    )
                )
                continue
            if baseline_days < needed:
                excluded.append(
                    ExcludedSession(
                        date=date,
                        side=side,
                        reason="short_baseline",
                        detail=(
                            f"ratio {ratio:.3f} uses a {baseline_days:g}-session baseline, "
                            f"not {needed:g} — a short-window ratio is not the ratified "
                            f"20-session ratio"
                        ),
                    )
                )
                continue
            (call_ratios if side == "call" else put_ratios).append(ratio)
            samples.append(
                Sample(
                    date=date,
                    side=side,
                    ratio=ratio,
                    today_volume=_f(_row_get(row, f"deep_otm_{side}_volume")) or 0.0,
                    baseline_mean=_f(
                        _row_get(row, f"baseline_{side}_mean")
                    ) or 0.0,
                )
            )
            qualified_dates.add(date)

    if not call_ratios and not put_ratios:
        return CalibrationProposal(
            status="no_qualifying_sessions",
            n=None,
            n_call=None,
            n_put=None,
            samples=0,
            sessions_considered=len(stored),
            sessions_qualified=0,
            sessions_needed=sessions_needed,
            threshold_pct=reference,
            ratios=(),
            call_ratios=(),
            put_ratios=(),
            excluded=tuple(excluded),
            raw_total_volume_p90=None,
            median_ratio=None,
            min_ratio=None,
            max_ratio=None,
            notes=(
                "every stored session is excluded; the reasons above are the whole story",
            ),
        )

    pooled = call_ratios + put_ratios
    n = percentile_linear(pooled, percentile)
    n_call = percentile_linear(call_ratios, percentile) if call_ratios else None
    n_put = percentile_linear(put_ratios, percentile) if put_ratios else None
    raw = [_f(_row_get(r, "deep_otm_total_volume")) for r in ordered]
    raw_ok = [v for v in raw if v is not None]
    enough = len(qualified_dates) >= sessions_needed
    notes = [POOLING_NOTE]
    if not enough:
        notes.append(
            f"{len(qualified_dates)} of {sessions_needed} qualifying sessions: the P90 of "
            f"{len(qualified_dates)} points is the top of a very short series and will "
            f"move materially as sessions land (ratified: the instability of P90 on 20 "
            f"samples is why the freeze is a review, not a script)"
        )
    if excluded:
        notes.append(
            f"{len(excluded)} stored side-sample(s) excluded "
            f"({len(stored) - len(qualified_dates)} of {len(stored)} sessions affected): "
            f"flow_baseline is append-only, so sessions written before the historical "
            f"backfill carry a short baseline"
        )
    outliers = [s for s in samples if s.ratio >= OUTLIER_RATIO]
    if outliers:
        notes.append(
            f"{len(outliers)} of {len(samples)} qualified sample(s) are at or above "
            f"{OUTLIER_RATIO:g}x baseline, which is a completeness question about the "
            f"stored chain snapshots (see samples_above) rather than a market fact on "
            f"its face; a P90 read off this sample is sensitive to exactly these rows"
        )
    return CalibrationProposal(
        status="sufficient" if enough else "forming",
        n=n,
        n_call=n_call,
        n_put=n_put,
        samples=len(pooled),
        sessions_considered=len(stored),
        sessions_qualified=len(qualified_dates),
        sessions_needed=sessions_needed,
        threshold_pct=reference,
        ratios=tuple(pooled),
        call_ratios=tuple(call_ratios),
        put_ratios=tuple(put_ratios),
        excluded=tuple(excluded),
        raw_total_volume_p90=percentile_linear(raw_ok, percentile) if raw_ok else None,
        median_ratio=percentile_linear(pooled, 0.5),
        min_ratio=min(pooled),
        max_ratio=max(pooled),
        notes=tuple(notes),
        samples_detail=tuple(samples),
    )


__all__ = [
    "DEFAULT_SESSIONS_NEEDED",
    "OUTLIER_RATIO",
    "METHOD",
    "N_PERCENTILE",
    "CalibrationError",
    "CalibrationProposal",
    "ExcludedSession",
    "Sample",
    "flow_calibration",
    "percentile_linear",
]
