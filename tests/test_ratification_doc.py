"""Tests for the ratification inventory generator (scripts/gen_ratification_interpretations.py).

The ratification document is only trustworthy if its section (a) is actually derived from
the code rather than kept in sync by hand. These tests check the derivation: that every
``# INTERPRETATION:`` decision in the tree lands in the document, that cross-references and
the generator's own quoted examples do not, and that regeneration does not disturb
anything the operator wrote.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gen_ratification_interpretations.py"
DOC = REPO_ROOT / "docs" / "ratification.md"


def load_generator():
    spec = importlib.util.spec_from_file_location("gen_ratifications", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen():
    return load_generator()


def test_every_interpretation_is_in_the_inventory(gen):
    """A decision in the code that never reaches the operator is a decision nobody made."""
    doc_text = DOC.read_text(encoding="utf-8")
    missing = []
    for domain, entries in gen.collect().items():
        for rel, line_no, _label, _summary in entries:
            if f"`{rel}:{line_no}`" not in doc_text:
                missing.append(f"{rel}:{line_no} ({domain})")
    assert not missing, f"interpretations absent from docs/ratification.md: {missing}"


def test_inventory_counts_match_the_document(gen):
    found = gen.collect()
    total = sum(len(v) for v in found.values())
    assert total > 40, "the inventory collapsed; the extractor is probably broken"
    doc_text = DOC.read_text(encoding="utf-8")
    assert f"**{total} interpretations across {len(found)} domains.**" in doc_text
    for domain, entries in found.items():
        assert f"### {domain} ({len(entries)})" in doc_text


def test_cross_references_are_not_inventory_entries(gen, tmp_path):
    """A line that only *points* at an interpretation is not a decision to ratify."""
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "# A real decision.",
                "# INTERPRETATION: the short side wins ties, because the rulebook is silent.",
                "",
                "def f():",
                '    """Docstring mentioning the convention."""',
                "    # Histogram note. See INTERPRETATION.",
                "    # Per the module INTERPRETATION above, this is fine.",
                "    # The marker is named inline as ``# INTERPRETATION:`` here.",
                "    return 1",
            ]
        ),
        encoding="utf-8",
    )
    entries = gen.extract(source, "sample.py")
    assert [line for line, _l, _s in entries] == [2]


def test_summary_carries_what_and_why(gen, tmp_path):
    """A summary that states only the decision is not reviewable at a glance."""
    source = tmp_path / "sample.py"
    source.write_text(
        "# INTERPRETATION: seeding = SMA of the first `period` values. "
        "The brief is silent, and SMA seeding is the charting-platform standard.\n",
        encoding="utf-8",
    )
    (_line, _label, summary), = gen.extract(source, "sample.py")
    assert "SMA" in summary
    assert "charting-platform standard" in summary  # the why


def test_multiline_comment_rejoins_without_stray_hashes(gen, tmp_path):
    """A sentence wrapped across comment lines is still one sentence."""
    source = tmp_path / "sample.py"
    source.write_text(
        "# INTERPRETATION: a zero baseline is not an error — it yields inf,\n"
        "    # and the checklist turns that into a T4 FAIL rather than a crash.\n",
        encoding="utf-8",
    )
    (_line, _label, summary), = gen.extract(source, "sample.py")
    assert "#" not in summary
    assert "rather than a crash" in summary


def test_docstring_terminator_stops_the_summary(gen, tmp_path):
    """The summary must not absorb the prose that follows the comment."""
    source = tmp_path / "sample.py"
    source.write_text(
        'def f():\n'
        '    """Doc.\n'
        "\n"
        "    # INTERPRETATION: ties break toward calls, for key stability day to day.\n"
        '    """\n'
        "\n"
        "    # A completely unrelated trailing comment about something else entirely.\n",
        encoding="utf-8",
    )
    entries = gen.extract(source, "sample.py")
    assert len(entries) == 1
    assert "unrelated" not in entries[0][2]


def test_generator_does_not_inventory_itself(gen):
    """The generator quotes the marker in its own source; counting that would be silly."""
    found = gen.collect()
    everything = [row for entries in found.values() for row in entries]
    assert not any(rel.endswith("gen_ratification_interpretations.py") for rel, *_ in everything)


def test_write_only_touches_the_generated_block(gen, tmp_path):
    """Operator notes outside the markers are not the script's to rewrite."""
    doc = tmp_path / "ratification.md"
    doc.write_text(
        "# Title\n\nOPERATOR NOTE: do not delete, I typed this.\n\n"
        f"{gen.BEGIN_MARKER}\n\nSTALE\n\n{gen.END_MARKER}\n\n## Appendix\n\nMY OWN NOTES\n",
        encoding="utf-8",
    )
    before = doc.read_text(encoding="utf-8")
    gen.write(doc, gen.render(gen.collect()))
    after = doc.read_text(encoding="utf-8")

    assert "OPERATOR NOTE" in after
    assert "MY OWN NOTES" in after
    assert "STALE" not in after
    assert before != after


def test_committed_document_is_up_to_date(gen):
    """The checked-in doc must match what the current tree generates.

    This is the guard that stops section (a) from drifting away from the code: if someone
    edits an interpretation comment and forgets to regenerate, this fails.
    """
    assert gen.render(gen.collect()).strip() in DOC.read_text(encoding="utf-8")


def _generated_block(gen) -> str:
    """Only the text between the markers — sections (b)-(d) are the operator's, not ours."""
    body = DOC.read_text(encoding="utf-8").partition(gen.BEGIN_MARKER)[2]
    return body.partition(gen.END_MARKER)[0]


def test_every_entry_is_a_checkbox(gen):
    """The point of section (a) is signatures: every row needs a box to tick."""
    for line in _generated_block(gen).splitlines():
        if line.startswith("- "):
            assert line.startswith("- [ ] "), f"entry is not an approvable checkbox: {line}"