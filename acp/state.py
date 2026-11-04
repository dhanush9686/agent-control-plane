"""The shared notebook every node reads from and writes to.

Analogy: a clipboard passed down an assembly line. Each station (node) reads
what it needs, scribbles its part, and hands it on.
"""

from __future__ import annotations

from typing import TypedDict

from langchain_core.documents import Document


class AgentState(TypedDict, total=False):
    question: str
    search_query: str  # empty on the first pass; set by refine_query
    retrieved_docs: list[Document]  # grows across attempts (deduplicated)
    search_attempts: int
    sufficient_context: bool  # the judge's verdict
    judge_reasoning: str
    answer: str


def initial_state(question: str) -> AgentState:
    return AgentState(
        question=question,
        search_query="",
        retrieved_docs=[],
        search_attempts=0,
        sufficient_context=False,
        judge_reasoning="",
        answer="",
    )
