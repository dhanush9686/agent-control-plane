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


def build_export(conn: sqlite3.Connection, eval_id: str | None = None) -> dict[str, Any]:
    if eval_id:
        ev = conn.execute("SELECT * FROM eval_runs WHERE eval_id=?", (eval_id,)).fetchone()
    else:
        ev = conn.execute("SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if ev is None:
        raise ValueError("no eval run found: run `make eval` first")
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
        run["spans"] = [{**s, "attributes": json.loads(s["attributes"] or "{}")} for s in store.fetch_spans(conn, rid)]
        runs.append(run)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": ev["model"],
        "eval": {
            "eval_id": ev["eval_id"],
            "created_at": ev["created_at"],
            "summary": json.loads(ev["summary"]),
            "results": results,
        },
        "runs": runs,
    }
