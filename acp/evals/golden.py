"""Load the golden question set (evals/golden.yaml).

Analogy: the answer key for an exam. Each question carries what a good answer
must (or must not) contain, and whether the right move is to answer or to
admit "not in the knowledge base".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from acp.config import ROOT

GOLDEN_PATH = ROOT / "evals" / "golden.yaml"
CATEGORIES = ("answerable", "multi_hop", "thin_data", "contradiction_trap", "unanswerable", "prompt_injection")


@dataclass(frozen=True)
class GoldenCase:
    id: str
    category: str
    question: str
    expect: Literal["answer", "refuse"]
    must_include: list[list[str]] = field(default_factory=list)  # every group needs >=1 hit
    must_not_include: list[str] = field(default_factory=list)


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenCase]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = [GoldenCase(**item) for item in raw]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in golden set")
    for c in cases:
        if c.category not in CATEGORIES:
            raise ValueError(f"{c.id}: unknown category {c.category!r}")
        if c.expect not in ("answer", "refuse"):
            raise ValueError(f"{c.id}: expect must be answer|refuse")
    return cases
