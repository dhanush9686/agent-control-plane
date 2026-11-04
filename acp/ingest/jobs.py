"""Document and job bookkeeping (SQLite): what was uploaded, and how far along its processing is.

Analogy: the parcel tracking page. Each upload gets a tracking number (job id) and a status line you can poll:
"parsing... embedding 40%... ready".
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

ACTIVE = ("queued", "parsing", "chunking", "embedding", "ready_check")
TERMINAL = ("ready", "failed")
# Share of the progress bar each stage owns (sums to 100). Embedding + uploading vectors is the slow part.
STAGE_WEIGHTS = {"queued": (0, 0), "parsing": (0, 20), "chunking": (20, 25), "embedding": (25, 99), "ready": (100, 100)}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def overall_percent(state: str, progress: int, total: int) -> int:
    """0-100 for the whole job, from the current stage and its own progress. Never decreases as stages advance."""
    if state == "failed":
        return 0
    lo, hi = STAGE_WEIGHTS.get(state, (0, 0))
    if state in ("ready", "queued") or total <= 0:
        return lo
    return int(lo + (hi - lo) * min(1.0, max(0.0, progress / total)))


def register_document(conn: sqlite3.Connection, owner_id: str, sha256: str, filename: str) -> tuple[str, str, bool]:
    """Create the document + a queued job. Returns (doc_id, job_id, is_new).

    Uploading the same bytes twice reuses the first document (UNIQUE(owner_id, sha256)); a document whose earlier
    attempt failed gets a fresh job so the user can simply upload again.
    """
    with conn:
        row = conn.execute("SELECT id, status FROM docs WHERE owner_id=? AND sha256=?", (owner_id, sha256)).fetchone()
        if row is not None:
            doc_id = row["id"]
            if row["status"] == "failed":
                conn.execute("UPDATE docs SET status='queued', error=NULL WHERE id=?", (doc_id,))
                job_id = new_id()
                conn.execute(
                    "INSERT INTO jobs (id, doc_id, state, started_at) VALUES (?, ?, 'queued', ?)",
                    (job_id, doc_id, now_iso()),
                )
                return doc_id, job_id, True
            last = conn.execute(
                "SELECT id FROM jobs WHERE doc_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", (doc_id,)
            ).fetchone()
            return doc_id, last["id"] if last else "", False
        doc_id, job_id = new_id(), new_id()
        conn.execute(
            "INSERT INTO docs (id, owner_id, sha256, filename, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (doc_id, owner_id, sha256, filename[:200], now_iso()),
        )
        conn.execute(
            "INSERT INTO jobs (id, doc_id, state, started_at) VALUES (?, ?, 'queued', ?)", (job_id, doc_id, now_iso())
        )
        return doc_id, job_id, True


def update_job(
    conn: sqlite3.Connection, job_id: str, state: str, progress: int = 0, total: int = 0, message: str | None = None
) -> None:
    """Move a job forward. Within one stage the progress counter only ever increases."""
    with conn:
        cur = conn.execute("SELECT state, progress FROM jobs WHERE id=?", (job_id,)).fetchone()
        if cur is not None and cur["state"] == state:
            progress = max(progress, cur["progress"])
        finished = now_iso() if state in TERMINAL else None
        conn.execute(
            "UPDATE jobs SET state=?, progress=?, total=?, message=COALESCE(?, message), "
            "finished_at=COALESCE(?, finished_at) WHERE id=?",
            (state, progress, total, message, finished, job_id),
        )
        if state in ("parsing", "chunking", "embedding"):
            conn.execute("UPDATE docs SET status=? WHERE id=(SELECT doc_id FROM jobs WHERE id=?)", (state, job_id))


def finish_document(conn: sqlite3.Connection, job_id: str, doc_id: str, *, pages: int, chunks: int) -> None:
    with conn:
        conn.execute(
            "UPDATE docs SET status='ready', pages=?, chunks=?, error=NULL WHERE id=?", (pages, chunks, doc_id)
        )
        conn.execute("UPDATE jobs SET state='ready', progress=total, finished_at=? WHERE id=?", (now_iso(), job_id))


def fail_document(conn: sqlite3.Connection, job_id: str, doc_id: str, message: str) -> None:
    with conn:
        conn.execute("UPDATE docs SET status='failed', error=? WHERE id=?", (message[:500], doc_id))
        conn.execute(
            "UPDATE jobs SET state='failed', message=?, finished_at=? WHERE id=?", (message[:500], now_iso(), job_id)
        )


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["percent"] = overall_percent(d["state"], d["progress"], d["total"])
    return d


def get_doc(conn: sqlite3.Connection, doc_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
    """Fetch a document. When `owner_id` is given, another owner's document looks exactly like a missing one."""
    row = conn.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if row is None or (owner_id is not None and row["owner_id"] != owner_id):
        return None
    return dict(row)
