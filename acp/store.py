"""The SQLite trace store: schema + small read helpers.

Analogy: the filing cabinet. `runs` is one folder per question asked;
`spans` is the stack of timestamped sheets inside each folder (one sheet per
step or model call). SQLite because it is a single file, needs no server, and
is plenty for thousands of runs (see docs/DECISIONS.md).
"""

from __future__ import annotations

import random
import sqlite3
import time
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


# --- schema versions -------------------------------------------------------------------------------
# Analogy: numbered renovation permits. The database remembers which renovation it is on
# (`PRAGMA user_version`); on open we apply only the permits it has not had yet, each in one transaction.
# Version 1 = the original tables above. Version 2 = multi-user document support.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE runs ADD COLUMN user_id TEXT",
        "ALTER TABLE runs ADD COLUMN doc_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, started_at)",
        """CREATE TABLE IF NOT EXISTS users (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            key_hash   TEXT NOT NULL UNIQUE,   -- sha256 of the API key; the key itself is never stored
            key_prefix TEXT NOT NULL,          -- first characters, to recognise a key in logs/admin
            created_at TEXT NOT NULL,
            disabled   INTEGER NOT NULL DEFAULT 0,
            quota_json TEXT                    -- optional per-user overrides of the default quotas
        )""",
        """CREATE TABLE IF NOT EXISTS docs (
            id         TEXT PRIMARY KEY,
            owner_id   TEXT NOT NULL,
            sha256     TEXT NOT NULL,
            filename   TEXT NOT NULL,          -- display only; never used as a path
            pages      INTEGER,
            chunks     INTEGER,
            status     TEXT NOT NULL,          -- queued | parsing | embedding | ready | failed
            error      TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (owner_id, sha256)          -- uploading the same file twice reuses the first
        )""",
        "CREATE INDEX IF NOT EXISTS idx_docs_owner ON docs(owner_id)",
        """CREATE TABLE IF NOT EXISTS jobs (
            id          TEXT PRIMARY KEY,
            doc_id      TEXT NOT NULL,
            state       TEXT NOT NULL,         -- queued | parsing | chunking | embedding | indexing | ready | failed
            progress    INTEGER NOT NULL DEFAULT 0,
            total       INTEGER NOT NULL DEFAULT 0,
            message     TEXT,
            started_at  TEXT,
            finished_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_jobs_doc ON jobs(doc_id)",
        """CREATE TABLE IF NOT EXISTS chunks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id     TEXT NOT NULL,
            owner_id   TEXT NOT NULL,
            idx        INTEGER NOT NULL,       -- position in the document; equals the row in its FAISS index
            page       INTEGER NOT NULL,       -- 1-based PDF page index
            page_label TEXT,
            section    TEXT,
            kind       TEXT NOT NULL DEFAULT 'text',   -- text | summary
            text       TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, idx)",
        # Keyword (BM25) search. rowid is the chunks.id, so a hit maps straight back to its chunk.
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, doc_id UNINDEXED, owner_id UNINDEXED)",
        """CREATE TABLE IF NOT EXISTS usage (
            user_id TEXT NOT NULL,
            day     TEXT NOT NULL,             -- UTC date
            asks    INTEGER NOT NULL DEFAULT 0,
            tokens  INTEGER NOT NULL DEFAULT 0,
            uploads INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )""",
    ],
}
MIGRATIONS[3] = [
    # Tokens spent per model per UTC day (survives restarts, so the daily free-tier cap is honoured across runs).
    """CREATE TABLE IF NOT EXISTS model_usage (
        model  TEXT NOT NULL,
        day    TEXT NOT NULL,
        tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (model, day)
    )""",
    # Identical question on the same document returns the stored answer instead of spending tokens again.
    """CREATE TABLE IF NOT EXISTS answer_cache (
        cache_key  TEXT PRIMARY KEY,           -- sha256(owner, doc, normalized question)
        owner_id   TEXT NOT NULL,
        doc_id     TEXT NOT NULL,
        response   TEXT NOT NULL,              -- JSON of the API answer
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cache_doc ON answer_cache(doc_id)",
]
MIGRATIONS[4] = [
    # Money spent per model per day (Bedrock bills per token), used by the daily spend cap.
    "ALTER TABLE model_usage ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0",
]
MIGRATIONS[5] = [
    # Append-only, hash-chained record of what the assurance layer did (see acp/assure/audit.py).
    """CREATE TABLE IF NOT EXISTS audit_log (
        seq        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        actor      TEXT NOT NULL,
        action     TEXT NOT NULL,
        run_id     TEXT,
        detail     TEXT NOT NULL,              -- canonical JSON, already redacted
        prev_hash  TEXT NOT NULL,
        hash       TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id)",
]
LATEST = max(MIGRATIONS)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the database up to LATEST. Safe to call from several processes at once."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= LATEST:
        return  # hot path: one cheap read, no locks
    conn.execute("BEGIN IMMEDIATE")  # take the write lock; the loser of a race waits, then re-reads the version
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            fresh = not conn.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone()
            if fresh:
                for stmt in SCHEMA.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
            version = 1  # a database that already had `runs` is a version-1 database
        for target in sorted(v for v in MIGRATIONS if v > version):
            for stmt in MIGRATIONS[target]:
                conn.execute(stmt)
            version = target
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _setup(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=30000")  # wait for other writers instead of failing (parallel workers)
    conn.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
    _migrate(conn)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and migrate if needed) the database.

    The very first open of a brand-new file by several processes at once can still report "database is locked"
    (switching to WAL mode does not honour the busy timeout), so setup is retried briefly with jitter.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    for attempt in range(12):
        try:
            _setup(conn)
            return conn
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 11:
                conn.close()
                raise
            if conn.in_transaction:
                conn.rollback()
            time.sleep(0.05 * (attempt + 1) + random.random() * 0.05)
    return conn  # unreachable; keeps type checkers happy


def fetch_runs(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def fetch_spans(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM spans WHERE run_id = ? ORDER BY start_ms", (run_id,)).fetchall()
    return [dict(r) for r in rows]
