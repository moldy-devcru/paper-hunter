"""The deployment artifacts: the systemd unit and the operator runbook.

These two files are the only part of the UI that nobody's test suite exercises by
importing them, which is exactly why they need assertions. A unit file that lost
``ProtectSystem=strict`` still works — it is the *wrong* thing that still works, and
nothing about serving a page would tell you. A runbook that stopped explaining how to
reach the service is also silent until someone is standing at the machine at 23:00.

What is asserted here:

* the unit exists, parses as an INI, and carries the hardening directives soak.service
  established as the baseline — parsed with ``configparser``, not grepped, so a directive
  that moved into a comment does not pass;
* the bind/port/exec line matches what the runbook tells the operator to open, and the
  unit binds all interfaces rather than loopback (a LAN tool on 127.0.0.1 serves nobody);
* the runbook has the sections an operator needs at 23:00: how to reach it, how to
  restart it, the read-only layers, the cache rebuild, what "degraded" means, and the
  no-auth boundary statement.

Everything is offline and non-invasive: the unit is parsed, never installed, and no port
is bound. Nothing here starts a server.
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT = REPO / "scripts" / "paper-hunter-ui.service"
SOAK_UNIT = REPO / "scripts" / "soak.service"
RUNBOOK = REPO / "docs" / "ui-runbook.md"

PORT = "8478"
BIND = "0.0.0.0"

#: Directives soak.service sets because a bug in the soak must not reach the rulebook.
#: The UI process has the same blast radius concern (same repo, same data dir), so the
#: baseline is inherited rather than reinvented.
INHERITED_HARDENING = (
    "ProtectSystem",
    "ProtectHome",
    "PrivateTmp",
    "NoNewPrivileges",
)

#: The one writable path, and the only reason the terminal needs one: the bar cache.
WRITABLE_PATH = "/opt/paper-hunter/data"

#: Section headings the runbook must keep, by a phrase that identifies the section
#: rather than pinning its exact wording.
REQUIRED_RUNBOOK_TOPICS = {
    "how to reach it": ("hostname -I", f":{PORT}", BIND),
    "how to restart it": ("systemctl restart paper-hunter-ui.service",),
    "read-only layers": ("mode=ro", "non-GET", "net.js"),
    "barcache rebuild": ("barcache.db", "rm /opt/paper-hunter/data/barcache.db"),
    "degraded health": ("degraded", "present", "notes"),
    "no-auth boundary": ("no authentication", "auth", "first"),
}


def unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string(UNIT.read_text(encoding="utf-8"))
    return parser


def value(parser: configparser.ConfigParser, key: str) -> str:
    return parser["Service"].get(key, "")


# ---------------------------------------------------------------------------
# 1. the unit exists and parses
# ---------------------------------------------------------------------------


def test_the_ui_unit_exists_and_parses() -> None:
    assert UNIT.exists(), "scripts/paper-hunter-ui.service is missing"
    parser = unit()
    assert parser.has_section("Service")
    assert parser["Unit"]["Description"].strip(), "a unit with no Description is a mystery unit"
    assert parser["Install"]["WantedBy"].strip() == "multi-user.target"


def test_the_unit_documents_its_own_install_commands() -> None:
    """soak.service's best feature: the header says exactly how to install it.

    A deployer should never have to reconstruct the cp/daemon-reload/enable sequence
    from tribal knowledge, and the sequence is the part that is easy to get subtly wrong.
    """
    text = UNIT.read_text(encoding="utf-8")
    assert text.startswith("#"), "the unit must carry a header comment block"
    for command in (
        "sudo cp scripts/paper-hunter-ui.service /etc/systemd/system/",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable --now paper-hunter-ui.service",
    ):
        assert command in text, f"install instructions are missing: {command}"
    # The smoke test must be from a SECOND host, not just loopback: a 127.0.0.1 bind
    # passes every local check and serves nobody.
    assert "curl" in text and "LAN-IP" in text


def test_the_unit_serves_uvicorn_from_the_repo_venv_on_the_documented_port() -> None:
    exec_start = value(unit(), "ExecStart")
    assert "/opt/paper-hunter/.venv/bin/python -m uvicorn" in exec_start
    assert f"--host {BIND}" in exec_start
    assert f"--port {PORT}" in exec_start
    # A reload watcher would change the app under the operator mid-read.
    assert "--reload" not in exec_start
    assert value(unit(), "WorkingDirectory") == "/opt/paper-hunter"


def test_the_unit_binds_the_lan_not_loopback() -> None:
    """A LAN tool on 127.0.0.1 is a server nobody can reach, and it looks healthy."""
    assert "--host 127.0.0.1" not in value(unit(), "ExecStart")
    assert f"--host {BIND}" in value(unit(), "ExecStart")


# ---------------------------------------------------------------------------
# 2. hardening inherited from soak.service
# ---------------------------------------------------------------------------


def test_the_unit_inherits_soaks_hardening_baseline() -> None:
    parser = unit()
    for directive in INHERITED_HARDENING:
        assert parser["Service"].get(directive) in ("yes", "strict"), (
            f"{directive} is not set the way soak.service sets it"
        )


def test_protectsystem_is_strict_with_only_the_data_dir_writable() -> None:
    assert value(unit(), "ProtectSystem") == "strict"
    # ReadWritePaths narrows it; a bare ProtectSystem=strict with no writable path would
    # make the bar cache unwritable and the terminal would 500 on every request.
    writable = [path.strip() for path in value(unit(), "ReadWritePaths").split() if path.strip()]
    assert writable == [WRITABLE_PATH], (
        f"the only writable path must be the bar cache dir, got {writable}"
    )
    # A second ReadWritePaths line, or a root-level one, would widen the surface back up
    # without changing the first.
    assert "/" not in writable, "ReadWritePaths=/ would undo ProtectSystem=strict"
    assert value(unit(), "ReadWritePaths").count(WRITABLE_PATH) == 1


def test_the_unit_runs_as_the_unprivileged_service_user() -> None:
    assert value(unit(), "User") == "paper-hunter"
    assert value(unit(), "Group") == "paper-hunter"
    # Root would make every other hardening directive decorative.
    assert value(unit(), "User") != "root"


def test_the_unit_restarts_on_failure_and_logs_to_journal() -> None:
    assert value(unit(), "Restart") == "on-failure"
    assert value(unit(), "StandardOutput") == "journal"
    assert value(unit(), "StandardError") == "journal"


def test_the_unit_credential_file_is_optional_and_documented() -> None:
    """`-` prefix = optional. The server needs no credentials, and the file must not be
    able to make the unit fail to start when the soak has never been deployed."""
    env_file = value(unit(), "EnvironmentFile")
    assert env_file == "-/etc/paper-hunter/soak.env", (
        f"EnvironmentFile must be optional, got {env_file!r}"
    )
    text = UNIT.read_text(encoding="utf-8")
    assert "ALPACA_PAPER_KEY" in text and "ALPACA_PAPER_SECRET" in text, (
        "the names of the variables it would carry must be in the header"
    )
    assert "None is required" in text or "NOT NEEDED" in text, (
        "the header must say the terminal needs no credentials to run"
    )


# ---------------------------------------------------------------------------
# 3. the runbook
# ---------------------------------------------------------------------------


def test_the_runbook_exists_and_names_the_unit() -> None:
    assert RUNBOOK.exists()
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "paper-hunter-ui.service" in text
    assert "ui-design.md" in text, "the runbook should point at the design spec"


def test_the_runbook_covers_every_required_topic() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for topic, phrases in REQUIRED_RUNBOOK_TOPICS.items():
        for phrase in phrases:
            assert phrase in text, f"the runbook does not cover {topic}: missing {phrase!r}"


def test_the_runbook_states_the_no_auth_boundary_and_its_caveat() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "there is no authentication" in lowered
    # The ruling is auth FIRST, not "add a password later" — and the runbook has to say
    # it in a way that survives being skimmed.
    assert "auth **first**" in text or "auth first" in lowered
    assert "do not \"just\" forward the port" in lowered or "do not \"just\"" in lowered


def test_the_runbook_documents_how_to_find_the_lan_ip() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "hostname -I" in text
    assert "192.168.7.73" in text, (
        "the address as it stands today, so the runbook is copy-pasteable; the command "
        "above it is what to trust when the DHCP lease moves"
    )


def test_the_runbook_explains_degraded_rather_than_saying_ok_is_fine() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "expected pre-window state" in text or "not** a broken server" in text
    assert "notes" in text and "present" in text, (
        "the operator must be told which field says which file is missing"
    )


def test_the_runbook_says_the_cache_can_be_deleted_and_how() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "rebuildable" in text.lower()
    assert "delete" in text.lower() or "rm /opt/paper-hunter/data/barcache.db" in text
    # And it must say the terminal does not refill it on its own, which is the part that
    # turns "I deleted the cache" into "why is the chart empty".
    assert "fetch=true" in text


def test_the_runbook_names_the_three_read_only_layers() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert text.count("mode=ro") >= 1
    assert "STATIC_MODULES" in text, "the explicit-route layer should name where it lives"
    assert "sendBeacon" in text, "the frontend layer should say what it greps for"