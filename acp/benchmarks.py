"""Turn public, human-made datasets into the formats this project already evaluates.

Why: our own corpus and labels are written by the project author. These slices come from datasets built by
other people, so a reader does not have to trust the author's judgement.

  SQuAD 2.0   (human-written questions, incl. human-written UNANSWERABLE ones; CC BY-SA 4.0)
                -> a second knowledge base + golden set for the agent (answer vs refuse)
  RAGTruth    (RAG answers whose hallucinations were marked by human annotators)
                -> labelled items for grading the GRADER
  FEVER       (claims labelled SUPPORTS / REFUTES / NOT ENOUGH INFO by human annotators)
                -> more labelled items for grading the grader

All functions here are pure (rows in, items out) so they are unit-tested offline with tiny fake rows;
`scripts/fetch_benchmarks.py` does the downloading.
"""

from __future__ import annotations

import ast
import json
import random
import re
from typing import Any

MAX_CONTEXT_CHARS = 2500  # keep prompts small enough for free-tier token-per-minute limits
MAX_ANSWER_CHARS = 700


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_squad_kb(
    rows: list[dict[str, Any]], n_questions: int = 60, seed: int = 0
) -> tuple[str, list[dict[str, Any]]]:
    """(corpus_text, golden_cases). Half answerable, half unanswerable (SQuAD 2.0's human-written ones).

    KB = every distinct paragraph in `rows` (so a retriever has real distractors); questions are drawn from
    paragraphs that have BOTH an answerable and an unanswerable question.
    Caveat (documented in the README): a SQuAD "unanswerable" question is unanswerable *given its paragraph*;
    a different paragraph of the same article could rarely answer it, which would count against the agent unfairly.
    """
    by_context: dict[str, dict[str, Any]] = {}
    for r in rows:
        ctx = _one_line(r["context"])
        entry = by_context.setdefault(ctx, {"title": r["title"], "answerable": [], "unanswerable": []})
        answers = (
            r["answers"]["text"] if isinstance(r["answers"], dict) else ast.literal_eval(str(r["answers"]))["text"]
        )
        (entry["answerable"] if answers else entry["unanswerable"]).append((r, sorted(set(answers))))
    rng = random.Random(seed)
    paired = [c for c, e in by_context.items() if e["answerable"] and e["unanswerable"]]
    rng.shuffle(paired)
    half = n_questions // 2
    cases: list[dict[str, Any]] = []
    for ctx in paired[:half]:
        e = by_context[ctx]
        ra, answers = e["answerable"][0]
        ru, _ = e["unanswerable"][0]
        cases.append(
            {
                "id": f"sq-a-{len(cases):02d}",
                "category": "answerable",
                "question": _one_line(ra["question"]),
                "expect": "answer",
                "must_include": [answers[:6]],  # any-of: all human-provided answer variants
            }
        )
        cases.append(
            {
                "id": f"sq-u-{len(cases):02d}",
                "category": "unanswerable",
                "question": _one_line(ru["question"]),
                "expect": "refuse",
            }
        )
    sections: dict[str, list[str]] = {}
    for ctx, e in by_context.items():
        sections.setdefault(e["title"].replace("_", " "), []).append(ctx)
    corpus = "\n\n".join(f"=== {t} ===\n" + "\n\n".join(paras) for t, paras in sections.items()) + "\n"
    return corpus, cases


def ragtruth_items(rows: list[dict[str, Any]], per_class: int = 20, seed: int = 0) -> list[dict[str, Any]]:
    """Balanced labelled items from RAGTruth QA rows. Label = annotators marked any hallucination span."""
    pos, neg = [], []
    for r in rows:
        if r.get("task_type") != "QA":
            continue
        ctx, out = r["context"], r["output"]
        if not ctx or not out or len(ctx) > MAX_CONTEXT_CHARS or len(out) > MAX_ANSWER_CHARS:
            continue
        labels = r["hallucination_labels"]
        labels = json.loads(labels) if isinstance(labels, str) else labels
        (pos if labels else neg).append(
            {
                "id": f"rt-{r['id']}",
                "dataset": "RAGTruth",
                "kind": "hallucinated" if labels else "clean",
                "question": _one_line(r["query"]),
                "context": ctx,
                "answer": _one_line(out),
                "unsupported": bool(labels),
                "labelled_by": "human annotators (RAGTruth)",
            }
        )
    rng = random.Random(seed)
    rng.shuffle(pos)
    rng.shuffle(neg)
    return pos[:per_class] + neg[:per_class]


def fever_items(rows: list[dict[str, Any]], per_class: int = 20, seed: int = 0) -> list[dict[str, Any]]:
    """Balanced items: claim SUPPORTED by the evidence vs not (REFUTES or NOT ENOUGH INFO; both mean 'not supported').

    In the nli_fever copy the CLAIM is in `premise` and the Wikipedia EVIDENCE is in `hypothesis` (the names are
    swapped relative to their usual meaning); verified by reading rows before relying on it.
    """
    sup, not_sup = [], []
    for r in rows:
        claim, evidence = _one_line(r["premise"]), _one_line(r["hypothesis"])
        label = r["fever_gold_label"]
        if (
            not claim
            or not evidence
            or len(evidence) > MAX_CONTEXT_CHARS
            or label not in ("SUPPORTS", "REFUTES", "NOT ENOUGH INFO")
        ):
            continue
        item = {
            "id": f"fv-{r['cid']}-{label[:3]}",
            "dataset": "FEVER",
            "kind": {"SUPPORTS": "supported", "REFUTES": "refuted", "NOT ENOUGH INFO": "not_enough_info"}[label],
            "question": "n/a",
            "context": evidence,
            "answer": claim,
            "unsupported": label != "SUPPORTS",
            "labelled_by": "human annotators (FEVER)",
        }
        (sup if label == "SUPPORTS" else not_sup).append(item)
    rng = random.Random(seed)
    rng.shuffle(sup)
    rng.shuffle(not_sup)
    return sup[:per_class] + not_sup[:per_class]
