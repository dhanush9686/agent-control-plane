"""Evaluation history: one line per eval run, committed to git, drawn as trend charts.

Analogy: the pencil marks on a doorframe recording a child's height. The SQLite file is local and rebuilt in CI,
so the long-term record lives in results/history.jsonl, which a scheduled workflow appends to and commits.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from acp.config import ROOT

HISTORY_PATH = ROOT / "results" / "history.jsonl"
MIN_CASES = 10  # smoke runs of a few questions are not trend points
METRICS = (
    "answered_correctly_rate",
    "hallucination_rate",
    "refusal_correctness",
    "citation_rate",
    "pass_rate",
    "avg_latency_ms",
    "avg_cost_usd",
)


def entry_from_eval(eval_id: str, created_at: str, model: str | None, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "eval_id": eval_id,
        "created_at": created_at,
        "agent": summary.get("agent", "graph"),
        "agent_model": summary.get("agent_model") or model,
        "judge_model": summary.get("judge_model", "same"),
        "label": summary.get("label"),
        "n_cases": summary["n_cases"],
        "n_errors": summary.get("n_errors", 0),
        **{m: summary.get(m) for m in METRICS},
        "ci95": summary.get("ci95"),
    }


def load(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def append_missing(entries: list[dict[str, Any]], path: Path = HISTORY_PATH) -> int:
    """Append entries whose eval_id is not yet recorded (idempotent). Returns how many were added."""
    have = {e["eval_id"] for e in load(path)}
    new = [e for e in entries if e["eval_id"] not in have and e["n_cases"] >= MIN_CASES]
    if new:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for e in sorted(new, key=lambda x: x["created_at"]):
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(new)


def sync_from_db(conn: sqlite3.Connection, path: Path = HISTORY_PATH) -> int:
    rows = conn.execute("SELECT * FROM eval_runs ORDER BY created_at").fetchall()
    return append_missing(
        [entry_from_eval(r["eval_id"], r["created_at"], r["model"], json.loads(r["summary"])) for r in rows], path
    )
