"""Phase 3b — the CLI's safety surface: dry run is the default, --live is a gate.

Only the argument-level guarantees are exercised here. Everything past the parser needs
a data provider, and Phase 3b's other suites cover that with fixtures.
"""

from __future__ import annotations

import pytest

from executor.main import build_parser, main


def test_dry_run_is_on_when_neither_flag_is_given():
    args = build_parser().parse_args(["watch"])
    assert args.dry_run is True


def test_the_two_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--live", "--dry-run", "watch"])


def test_live_without_credentials_fails_before_any_work(monkeypatch, capsys):
    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)
    # exit 1: a configuration refusal, printed as an error line rather than a traceback
    assert main(["--live", "watch"]) == 1
    err = capsys.readouterr().err
    assert "ALPACA_PAPER_KEY" in err  # names the variable
    assert "sk-" not in err.lower()  # and no value-shaped string


def test_dry_run_never_reads_a_credential(monkeypatch, capsys):
    """A missing key must not stop a dry run; it fails later, on the missing plan."""

    def explode(*_a, **_k):  # pragma: no cover - only runs on failure
        raise AssertionError("dry run read the credential environment")

    monkeypatch.delenv("ALPACA_PAPER_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET", raising=False)
    monkeypatch.setattr("executor.alpaca_trading.AlpacaTradingClient.from_env", explode)
    assert main(["watch"]) == 2
    assert "no plan" in capsys.readouterr().err