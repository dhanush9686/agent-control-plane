"""Cheap, deterministic checks (no LLM). Fast, free, and never moody.

Analogy: the spell-checker next to the human editor. It can't judge meaning,
but it reliably catches missing citations, banned phrases and missing refusals.
"""

from __future__ import annotations

import re
import unicodedata

from acp.evals.golden import GoldenCase
from acp.nodes import extract_citations


def normalize(text: str) -> str:
    """Lowercase, fold unicode (curly quotes, non-breaking hyphens), keep word characters only."""
    t = unicodedata.normalize("NFKC", text).lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


def contains(text: str, phrase: str) -> bool:
    """Whole-word(s) match so 'no' does not match 'know'."""
    return normalize(phrase) in normalize(text)


def is_refusal(answer: str) -> bool:
    return contains(answer, "not in the knowledge base")


def citation_check(answer: str, retrieved_sources: set[str]) -> tuple[bool, list[str]]:
    """(ok, problems). A non-refusal answer must cite >=1 source, and only sources it actually retrieved."""
    cited = extract_citations(answer)
    problems: list[str] = []
    if not is_refusal(answer) and not cited:
        problems.append("no citation")
    unknown = [c for c in cited if c not in retrieved_sources]
    if unknown:
        problems.append(f"cites sources that were not retrieved: {unknown}")
    return (not problems), problems


def behavior_check(case: GoldenCase, answer: str) -> list[str]:
    """Did the agent do the expected thing (answer vs refuse, required/banned phrases)? Returns failures."""
    failures: list[str] = []
    refused = is_refusal(answer)
    if case.expect == "refuse" and not refused:
        failures.append("should have refused (answer is not in the knowledge base) but answered")
    if case.expect == "answer" and refused:
        failures.append("should have answered but refused")
    if case.expect == "answer":
        for group in case.must_include:
            if not any(contains(answer, p) for p in group):
                failures.append(f"missing expected content (one of {group})")
    for phrase in case.must_not_include:
        if contains(answer, phrase):
            failures.append(f"contains banned phrase {phrase!r}")
    return failures
