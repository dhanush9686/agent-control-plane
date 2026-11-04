"""REAL end-to-end tests: real Groq LLM + real FAISS index + real tracing.

Skipped by default (they spend free-tier tokens). Run with: make test-live
"""

import os

import pytest

from acp import store
from acp.config import get_settings
from acp.evals.checks import is_refusal
from acp.llm import get_llm
from acp.nodes import extract_citations
from acp.retrieval import FaissRetriever
from acp.runner import Agent
from acp.tracing import Tracing

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="live test: run `make test-live`"),
]


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    s = get_settings()
    s = type(s)(db_path=tmp_path_factory.mktemp("live") / "t.db")
    return Agent(get_llm(s), FaissRetriever(s), s, Tracing(s)), s


def test_real_answer_has_citation_and_trace(agent):
    a, s = agent
    final, run_id = a.ask("Who directed KGF: Chapter 1?")
    assert "Neel" in final["answer"] and extract_citations(final["answer"])
    run = store.connect(s.db_path).execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run["input_tokens"] > 0 and run["llm_calls"] >= 2


def test_real_unanswerable_question_is_refused(agent):
    a, _ = agent
    final, _ = a.ask("Who plays Deva in Salaar?")
    assert is_refusal(final["answer"])
    assert final["search_attempts"] == 2  # retry cap reached before refusing
