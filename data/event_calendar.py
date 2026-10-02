"""Event calendar — the T5 hard veto (Phase 1b).

Brief, T5: "no earnings/FOMC day entries — event calendar checked daily, hard veto."
Superseded in part 2026-10-02 (operator): the earnings half is dropped for SPY — see
``checklist.t5_options_chain.event_calendar.earnings_veto``, which records the ruling.
FOMC and CPI remain hard vetoes.
The brief's data-requirements list names the events: "(FOMC, CPI, OPEX): free sources,
static file refreshed weekly." This module is the checker for that static file.

Design notes:
  * The calendar is data, not code. Adding an event is a YAML edit + commit, so the
    veto cannot be silently changed by a code deploy.
  * ``is_event_day`` answers the only question the executor asks. ``veto_reasons``
    exists so a veto can be written into the journal with its cause (a veto without a
    recorded reason is not auditable).
  * Which kinds are hard vetoes is a *rulebook* decision, not a calendar decision —
    it lives in ``checklist.t5_options_chain.event_calendar.veto_kinds``. OPEX is in
    the calendar for awareness but is not a T5 hard veto in the brief.
  * Dates may be marked ``verified: false``. The brief's own sources get revised and
    unscheduled market holidays move CPI releases; an unverified date is surfaced, not
    hidden. See data/events/README.md for the refresh procedure.

Python 3.12+, stdlib + pyyaml.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
EVENTS_DIR = Path(__file__).resolve().parent / "events"

EventKind = Literal["fomc", "cpi", "opex", "earnings"]


class EventError(ValueError):
    """Raised when a calendar file cannot be loaded or contains nonsense."""


class CalendarEvent(BaseModel):
    """One dated event.

    ``veto`` is per-event rather than per-kind because severity genuinely differs:
    an FOMC decision is a hard veto, a monthly OPEX is not (it is expiry mechanics).
    The default comes from the kind so a calendar author does not have to repeat it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: dt.date
    kind: EventKind
    label: str
    release_time_et: str | None = None
    veto: bool | None = None
    verified: bool = True
    source: str | None = None
    note: str | None = None

    @classmethod
    def _default_veto(cls, kind: str) -> bool:
        # The brief's T5 names earnings and FOMC as hard-veto days; CPI is a macro
        # release with the same IV-crush/rerating effect and is listed in the same
        # calendar. OPEX is listed for awareness, not as a veto.
        #
        # RULED 2026-10-02 (operator): earnings dropped for SPY — a single component's
        # print does not move index IV regime. The kind stays in the Literal because a
        # calendar may still record one for awareness; it just no longer vetoes by
        # default, and the rulebook's veto_kinds say so independently of this default.
        return kind in {"fomc", "cpi"}

    def is_veto(self) -> bool:
        return self.veto if self.veto is not None else self._default_veto(self.kind)

    @property
    def day(self) -> dt.date:
        return self.date


class CalendarFile(BaseModel):
    """One calendar file's worth of events. Usually one quarter per file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quarter: str | None = Field(default=None, description="e.g. 2026-Q4")
    updated: dt.date | None = None
    notes: list[str] = Field(default_factory=list)
    events: list[CalendarEvent]

    def veto_events(self) -> list[CalendarEvent]:
        return [e for e in self.events if e.is_veto()]


class EventCalendar:
    """Merged view over every calendar file in a directory.

    Duplicate dates are allowed on purpose: a FOMC day can also be an OPEX-adjacent
    expiry, and the journal should record both causes.
    """

    def __init__(self, files: list[CalendarFile], *, source_dir: Path | None = None) -> None:
        self.files = files
        self.source_dir = source_dir
        self._events = sorted((e for f in files for e in f.events), key=lambda e: (e.date, e.kind))

    # -- construction ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> EventCalendar:
        """Load one calendar file (YAML or JSON — YAML is a JSON superset)."""
        return cls([load_calendar_file(path)], source_dir=Path(path).parent)

    @classmethod
    def load_dir(cls, directory: str | Path | None = None) -> EventCalendar:
        """Load every ``*.yaml``/``*.yml``/``*.json`` calendar in a directory."""
        d = Path(directory) if directory is not None else EVENTS_DIR
        if not d.is_dir():
            raise EventError(f"event calendar directory not found: {d}")
        paths = sorted(p for p in d.iterdir() if p.suffix in {".yaml", ".yml", ".json"})
        if not paths:
            raise EventError(f"no calendar files in {d} — T5 veto would silently never fire")
        files = [load_calendar_file(p) for p in paths]
        if not any(f.events for f in files):
            raise EventError(
                f"no events in any calendar file under {d} — is_event_day would return False "
                "forever, which is exactly the failure T5 exists to prevent"
            )
        return cls(files, source_dir=d)

    # -- queries ---------------------------------------------------------------

    @property
    def events(self) -> list[CalendarEvent]:
        return list(self._events)

    def events_on(self, day: dt.date | str) -> list[CalendarEvent]:
        d = _as_date(day)
        return [e for e in self._events if e.date == d]

    def is_event_day(
        self, day: dt.date | str, *, veto_kinds: list[str] | None = None
    ) -> bool:
        """True if the day carries any veto-class event (T5 hard veto).

        ``veto_kinds`` narrows to the rulebook's declared veto kinds (default: every
        event's own veto flag, which is how the calendar files say what they are).
        """
        return bool(self.veto_events_on(day, veto_kinds=veto_kinds))

    def veto_events_on(
        self, day: dt.date | str, *, veto_kinds: list[str] | None = None
    ) -> list[CalendarEvent]:
        d = _as_date(day)
        allowed = None if veto_kinds is None else set(veto_kinds)
        return [
            e
            for e in self.events_on(d)
            if e.is_veto() and (allowed is None or e.kind in allowed)
        ]

    def veto_reasons(self, day: dt.date | str, *, veto_kinds: list[str] | None = None) -> list[str]:
        """Journal-ready strings explaining a veto ('T5 event veto: fomc 2026-10-28 FOMC')."""
        return [
            f"T5 event veto: {e.kind} {e.date.isoformat()} {e.label}"
            + ("" if e.verified else " [UNVERIFIED — see data/events/README.md]")
            for e in self.veto_events_on(day, veto_kinds=veto_kinds)
        ]

    def unverified(self) -> list[CalendarEvent]:
        """Events still carrying ``verified: false`` — reconcile before first trade."""
        return [e for e in self._events if not e.verified]

    def coverage(self) -> tuple[dt.date | None, dt.date | None]:
        """(first_date, last_date) covered by the loaded calendar."""
        if not self._events:
            return (None, None)
        return (self._events[0].date, self._events[-1].date)

    def __len__(self) -> int:
        return len(self._events)


def load_calendar_file(path: str | Path) -> CalendarFile:
    """Parse one calendar file. Raises ``EventError`` with the file name on failure."""
    p = Path(path)
    if not p.is_file():
        raise EventError(f"calendar file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:
        # yaml raises YAMLError for most parse problems but plain ValueError for some
        # scalar constructors (e.g. 'date: 2026-13-45' -> timestamp). Both are "bad file".
        raise EventError(f"{p}: not valid YAML/JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EventError(f"{p}: top level must be a mapping")
    try:
        return CalendarFile.model_validate(data)
    except Exception as exc:
        raise EventError(f"{p} is not a valid calendar file:\n{exc}") from exc


def _as_date(day: dt.date | str) -> dt.date:
    if isinstance(day, dt.datetime):  # a datetime is-a date; refuse it silently
        raise EventError("pass a date or 'YYYY-MM-DD' string, not a datetime")
    if isinstance(day, str):
        try:
            return dt.date.fromisoformat(day)
        except ValueError as exc:
            raise EventError(f"bad date {day!r}: expected YYYY-MM-DD") from exc
    return day


__all__ = [
    "EVENTS_DIR",
    "CalendarEvent",
    "CalendarFile",
    "EventCalendar",
    "EventError",
    "load_calendar_file",
]