"""Run one ingestion job: parse -> chunk -> embed + upload -> ready.

Analogy: the assembly line foreman. Each station reports progress; if any station fails, the foreman clears the
line (no half-built product left behind) and writes a plain-language reason on the tracking page.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from acp import store
from acp.config import Settings
from acp.errors import ACPError
from acp.ingest import jobs
from acp.ingest.chunking import chunk_document
from acp.ingest.parse_worker import parse_pdf
from acp.ingest.pdf import ParsedPdf
from acp.kb import KnowledgeBase

log = logging.getLogger("acp.ingest")
GENERIC_FAILURE = "Something went wrong while processing this file. Please try again, or try a different PDF."


@dataclass
class IngestDeps:
    settings: Settings
    kb: KnowledgeBase
    parse: Callable[[Path, Path, int, int], ParsedPdf] = parse_pdf  # replaceable in tests (e.g. to simulate a crash)


def run_job(job_id: str, deps: IngestDeps) -> None:
    """Process one job to completion. Never raises: every outcome is recorded on the job and document rows."""
    s, kb = deps.settings, deps.kb
    conn = store.connect(s.db_path)
    try:
        job = jobs.get_job(conn, job_id)
        doc = jobs.get_doc(conn, job["doc_id"]) if job else None
        if job is None or doc is None:
            log.error("job %s has no document", job_id)
            return
        owner_id, doc_id = doc["owner_id"], doc["id"]
        pdf = kb.pdf_path(owner_id, doc_id)
        try:
            jobs.update_job(conn, job_id, "parsing", 0, 1)
            parsed = deps.parse(pdf, kb.doc_dir(owner_id, doc_id) / "work", s.max_pages, s.parse_timeout_s)
            jobs.update_job(conn, job_id, "parsing", 1, 1)

            jobs.update_job(conn, job_id, "chunking", 0, 1)
            chunks = chunk_document(parsed, s.chunk_chars, s.chunk_overlap, s.max_chunks)
            jobs.update_job(conn, job_id, "chunking", 1, 1)

            def progress(done: int, total: int) -> None:
                jobs.update_job(conn, job_id, "embedding", done, total)

            jobs.update_job(conn, job_id, "embedding", 0, len(chunks) * 2)
            kb.delete_document_data_only(conn, owner_id, doc_id)  # a retry must not double-insert chunks/vectors
            kb.add_document(conn, owner_id, doc_id, chunks, progress)
            shutil.rmtree(kb.doc_dir(owner_id, doc_id) / "work", ignore_errors=True)
            jobs.finish_document(conn, job_id, doc_id, pages=len(parsed.pages), chunks=len(chunks))
        except ACPError as e:  # refusals with a user-friendly message (PdfError, VectorStoreError, ...)
            _fail(conn, kb, job_id, owner_id, doc_id, str(e))
        except Exception:  # noqa: BLE001 - never leak internals to the user; keep them in the log
            log.exception("ingest job %s crashed", job_id)
            _fail(conn, kb, job_id, owner_id, doc_id, GENERIC_FAILURE)
    finally:
        conn.close()


def _fail(conn, kb: KnowledgeBase, job_id: str, owner_id: str, doc_id: str, message: str) -> None:  # noqa: ANN001
    """Clear partial data (rows, vectors, the stored PDF) and record why it failed."""
    try:
        kb.delete_document(conn, owner_id, doc_id)
    except Exception:  # noqa: BLE001
        log.exception("cleanup after failed job %s also failed", job_id)
    jobs.fail_document(conn, job_id, doc_id, message)


def recover_interrupted(deps: IngestDeps) -> list[str]:
    """After a restart: re-queue jobs that were mid-flight if their PDF still exists, else mark them failed.

    Returns the job ids that should be run again.
    """
    s, kb = deps.settings, deps.kb
    conn = store.connect(s.db_path)
    requeue: list[str] = []
    try:
        rows = conn.execute(
            "SELECT j.id AS job_id, d.id AS doc_id, d.owner_id FROM jobs j JOIN docs d ON d.id = j.doc_id "
            f"WHERE j.state IN ({','.join('?' * len(jobs.ACTIVE))})",
            jobs.ACTIVE,
        ).fetchall()
        for r in rows:
            if kb.pdf_path(r["owner_id"], r["doc_id"]).is_file():
                jobs.update_job(conn, r["job_id"], "queued", 0, 0, "Resuming after a restart")
                requeue.append(r["job_id"])
            else:
                jobs.fail_document(
                    conn, r["job_id"], r["doc_id"], "Processing was interrupted by a restart. Please upload again."
                )
    finally:
        conn.close()
    return requeue
