"""Export traces + the latest eval to one JSON file for the static dashboard.

Analogy: photocopying the case folders into a binder the public can read
without access to the filing cabinet. Only runs that belong to the chosen
eval are exported (clean, reproducible), and nothing from .env or settings is
included -- just questions, retrieved text, answers, spans and scores.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from acp import store
from acp.lineage import build_lineage, document_stats, link_claims, to_cypher


def _export_one(conn: sqlite3.Connection, ev: sqlite3.Row) -> dict[str, Any]:
    results = [
        json.loads(r["result"])
        for r in conn.execute("SELECT result FROM eval_results WHERE eval_id=? ORDER BY case_id", (ev["eval_id"],))
    ]
    runs = []
    for res in results:
        rid = res.get("run_id")
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone() if rid else None
        if row is None:
            continue
        run = dict(row)
        run["case_id"] = res["case_id"]
        run["category"] = res["category"]
        run["retrieved"] = json.loads(run.pop("retrieved_json") or "[]")
        run["spans"] = [
            {**sp, "attributes": json.loads(sp["attributes"] or "{}")} for sp in store.fetch_spans(conn, rid)
        ]
        runs.append(run)
    runs_by_id = {r["run_id"]: r for r in runs}
    for res in results:  # annotate each claim with the chunk that supports it (drives the Lineage tab)
        run = runs_by_id.get(res.get("run_id"))
        if run is not None:
            res["claims"] = link_claims(res.get("claims", []), run["retrieved"])
    graph = build_lineage(results, runs)
    return {
        "eval_id": ev["eval_id"],
        "created_at": ev["created_at"],
        "summary": json.loads(ev["summary"]),
        "results": results,
        "runs": runs,
        "doc_stats": document_stats(graph),
        "_graph": graph,  # stripped before writing JSON; used for the Cypher export
    }


def lineage_cypher(export: dict[str, Any]) -> str:
    """Cypher for the latest evaluation in an export (see docker-compose.yml for a local Neo4j)."""
    return to_cypher(export["evals"][0]["_graph"])


def strip_private(export: dict[str, Any]) -> dict[str, Any]:
    return {**export, "evals": [{k: v for k, v in e.items() if not k.startswith("_")} for e in export["evals"]]}


def build_export(
    conn: sqlite3.Connection, eval_id: str | None = None, history: int = 3, min_cases: int = 10
) -> dict[str, Any]:
    """Export the latest eval (and up to `history`-1 earlier full ones) so the dashboard can compare runs.

    Small smoke runs (fewer than `min_cases` questions) are skipped unless asked for by id.
    """
    if eval_id:
        rows = conn.execute("SELECT * FROM eval_runs WHERE eval_id=?", (eval_id,)).fetchall()
    else:
        rows = [
            r
            for r in conn.execute("SELECT * FROM eval_runs ORDER BY created_at DESC").fetchall()
            if json.loads(r["summary"])["n_cases"] >= min_cases
        ][:history]
    if not rows:
        raise ValueError("no eval run found: run `make eval` first")
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": rows[0]["model"],
        "evals": [_export_one(conn, r) for r in rows],
    }
