"""Shared fakes so every unit test runs offline: no Groq, no embeddings, no torch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.documents import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp.config import Settings  # noqa: E402
from acp.llm import LLMResult  # noqa: E402

CORPUS = [
    Document(page_content="Rocky kills Garuda.", metadata={"source": "KGF 1"}),
    Document(page_content="Rocky dies at the end.", metadata={"source": "KGF 2"}),
    Document(page_content="Salaar is in Telugu.", metadata={"source": "Salaar"}),
    Document(page_content="Dragon is unreleased.", metadata={"source": "Dragon"}),
]


class FakeRetriever:
    """Returns the first k docs, shifted by call count so a retry yields something new."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, k: int) -> list[Document]:
        start = len(self.queries)
        self.queries.append(query)
        return [CORPUS[(start + i) % len(CORPUS)] for i in range(k)]


class FakeLLM:
    """Scripted LLM. `sufficient` is the judge's verdict per call (last value repeats)."""

    model = "fake"

    def __init__(self, sufficient: list[bool] | None = None, answer: str = "Rocky kills Garuda [KGF 1]") -> None:
        self.sufficient = list(sufficient if sufficient is not None else [True])
        self.answer = answer
        self.calls: list[str] = []
        self.grounding_report = None

    def complete(self, prompt: str, *, purpose: str) -> LLMResult:
        self.calls.append(purpose)
        text = "refined query" if purpose == "refine_query" else self.answer
        return LLMResult(text=text, input_tokens=100, output_tokens=20, latency_ms=5.0)

    def structured(self, schema, prompt: str, *, purpose: str) -> LLMResult:
        self.calls.append(purpose)
        if purpose == "judge":
            v = self.sufficient.pop(0) if len(self.sufficient) > 1 else self.sufficient[0]
            parsed = schema(sufficient=v, reasoning="because")
        elif purpose == "grounding_extract":
            names = [c.claim for c in self.grounding_report.claims] if self.grounding_report else []
            parsed = schema(claims=names)
        else:  # grounding_verify
            parsed = self.grounding_report or schema(claims=[])
        return LLMResult(parsed=parsed, input_tokens=200, output_tokens=40, latency_ms=7.0)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "traces.db", max_attempts=2, top_k=2)
