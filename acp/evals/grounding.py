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


class ClaimList(BaseModel):
    claims: list[str] = Field(description="Atomic factual claims made by the answer; empty if none")


class ClaimVerdict(BaseModel):
    claim: str = Field(description="One atomic factual statement taken from the answer")
    supported: bool = Field(description="True only if the retrieved context explicitly supports this claim")
    evidence: str = Field(description="Short verbatim quote from the context that supports it, or empty if unsupported")


class GroundingReport(BaseModel):
    claims: list[ClaimVerdict] = Field(description="One verdict per claim given")


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


def extract_claims(llm: LLM, question: str, answer: str) -> tuple[list[str], int, int]:
    """Step 1: split the answer into atomic claims. Sees NO context, so it cannot be swayed by it."""
    prompt = f"""Split the ANSWER into atomic factual claims, one per item, each understandable on its own.
Do NOT include: citations like [Source], statements that something is "not in the knowledge base",
or restatements of the question. If the answer makes no factual claim, return an empty list.

QUESTION: {question}

ANSWER:
{answer}"""
    res = llm.structured(ClaimList, prompt, purpose="grounding_extract")
    claims = [c for c in res.parsed.claims if c.strip() and not is_refusal(c)]
    return claims, res.input_tokens, res.output_tokens


def verify_claims(llm: LLM, claims: list[str], docs: list[Document]) -> tuple[list[ClaimVerdict], int, int]:
    """Step 2: check each claim against the retrieved context only."""
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
    prompt = f"""You are a strict fact-checker. For EACH numbered CLAIM decide if the CONTEXT explicitly supports it.
Use no outside knowledge: a claim that is plausible or true in the real world but absent from the CONTEXT
is NOT supported. A claim that contradicts the CONTEXT, changes a name, number or date, or adds detail the
CONTEXT does not state is NOT supported. For supported claims copy a short verbatim quote as evidence.
Return one verdict per claim, in order.

CLAIMS:
{numbered}

CONTEXT:
{format_docs(docs)}"""
    res = llm.structured(GroundingReport, prompt, purpose="grounding_verify")
    return list(res.parsed.claims), res.input_tokens, res.output_tokens


def evaluate_grounding(llm: LLM, question: str, answer: str, docs: list[Document]) -> GroundingResult:
    """Two-step grounding check: extract claims, then verify them. Refusal sentences are never claims."""
    claims, t_in, t_out = extract_claims(llm, question, answer)
    verdicts: list[ClaimVerdict] = []
    if claims:  # nothing to verify -> skip the second call (saves free-tier quota)
        verdicts, v_in, v_out = verify_claims(llm, claims, docs)
        t_in, t_out = t_in + v_in, t_out + v_out
        verdicts = [v for v in verdicts if not is_refusal(v.claim)]
    unsupported = [v for v in verdicts if not v.supported]
    score = 1.0 if not verdicts else (len(verdicts) - len(unsupported)) / len(verdicts)
    return GroundingResult(verdicts, unsupported, score, t_in, t_out)
