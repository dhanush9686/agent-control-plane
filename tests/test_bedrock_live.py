"""REAL Amazon Bedrock + Pinecone calls. Skipped unless RUN_LIVE=1 (they cost a fraction of a cent).

RUN_LIVE=1 pytest -m live tests/test_bedrock_live.py
"""

import math
import os

import pytest

from acp.config import Settings
from acp.embeddings import make_embedder
from acp.llm import get_llm
from acp.nodes import JudgeVerdict

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="live test: set RUN_LIVE=1"),
]

CONTEXT = (
    "[Source: report.pdf p.12]\n"
    "The Halvorsen arbitration clause requires disputes to be settled in Rotterdam within 45 days."
)


def _judge(llm, question):
    prompt = (
        f"Is the context enough to answer the question? sufficient=true ONLY if it explicitly states the answer.\n"
        f"Question: {question}\nContext:\n{CONTEXT}"
    )
    return llm.structured(JudgeVerdict, prompt, purpose="judge")


def test_cheapest_model_completes_and_reports_real_token_counts():
    s = Settings()
    assert s.llm_provider == "bedrock" and "nova-micro" in s.model
    # (Observed: asked to "reply with exactly one word: pong", Nova Micro said "game". The cheapest model follows
    # literal-format instructions loosely, so this test uses a plain factual question instead.)
    res = get_llm(s).complete("What is the capital of France? Answer in one word.", purpose="live")
    assert "paris" in res.text.lower() and res.input_tokens > 0 and res.output_tokens > 0 and res.model == s.model


def test_structured_output_through_tool_use_gets_both_judgements_right():
    llm = get_llm(Settings())
    assert _judge(llm, "Where must Halvorsen disputes be settled?").parsed.sufficient is True
    assert _judge(llm, "Who is the chief executive?").parsed.sufficient is False


def test_titan_embeddings_are_normalized_1024_vectors_and_similar_texts_score_higher():
    emb = make_embedder(Settings())
    a, b, c = emb.embed(["The warranty lasts 27 months.", "How long is the warranty period?", "Bananas are yellow."])
    dot = lambda x, y: sum(p * q for p, q in zip(x, y, strict=True))  # noqa: E731
    assert len(a) == 1024 and math.isclose(dot(a, a), 1.0, abs_tol=1e-3)
    assert dot(a, b) > dot(a, c)
