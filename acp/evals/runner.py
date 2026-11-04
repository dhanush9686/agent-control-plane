"""Run the golden set through the (traced) agent and grade every answer.

Analogy: exam day. Each question is handed to the agent as a fresh student,
the answer is graded by the answer key (checks.py) and a fact-checker
(grounding.py), and the marksheet is saved to SQLite + results/eval_report.json.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acp import store
from acp.config import ROOT, Settings
from acp.errors import ACPError
from acp.evals import checks
from acp.evals.golden import GoldenCase
from acp.evals.grounding import evaluate_grounding
from acp.evals.report import aggregate
from acp.llm import LLM
from acp.runner import Agent, summarize_run

REPORT_PATH = ROOT / "results" / "eval_report.json"


def evaluate_case(agent: Agent, judge_llm: LLM, case: GoldenCase, settings: Settings) -> dict[str, Any]:
    """Ask one question and grade it. Provider outages become an `error` result, not a crash."""
    base: dict[str, Any] = {
        "case_id": case.id,
        "category": case.category,
        "question": case.question,
        "expect": case.expect,
    }
    try:
        final, run_id = agent.ask(case.question)
        answer = final["answer"]
        docs = final["retrieved_docs"]
        grounding = evaluate_grounding(judge_llm, case.question, answer, docs)
    except ACPError as e:
        return {**base, "error": str(e), "passed": False, "latency_ms": 0.0, "cost_usd": 0.0}

    refused = checks.is_refusal(answer)
    cite_ok, cite_problems = checks.citation_check(answer, {d.metadata["source"] for d in docs})
    behavior_failures = checks.behavior_check(case, answer)
    run = summarize_run(settings.db_path, run_id) if run_id else None
    failures = list(behavior_failures) + cite_problems
    if grounding.hallucinated:
        failures.append(f"{len(grounding.unsupported)} unsupported claim(s)")
    return {
        **base,
        "run_id": run_id,
        "answer": answer,
        "refused": refused,
        "attempts": final["search_attempts"],
        "sources": sorted({d.metadata["source"] for d in docs}),
        "behavior_failures": behavior_failures,
        "citation_ok": cite_ok,
        "grounding_score": grounding.score,
        "hallucinated": grounding.hallucinated,
        "claims": [c.model_dump() for c in grounding.claims],
        "unsupported_claims": [c.claim for c in grounding.unsupported],
        "failures": failures,
        "passed": not failures,
        "latency_ms": run["latency_ms"] if run else 0.0,
        "cost_usd": run["cost_usd"] if run else 0.0,
        "error": None,
    }


def run_eval(
    agent: Agent,
    judge_llm: LLM,
    cases: list[GoldenCase],
    settings: Settings,
    pause_s: float = 0.0,
    report_path: Path = REPORT_PATH,
    progress: Any = print,
) -> dict[str, Any]:
    eval_id = uuid.uuid4().hex[:10]
    results = []
    for i, case in enumerate(cases, 1):
        r = evaluate_case(agent, judge_llm, case, settings)
        results.append(r)
        status = "ERROR" if r.get("error") else ("PASS" if r["passed"] else "FAIL")
        progress(f"[{i}/{len(cases)}] {status} {case.id}  {'; '.join(r.get('failures', [])) or r.get('error') or ''}")
        if pause_s:
            time.sleep(pause_s)

    report = {
        "eval_id": eval_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent_model": settings.model,
        "summary": aggregate(results),
        "results": results,
    }
    conn = store.connect(settings.db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO eval_runs VALUES (?,?,?,?)",
                (eval_id, report["created_at"], settings.model, json.dumps(report["summary"])),
            )
            conn.executemany(
                "INSERT INTO eval_results VALUES (?,?,?,?,?)",
                [(eval_id, r["case_id"], r.get("run_id"), r["category"], json.dumps(r)) for r in results],
            )
    finally:
        conn.close()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def judge_settings(settings: Settings, judge_model: str | None) -> Settings:
    """Optionally grade with a different model than the one that answered (reduces self-preference bias)."""
    return replace(settings, model=judge_model) if judge_model else settings
