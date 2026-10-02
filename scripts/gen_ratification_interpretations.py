#!/usr/bin/env python3
"""Regenerate section (a) of docs/ratification.md — the INTERPRETATION inventory.

Every judgment call made while implementing the frozen rulebook is recorded in the code
as a ``# INTERPRETATION:`` comment, next to the decision, so a reader of that function
sees why it behaves the way it does without having to find this document. This script
extracts those comments into the operator-facing inventory, grouped by domain, each with
a file:line reference the operator can open directly.

Run from the repo root:

    .venv/bin/python scripts/gen_ratification_interpretations.py            # print
    .venv/bin/python scripts/gen_ratification_interpretations.py --write     # rewrite

``--write`` replaces the block between the ``<!-- BEGIN GENERATED ... -->`` and
``<!-- END GENERATED ... -->`` markers in docs/ratification.md and leaves everything
outside them alone. Nothing else in that document is touched, including the operator's
own notes — which is why the inventory is generated rather than hand-maintained.

The summary line for each entry is the first sentence of the comment. That is a
deliberate limitation: these are one-line "what was decided + why" notes, and the full
reasoning stays in the code where the decision lives. If an entry's first sentence does
not say why, the comment needs work, not the script.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOC = REPO_ROOT / "docs" / "ratification.md"

BEGIN_MARKER = "<!-- BEGIN GENERATED: INTERPRETATION INVENTORY -->"
END_MARKER = "<!-- END GENERATED: INTERPRETATION INVENTORY -->"

SCAN_DIRS = ("executor", "journal", "config", "data", "analysis", "scripts")
SCAN_SUFFIXES = (".py", ".sql", ".yaml", ".yml")

#: Paths whose *domain* decides the grouping, most-specific first.
DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("indicators", ("executor/indicators.py", "executor/snapshot_builder.py")),
    ("checklist", ("executor/checklist.py",)),
    (
        "data",
        (
            "executor/iv_rank.py",
            "executor/alpaca_client.py",
            "data/event_calendar.py",
            "config/loader.py",
        ),
    ),
    (
        "trading",
        (
            "executor/watch_loop.py",
            "executor/alpaca_trading.py",
            "executor/position_manager.py",
            "journal/store.py",
            "journal/schema.sql",
        ),
    ),
    (
        "analysis",
        (
            "analysis/rollup.py",
            "analysis/shadow_roll.py",
            "executor/noshot.py",
            "executor/hunt_plan.py",
        ),
    ),
    ("soak", ("executor/soak.py",)),
    ("CLI / plumbing", ("executor/main.py",)),
)

#: A line that *defines* an interpretation opens with the marker and states a decision.
#: A line that merely *refers* to one ("see the module INTERPRETATION above") is not an
#: inventory entry, and counting it as one would inflate the operator's review queue with
#: cross-references rather than decisions. Excluded by the leading-word patterns below.
DEFINITION = re.compile(r"INTERPRETATION(?:\s*\([^)]*\))?\s*:")
#: Any line that mentions the marker without *defining* one: "see INTERPRETATION",
#: "the module INTERPRETATION above", a backticked convention note. Counting these would
#: pad the operator's review queue with cross-references instead of decisions, and a
#: padded queue is a queue nobody finishes.
CROSS_REFERENCE = re.compile(
    r"\b(?:See|see|Per|per|above|below|the)\b[^.]*\bINTERPRETATION\b",
)
#: A mention wrapped in literal markers (the module docstrings that explain the
#: ``# INTERPRETATION:`` convention itself) describes the convention, not a decision.
QUOTED = re.compile(r"`[^`]*INTERPRETATION[^`]*`")
LABELLED = re.compile(r"INTERPRETATION\s*\(([^)]+)\)\s*:")
SENTENCE_END = re.compile(r"(?<=[.?!;])\s+(?=[A-Z`(\"'])")
COMMENT_PREFIX = re.compile(r"^\s*(?:#+\s?)+")

#: Summary length target. One sentence is often only the "what"; two usually carry the
#: "why" too, and the operator is deciding whether to approve, not skimming a changelog.
SUMMARY_MIN_CHARS = 120
SUMMARY_MAX_CHARS = 260


def domain_for(rel: str) -> str:
    for name, prefixes in DOMAINS:
        if rel in prefixes:
            return name
    return "other"


def _paragraph(lines: list[str], index: int) -> str:
    """The full marked comment starting at ``lines[index]``, as one clean string.

    Comment continuations keep their meaning only if the ``#`` prefix is stripped, so a
    sentence wrapped across lines rejoins instead of gaining a stray "#" mid-clause.
    Stops at the first blank line or the closing docstring delimiter, so the summary can
    never absorb the prose that follows.
    """
    parts = [COMMENT_PREFIX.sub("", lines[index]).strip()]
    for follow in lines[index + 1 :]:
        stripped = follow.strip()
        if not stripped or stripped in {'"""', "'''"}:
            break
        parts.append(COMMENT_PREFIX.sub("", follow).strip())
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _summarise(blob: str) -> str:
    """First one-to-two sentences of the comment, clamped for a scannable review line."""
    blob = blob.replace("``", "`").lstrip("-—: ").strip()
    sentences = SENTENCE_END.split(blob)
    out = ""
    for sentence in sentences:
        if not sentence:
            continue
        candidate = f"{out} {sentence}".strip()
        if out and len(candidate) > SUMMARY_MAX_CHARS:
            break
        out = candidate
        if len(out) >= SUMMARY_MIN_CHARS:
            break
    out = out.rstrip(". ") + "."
    if len(out) > SUMMARY_MAX_CHARS:
        out = out[: SUMMARY_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return out


def extract(path: Path, rel: str) -> list[tuple[int, str, str]]:
    """``[(line_no, label, summary)]`` for one file."""
    entries: list[tuple[int, str, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if "INTERPRETATION" not in line or QUOTED.search(line):
            continue
        if CROSS_REFERENCE.search(line):
            continue
        paragraph = _paragraph(lines, index)
        match = DEFINITION.search(paragraph)
        if not match:
            continue
        label_match = LABELLED.search(paragraph)
        label = label_match.group(1).strip() if label_match else ""
        summary = _summarise(paragraph[match.end() :])
        if not summary or summary == ".":
            continue
        entries.append((index + 1, label, summary))
    return entries


def collect() -> dict[str, list[tuple[str, int, str, str]]]:
    """``{domain: [(rel_path, line, label, summary)]}``, domains in presentation order."""
    found: dict[str, list[tuple[str, int, str, str]]] = {}
    for directory in SCAN_DIRS:
        base = REPO_ROOT / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in SCAN_SUFFIXES or not path.is_file():
                continue
            if path.resolve() == Path(__file__).resolve():
                continue  # this generator quotes the marker; it must not inventory itself
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            domain = domain_for(rel)
            for line_no, label, summary in extract(path, rel):
                found.setdefault(domain, []).append((rel, line_no, label, summary))
    order = [name for name, _ in DOMAINS]
    order += sorted(set(found) - set(order))
    return {name: found[name] for name in order if found.get(name)}


def render(found: dict[str, list[tuple[str, int, str, str]]]) -> str:
    total = sum(len(v) for v in found.values())
    lines = [
        BEGIN_MARKER,
        "",
        f"**{total} interpretations across {len(found)} domains.** Every one was a decision",
        "the frozen rulebook or docs/brief.md did not make for us. Each row says what was",
        "decided and the one-line reason; the full reasoning is in the code at the referenced",
        "line, and that code is the source of truth if the two ever disagree.",
        "",
        "> Regenerate: `.venv/bin/python scripts/gen_ratification_interpretations.py --write`",
        "> (mechanical; the text between these markers is replaced, the rest of the document",
        "> — including any operator notes — is not).",
        "",
    ]
    for domain, entries in found.items():
        lines.append(f"### {domain} ({len(entries)})")
        lines.append("")
        for rel, line_no, label, summary in entries:
            tag = f" — {label}" if label else ""
            lines.append(
                f"- [ ] **`{rel}:{line_no}`**{tag} — {summary} "
                f"([source]({os_relative(rel)}#L{line_no}))"
            )
        lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines)


def os_relative(rel: str) -> str:
    """Path from docs/ratification.md to a repo file."""
    depth = len(Path("docs/ratification.md").parts) - 1
    return "../" * depth + rel


def write(doc: Path, block: str) -> bool:
    text = doc.read_text(encoding="utf-8")
    if BEGIN_MARKER not in text or END_MARKER not in text:
        raise SystemExit(
            f"{doc} is missing the generated-block markers; add them before --write"
        )
    head, _, rest = text.partition(BEGIN_MARKER)
    _, _, tail = rest.partition(END_MARKER)
    doc.write_text(head + block + tail, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc", default=str(DEFAULT_DOC), help="ratification doc to rewrite")
    parser.add_argument("--write", action="store_true", help="rewrite the doc in place")
    args = parser.parse_args(argv)

    found = collect()
    block = render(found)
    if args.write:
        write(Path(args.doc), block)
        total = sum(len(v) for v in found.values())
        print(f"wrote {total} entries across {len(found)} domains to {args.doc}")
    else:
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())