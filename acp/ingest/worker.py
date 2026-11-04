"""A single background worker that processes ingestion jobs one at a time.

Analogy: one mail-room clerk. Embedding a book is heavy (CPU and memory), so we deliberately do ONE at a time;
other uploads wait in line (their status says "queued").
"""

from __future__ import annotations

import logging
import queue
import threading

from acp.ingest.pipeline import IngestDeps, recover_interrupted, run_job

log = logging.getLogger("acp.ingest")


class JobRunner:
    def __init__(self, deps: IngestDeps) -> None:
        self._deps = deps
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Recover interrupted jobs, then start the worker thread."""
        for job_id in recover_interrupted(self._deps):
            self._queue.put(job_id)
        self._thread = threading.Thread(target=self._loop, name="ingest-worker", daemon=True)
        self._thread.start()

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def pending(self) -> int:
        return self._queue.qsize()

    def stop(self, timeout: float = 10.0) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout)

    def drain(self, timeout: float = 60.0) -> None:
        """Block until the queue is empty and the current job is done (used by tests)."""
        self._queue.join()

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                run_job(job_id, self._deps)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("worker error on job %s", job_id)
            finally:
                self._queue.task_done()
