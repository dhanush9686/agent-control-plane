"""The SQLite trace store: schema + small read helpers.

Analogy: the filing cabinet. `runs` is one folder per question asked;
`spans` is the stack of timestamped sheets inside each folder (one sheet per
step or model call). SQLite because it is a single file, needs no server, and
is plenty for thousands of runs (see docs/DECISIONS.md).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    question      TEXT NOT NULL,
    started_at    TEXT NOT NULL,          -- ISO-8601 UTC
    latency_ms    REAL,
    status        TEXT NOT NULL,          -- ok | error
    error         TEXT,
    answer        TEXT,
    attempts      INTEGER,
    sufficient    INTEGER,                -- the judge's final verdict (0/1)
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0,
    llm_calls     INTEGER DEFAULT 0,
    retries       INTEGER DEFAULT 0,
    retrieved_json TEXT                   -- [{"source":..., "text":...}] shown in failure drill-down
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS spans (
    span_id       TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    parent_id     TEXT,
    name          TEXT NOT NULL,          -- e.g. node.judge, llm.synthesize
    kind          TEXT NOT NULL,          -- run | node | llm
    start_ms      REAL NOT NULL,          -- offset from run start, for the waterfall
    latency_ms    REAL NOT NULL,
    status        TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    retries       INTEGER,
    attributes    TEXT                    -- JSON blob: judge verdict, query, sources...
);
CREATE INDEX IF NOT EXISTS idx_spans_run ON spans(run_id);
CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name);

CREATE TABLE IF NOT EXISTS eval_runs (
    eval_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    model      TEXT,
    summary    TEXT                       -- JSON of the aggregate report
);

CREATE TABLE IF NOT EXISTS eval_results (
    eval_id    TEXT NOT NULL,
    case_id    TEXT NOT NULL,
    run_id     TEXT,                      -- links to runs.run_id (the trace of that question)
    category   TEXT,
    result     TEXT NOT NULL,             -- JSON: scores, claims, failures
    PRIMARY KEY (eval_id, case_id)
);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create if needed) the trace DB with the schema applied."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")  # wait for other writers instead of failing (parallel eval workers)
    conn.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
    has_schema = conn.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone()
    if not has_schema:  # skip DDL on the hot path: it needs a write lock every time
        conn.executescript(SCHEMA)
    return conn


def fetch_runs(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def fetch_spans(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM spans WHERE run_id = ? ORDER BY start_ms", (run_id,)).fetchall()
    return [dict(r) for r in rows]
