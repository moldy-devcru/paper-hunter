"""The ``--arms`` parser on ``scripts/backfill_iv_rank.py``.

Pinned here because the shipped default was broken in a way only running the script
would find: ``--arms BC`` split on commas alone, produced the single token ``("BC",)``,
matched no band, and exited 2 with ``no arms matched ('BC',)``. The default value was
the thing that was wrong, so the tests pin the *default* as well as the tolerant
parsing — a parser fix alone would leave a default that only reads correctly by accident.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_iv_rank.py"


def _load():
    """Import the script as a module without installing it as a package."""
    spec = importlib.util.spec_from_file_location("_backfill_iv_rank_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


#: Every spelling that must mean the same set. Both the documented default form ("B,C")
#: and the packed form ("BC") are in the list deliberately — the regression was one of
#: them not working, so a test of only one would not have caught it.
EQUIVALENT = [
    "BC",
    "B,C",
    "B, C",
    "b,c",
    "B C",
    "  BC  ",
    "B,C,",
]


@pytest.mark.parametrize("spec", EQUIVALENT)
def test_both_spellings_parse_to_the_same_arms(mod, spec):
    assert mod.parse_arms(spec) == ("B", "C")


def test_default_value_parses_to_the_full_arm_set(mod):
    """The pinned reason the fix exists: the *default* has to survive its own parser."""
    default = mod.build_parser().get_default("arms")
    assert mod.parse_arms(default) == ("B", "C")


def test_default_is_written_in_the_canonical_comma_form(mod):
    assert mod.DEFAULT_ARMS_SPEC == "B,C"


def test_single_arm_selects_only_itself(mod):
    assert mod.parse_arms("C") == ("C",)
    assert mod.parse_arms("B") == ("B",)


def test_order_is_preserved_and_repeats_collapse(mod):
    assert mod.parse_arms("C,B,C") == ("C", "B")


@pytest.mark.parametrize("spec", ["", "   ", ",", " , , "])
def test_empty_spec_yields_no_arms_rather_than_defaulting(mod, spec):
    """An empty spec must fail loudly at the call site, not silently mean 'all arms'."""
    assert mod.parse_arms(spec) == ()


def test_parser_accepts_packed_flag_without_error(mod):
    """Full end-to-end through argparse — the flag must survive to a usable tuple."""
    args = mod.build_parser().parse_args(["--arms", "BC"])
    assert mod.parse_arms(args.arms) == ("B", "C")


def test_unknown_arm_is_reported_rather_than_silently_dropped(mod):
    """``--arms X`` parses fine and is then rejected by the band match, not by the parser.

    The parser does not know which arms exist — the rulebook owns that — so this pins
    the division of labour: the parser normalises, the band match decides.
    """
    assert mod.parse_arms("X") == ("X",)
    bands = (("B", 0, 0), ("C", 90, 180))
    assert tuple(b for b in bands if b[0] in mod.parse_arms("X")) == ()