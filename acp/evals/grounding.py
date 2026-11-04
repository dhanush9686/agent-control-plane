"""The grounding evaluator: is each claim in the answer backed by the sources?

Analogy: a fact-checker with a highlighter. It breaks the answer into single
statements ("claims") and, for each one, looks for a sentence in the retrieved
chunks that supports it. Unhighlighted claims are possible hallucinations.

Known biases of LLM-as-judge (see docs/DECISIONS.md): it may be lenient toward
text that sounds fluent, it can share blind spots with the model that wrote the
answer (same family), and results vary slightly run to run. We reduce this with
temperature 0, a strict prompt, forcing the judge to quote evidence, and the
deterministic checks in checks.py -- but we do not claim it is perfect.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.documents import Document
from pydantic import BaseModel, Field

from acp.evals.checks import is_refusal
from acp.llm import LLM
from acp.nodes import format_docs


class ClaimVerdict(BaseModel):
    claim: str = Field(description="One atomic factual statement taken from the answer")
    supported: bool = Field(description="True only if the retrieved context explicitly supports this claim")
    evidence: str = Field(description="Short verbatim quote from the context that supports it, or empty if unsupported")


class GroundingReport(BaseModel):
    claims: list[ClaimVerdict] = Field(description="All factual claims in the answer; empty if there are none")


@dataclass
class GroundingResult:
    claims: list[ClaimVerdict]
    unsupported: list[ClaimVerdict]
    score: float  # supported / total claims (1.0 when there are no factual claims)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def hallucinated(self) -> bool:
        return bool(self.unsupported)


def evaluate_grounding(llm: LLM, question: str, answer: str, docs: list[Document]) -> GroundingResult:
    prompt = f"""You are a strict fact-checker. Check an ANSWER against the retrieved CONTEXT only.

Steps:
1. Split the ANSWER into atomic factual claims. Do NOT list: citations like [Source], statements that
   something is "not in the knowledge base", or restatements of the question.
2. For each claim decide if the CONTEXT explicitly supports it. Use no outside knowledge. A claim that
   is plausible or true in the real world but absent from the CONTEXT is NOT supported.
   A claim that contradicts the CONTEXT is NOT supported.
3. For supported claims, copy a short verbatim quote from the CONTEXT as evidence.

QUESTION: {question}

ANSWER:
{answer}

CONTEXT:
{format_docs(docs)}"""
    res = llm.structured(GroundingReport, prompt, purpose="grounding_eval")
    report: GroundingReport = res.parsed
    # The judge is told to skip refusal sentences but sometimes lists them anyway (seen in a real run:
    # "That is not in the knowledge base." flagged as unsupported). Admitting absence is not a factual
    # claim, so drop those deterministically instead of trusting the prompt.
    claims = [c for c in report.claims if not is_refusal(c.claim)]
    unsupported = [c for c in claims if not c.supported]
    total = len(claims)
    score = 1.0 if total == 0 else (total - len(unsupported)) / total
    return GroundingResult(claims, unsupported, score, res.input_tokens, res.output_tokens)
