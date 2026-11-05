"""A small, cited library of payments rules the agent can look up, and the search over it.

Analogy: the binder on the desk next to the new hire. It holds short notes, each naming its source and saying what it
could not confirm. The agent may only quote from it, every rupee figure it gives must match a note's amounts (the same
numeric-fidelity guard that polices ledger figures), and "the binder doesn't say" is a correct answer.

Search is plain keyword scoring over a few notes: no embeddings, no vector database, no network. At this size a model of
relevance would be overkill and harder to test; the corpus is meant to grow by review, not by crawling.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

NOTES_PATH = Path(__file__).resolve().parents[2] / "evals" / "assure" / "regulation_notes.yaml"
REQUIRED = (
    "id",
    "title",
    "issuer",
    "reference",
    "issued",
    "url",
    "verified_on",
    "keywords",
    "summary",
    "rules",
    "not_confirmed",
)
_TOKEN = re.compile(r"[a-z0-9+]+")
_STOP = frozenset(
    "a an the of to in on for is are be by and or can may must what how when does do it its with at as from that this "
    "any under who which".split()
)
DISCLAIMER = (
    "Summary of a public source, for orientation. It is not legal advice; check the source before relying on it."
)


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@lru_cache(maxsize=4)
def load_notes(path: Path = NOTES_PATH) -> tuple[dict[str, Any], ...]:
    return tuple(yaml.safe_load(path.read_text()))


def _doc_tokens(n: dict[str, Any]) -> list[str]:
    # keywords count three times: they are the author's statement of what the note is about
    return (
        tokens(n["title"])
        + tokens(n["summary"])
        + tokens(" ".join(n["rules"]))
        + tokens(" ".join(map(str, n["keywords"]))) * 3
    )


def search(query: str, k: int = 3, notes: tuple[dict[str, Any], ...] | None = None) -> list[dict[str, Any]]:
    """Best-matching notes for a question, most relevant first. Empty when nothing matches (the honest answer)."""
    notes = notes if notes is not None else load_notes()
    q = tokens(query)
    if not q:
        return []
    docs = [_doc_tokens(n) for n in notes]
    avg = sum(len(d) for d in docs) / len(docs)
    scored = []
    for n, d in zip(notes, docs, strict=True):
        s = 0.0
        for term in set(q):
            tf = d.count(term)
            if not tf:
                continue
            df = sum(1 for other in docs if term in other)
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            s += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(d) / avg))  # BM25 with the usual constants
        if s > 0:
            scored.append((s, n))
    scored.sort(key=lambda x: -x[0])
    return [view(n) for _, n in scored[:k]]


def view(n: dict[str, Any]) -> dict[str, Any]:
    """What a tool call returns for one note. `amounts_paise` ends in `_paise` so the numeric guard checks figures."""
    return {
        "id": n["id"], "title": n["title"], "issuer": n["issuer"], "reference": n["reference"], "issued": n["issued"],
        "effective": n.get("effective", ""), "url": n["url"], "verified_on": n["verified_on"],
        "summary": n["summary"], "rules": n["rules"], "amounts_paise": n.get("amounts_paise") or {},
        "not_confirmed": n["not_confirmed"], "note": DISCLAIMER,
    }  # fmt: skip
