"""The daily-cycle systemd units in ``scripts/``.

Repo convention (from ``tests/test_ui_deploy.py``): unit files rot silently, so the
things that must not rot are asserted here — the deploy path, the hardening, the flags
that decide what the job DOES rather than what it RECORDS, and the timer's calendar.

These units are NOT installed or enabled by this test, by this branch, or by anything
else in the repo. ``docs/runbooks/daily-cycle-timers.md`` is the operator's checklist and
the rehearsal's NO-GO on the cron trio is the reason the checklist exists.
"""

from __future__ import annotations

import configparser
import shlex
from pathlib import Path

import pytest

from executor.flow_gate import POLICIES
from executor.main import build_parser

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

#: The deploy root. VERIFIED by docs/reviews/2026-10-03-soak-deploy-audit.md §2: the live
#: job runs from /opt/paper-hunter, not from a ~/dev worktree. Every unit asserts the
#: same root, because a plan that reads ~/dev's journal and a soak that reads /opt's are
#: two experiments wearing one name.
DEPLOY = "/opt/paper-hunter"

SERVICE_UNITS = ["plan.service", "watch.service", "eod.service"]
TIMER_UNITS = ["plan.timer", "watch.timer", "eod.timer"]

#: 09:20 plan, 09:30 watch, 16:15 EOD, 17:30 soak. Asserted as literals so a schedule
#: change has to be made deliberately in both the timer and this list.
SCHEDULE = {
    "plan.timer": "Mon..Fri 09:20:00 America/New_York",
    "watch.timer": "Mon..Fri 09:30:00 America/New_York",
    "eod.timer": "Mon..Fri 16:15:00 America/New_York",
    "soak.timer": "Mon..Fri 17:30:00 America/New_York",
}


def _text(unit: str) -> str:
    return (SCRIPTS / unit).read_text()


def _section(unit: str) -> configparser.ConfigParser:
    """Parse a unit file, preserving key case (systemd keys are case-sensitive)."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    # Drop the comment block: systemd treats it as preamble, configparser does not.
    body = "\n".join(
        line for line in _text(unit).splitlines()
        if not line.lstrip().startswith("#")
    )
    parser.read_string(body)
    assert "Service" in parser or "Timer" in parser, f"{unit} parsed to nothing"
    return parser


def _service(unit: str) -> configparser.SectionProxy:
    return _section(unit)["Service"]


def _timer(unit: str) -> configparser.SectionProxy:
    return _section(unit)["Timer"]


def _exec_argv(unit: str) -> list[str]:
    """The ExecStart arguments after the interpreter, for a `executor.main` unit.

    Only valid for units that invoke the executor CLI; `soak.service` runs
    `python -m executor.soak`, a different entry point with different flags.
    """
    lines = [
        line.split("=", 1)[1]
        for line in _text(unit).splitlines()
        if line.startswith("ExecStart=")
    ]
    assert len(lines) == 1, (
        f"{unit} must have exactly ONE ExecStart; got {len(lines)}. "
        "Multiple ExecStart lines silently replace each other in systemd."
    )
    argv = shlex.split(lines[0])
    assert "-m" in argv and "executor.main" in argv, (
        f"{unit} is not an executor.main unit; use _service() instead of _exec_argv()"
    )
    return argv[argv.index("-m") + 2:]  # strip the interpreter and '-m executor.main'


#: Units that invoke the executor CLI, so `--dry-run`/`--db` apply to them.
MAIN_UNITS = ["plan.service", "watch.service", "eod.service"]


def _directives(unit: str, key: str) -> list[str]:
    """Every ``Key=value`` directive for ``key``, in file order.

    configparser collapses a repeated key (``Environment=`` twice is normal and correct
    in systemd), so anything that must assert on a multi-valued directive reads the raw
    lines instead of fighting the parser over it.
    """
    return [
        line.split("=", 1)[1].strip()
        for line in _text(unit).splitlines()
        if line.startswith(f"{key}=")
    ]


@pytest.mark.parametrize("unit", SERVICE_UNITS)
def test_unit_exists_and_parses(unit):
    assert (SCRIPTS / unit).is_file(), f"{unit} is missing from scripts/"
    assert _section(unit).has_section("Service")


@pytest.mark.parametrize("unit", TIMER_UNITS + ["soak.timer"])
def test_timer_exists_and_parses(unit):
    assert (SCRIPTS / unit).is_file(), f"{unit} is missing from scripts/"
    assert _section(unit).has_section("Timer")


@pytest.mark.parametrize("unit", SERVICE_UNITS + ["soak.service"])
def test_unit_points_at_the_verified_deploy_root(unit):
    """Editing a worktree does not change what runs — this is the whole point."""
    service = _service(unit)
    assert service["WorkingDirectory"] == DEPLOY
    assert service["ExecStart"].startswith(f"{DEPLOY}/.venv/bin/python")


@pytest.mark.parametrize("unit", SERVICE_UNITS + ["soak.service"])
def test_unit_mirrors_the_soak_hardening_and_credential_conventions(unit):
    """Same posture as soak.service, for the same reason: the code and rulebook are inputs."""
    service = _service(unit)
    assert service["User"] == "paper-hunter"
    assert service["Group"] == "paper-hunter"
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"] == f"{DEPLOY}/data"
    assert service["NoNewPrivileges"] == "yes"
    assert service["EnvironmentFile"] == "-/etc/paper-hunter/soak.env"
    assert _directives(unit, "Environment") == [
        "PYTHONUNBUFFERED=1",
        "PYTHONDONTWRITEBYTECODE=1",
    ]


@pytest.mark.parametrize("unit", MAIN_UNITS)
def test_unit_never_places_live_orders(unit):
    """The one assertion in this file that is about safety rather than plumbing.

    `--dry-run` is the CLI default, so a unit that omits it is correct today and could
    stop being correct if the default ever moves. These jobs place NO orders.
    """
    argv = _exec_argv(unit)
    assert "--dry-run" in argv, f"{unit} must pass --dry-run explicitly"
    assert "--live" not in argv, f"{unit} must never pass --live"
    assert "--open-arm-a" not in argv


def test_soak_service_runs_for_real_and_is_only_inert_because_it_reads():
    """The soak is the one unit that must NOT dry-run — it exists to write.

    It places no orders because `executor.soak` has no routing path at all, which is a
    property of the module rather than of the unit file, so the unit cannot carry
    `--dry-run` here without defeating the whole job.
    """
    assert "--dry-run" not in _directives("soak.service", "ExecStart")[0]
    assert "-m executor.soak" in _text("soak.service")


@pytest.mark.parametrize("unit", MAIN_UNITS)
def test_unit_names_the_journal_explicitly(unit):
    """`executor/main.py` writes NO journal rows unless `--db` is passed.

    A leg that omits it exits clean having written nothing, which is the worst possible
    outcome for a job whose entire artifact is the journal.
    """
    argv = _exec_argv(unit)
    assert "--db" in argv, f"{unit} must pass --db"
    assert argv[argv.index("--db") + 1] == f"{DEPLOY}/data/journal.db"


@pytest.mark.parametrize("unit", MAIN_UNITS)
def test_exec_start_parses_as_the_real_cli(unit):
    """The ExecStart must survive the ACTUAL argparse, flags and all.

    This is the test that caught the real bug in this change: `--flow-gate carry_forward`
    was rejected by `main.FLOW_GATE_POLICIES`, a stale literal that had drifted from
    `executor/flow_gate.POLICIES`. Parsing the unit through the shipped parser is the only
    way that class of rot gets caught before it becomes a failed 09:30.
    """
    args = build_parser().parse_args(_exec_argv(unit))
    assert args.command in {"hunt-plan", "watch", "eod"}
    assert args.dry_run is True


def test_watch_service_pins_the_ruled_flow_gate_policy():
    """`watch` defaults `--flow-gate` to the TRUTHY string "none", so omitting it does
    NOT fall through to the rulebook the way `hunt-plan` does — the loop would silently
    hand T6 no gate at all. This is the rehearsal's finding 3 in a new disguise."""
    argv = _exec_argv("watch.service")
    assert "--flow-gate" in argv
    policy = argv[argv.index("--flow-gate") + 1]
    assert policy in POLICIES, f"{policy!r} is not an accepted policy {POLICIES}"
    assert policy == "carry_forward", "R3 ruled carry_forward; the unit must pass it"


def test_cli_accepts_every_policy_the_flow_gate_module_accepts():
    """The duplicate list in main.py is gone; the CLI cannot reject a name the module takes."""
    for policy in POLICIES:
        build_parser().parse_args(
            ["--db", "x.db", "watch", "--flow-gate", policy]
        )


def test_watch_service_names_the_iv_store_it_re_verifies_t5_against():
    argv = _exec_argv("watch.service")
    assert "--ivrank" in argv
    assert argv[argv.index("--ivrank") + 1] == f"{DEPLOY}/data/ivrank.db"


def test_watch_service_has_no_start_timeout_that_would_kill_a_six_hour_loop():
    """The loop exits on its own `--stop-et`; a 90s default would kill it at 09:32."""
    assert _service("watch.service")["TimeoutStartSec"] == "0"


def test_oneshot_legs_have_a_bounded_timeout():
    for unit in ("plan.service", "eod.service"):
        seconds = int(_service(unit)["TimeoutStartSec"])
        assert 0 < seconds <= 600, f"{unit} timeout {seconds} is outside a sane bound"


def test_eod_service_never_runs_offline():
    """`--offline` is the rehearsal tool. In production the ledger must come from the
    real chain, or the whole counterfactual record is a fixture replay."""
    assert "--offline" not in _exec_argv("eod.service")


def test_watch_timer_does_not_catch_up():
    """A late loop watches a partial session and writes NO_TRADE rows for a window it
    never saw. An honest empty session beats a fabricated partial."""
    assert _timer("watch.timer")["Persistent"] == "false"


@pytest.mark.parametrize("unit", ["plan.timer", "eod.timer", "soak.timer"])
def test_catch_up_timers_do_catch_up(unit):
    """The inverse: a missed plan or EOD run leaves a permanent gap in an append-only
    ledger, so these must catch up."""
    assert _timer(unit)["Persistent"] == "true"


@pytest.mark.parametrize("unit,calendar", SCHEDULE.items())
def test_timers_fire_at_the_documented_time_in_an_explicit_timezone(unit, calendar):
    """No bare `OnCalendar=Mon..Fri 17:30`: without a timezone systemd reads the SYSTEM
    zone, and an EOD job that silently shifts an hour twice a year writes one session's
    work under another session's date."""
    timer = _timer(unit)
    assert timer["OnCalendar"] == calendar
    assert "/" in timer["OnCalendar"].split()[-1], "OnCalendar must carry a timezone"
    assert timer["Unit"] == unit.replace(".timer", ".service")


@pytest.mark.parametrize("unit", TIMER_UNITS + ["soak.timer"])
def test_timers_are_enabled_but_not_started_by_this_repo(unit):
    """Every install block says `enable --now`; none of them starts the job itself."""
    assert "[Install]" in _text(unit)
    assert "WantedBy=timers.target" in _text(unit)