"""Measure the GRADER: how often does the grounding judge agree with known labels?

Analogy: before trusting a thermometer to take temperatures, check it against a few
liquids whose temperature you already know. Here the "liquids" are answers whose
correctness we constructed on purpose (swapped names, changed numbers, true-but-absent facts...).

Honesty note: labels in evals/judge_labels.yaml are written by the project author, not independent
annotators. They measure the judge on KNOWN tricky cases; they are not a human-agreement study.
Drop independently labelled items into evals/judge_labels_human.yaml to evaluate those too.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from langchain_core.documents import Document

from acp.config import ROOT, get_settings
from acp.errors import ACPError
from acp.evals.grounding import evaluate_grounding
from acp.evals.report import wilson
from acp.index import load_and_chunk
from acp.llm import LLM

LABEL_FILES = [ROOT / "evals" / "judge_labels.yaml", ROOT / "evals" / "judge_labels_human.yaml"]
OUT = ROOT / "results" / "judge_accuracy.json"


def load_labels(paths: list[Path] = LABEL_FILES) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for p in paths:
        if p.is_file():
            for it in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
                it.setdefault("labelled_by", "author" if p.name == "judge_labels.yaml" else "human")
                items.append(it)
    return items


def context_for(sources: list[str], chunks: list[Document]) -> list[Document]:
    docs = [c for c in chunks if any(c.metadata["source"].startswith(s) for s in sources)]
    if not docs:
        raise ValueError(f"no corpus chunks match sources {sources}")
    return docs


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Treat 'answer contains a hallucination' as the positive class."""
    tp = sum(1 for r in rows if r["label"] and r["predicted"])
    fp = sum(1 for r in rows if not r["label"] and r["predicted"])
    fn = sum(1 for r in rows if r["label"] and not r["predicted"])
    tn = sum(1 for r in rows if not r["label"] and not r["predicted"])
    n = len(rows)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    by_kind: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r["label"] == r["predicted"])
    return {
        "n": n,
        "accuracy": round((tp + tn) / n, 4) if n else None,
        "accuracy_ci95": wilson(tp + tn, n),
        "precision": None if precision is None else round(precision, 4),
        "recall": None if recall is None else round(recall, 4),
        "f1": None if f1 is None else round(f1, 4),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "accuracy_by_kind": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_kind.items())},
    }


def run_judge_check(judge: LLM, items: list[dict[str, Any]], chunks: list[Document], progress: Any = print) -> dict:
    rows = []
    for it in items:
        try:
            res = evaluate_grounding(
                judge, it.get("question", "(n/a)"), it["answer"], context_for(it["sources"], chunks)
            )
        except ACPError as e:  # one flaky call must not throw away the other 37 results
            progress(f"{it['id']} ERROR {e}")
            rows.append({"id": it["id"], "kind": it["kind"], "error": str(e), "labelled_by": it["labelled_by"]})
            continue
        row = {
            "id": it["id"],
            "kind": it["kind"],
            "label": bool(it["unsupported"]),
            "predicted": res.hallucinated,
            "flagged": [c.claim for c in res.unsupported],
            "labelled_by": it["labelled_by"],
        }
        rows.append(row)
        progress(f"{it['id']} {'ok ' if row['label'] == row['predicted'] else 'MISS'} {it['kind']}")
    scored = [r for r in rows if "error" not in r]
    return {"judge_model": judge.model, "n_errors": len(rows) - len(scored), **score(scored), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    from acp.evals.runner import judge_settings
    from acp.llm import get_llm

    p = argparse.ArgumentParser(prog="acp.evals.judge_check")
    p.add_argument("--judge-model", nargs="*", help="One or more grader models (default: settings.judge_model)")
    args = p.parse_args(argv)
    try:
        settings = get_settings()
        items, chunks = load_labels(), load_and_chunk(settings.corpus_path)
        reports = [
            run_judge_check(get_llm(judge_settings(settings, m)), items, chunks)
            for m in (args.judge_model or [settings.judge_model])
        ]
    except ACPError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"reports": reports}, indent=2, ensure_ascii=False))
    for r in reports:
        print(f"\n{r['judge_model']}: accuracy={r['accuracy']} 95% CI={r['accuracy_ci95']}")
        print(f"  precision={r['precision']} recall={r['recall']} confusion={r['confusion']}")
    print(f"Saved {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
