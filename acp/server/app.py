"""The HTTP API.

Analogy: the front desk of an archive. Visitors show a key card (API key), hand in documents, and ask the
librarian questions. The desk checks the card, enforces visiting limits, and never lets one visitor see another's
files. Everything slow (reading a 500-page book) happens in the back room (the background worker); the desk just
hands out a tracking number.

`create_app` takes its dependencies as arguments, so tests can run the entire API against fakes (no Groq, no
Pinecone, no PyTorch) and production wires in the real ones (see acp/server/main.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from acp import store
from acp.config import Settings
from acp.errors import ACPError, RateLimited
from acp.hybrid import DocRetriever
from acp.ingest import jobs
from acp.ingest.pdf import PdfError, looks_like_pdf
from acp.ingest.worker import JobRunner
from acp.kb import KnowledgeBase
from acp.llm import LLM
from acp.nodes import REFUSAL, extract_citations
from acp.ratelimit import DailyQuota, SlidingWindow
from acp.runner import Agent
from acp.server import auth
from acp.tracing import Tracing

log = logging.getLogger("acp.server")
STATIC_DIR = Path(__file__).parent / "static"
READ_CHUNK = 1024 * 1024
PDF_STATUS = {
    "too_many_pages": 422,
    "encrypted": 422,
    "scanned": 422,
    "corrupt": 422,
    "too_many_chunks": 422,
    "timeout": 422,
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    # Everything the page needs is served from our own origin; no inline scripts, no third-party resources.
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


@dataclass
class AppDeps:
    settings: Settings
    kb: KnowledgeBase
    runner: JobRunner
    llm_for_user: Callable[[], LLM]  # a budget-aware LLM (see ratelimit.BudgetedLLM) for one request
    ask_limiter: SlidingWindow
    auth_fail_limiter: SlidingWindow
    quota: DailyQuota
    tracing: Tracing | None = None


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


def _norm_question(q: str) -> str:
    return " ".join(q.lower().split())


def _cache_key(owner_id: str, doc_id: str, question: str) -> str:
    return hashlib.sha256(f"{owner_id}|{doc_id}|{_norm_question(question)}".encode()).hexdigest()


def _too_many(message: str, retry_after: float, scope: str) -> JSONResponse:
    return JSONResponse(
        {"error": message, "scope": scope, "retry_after_seconds": int(max(1, retry_after))},
        status_code=429,
        headers={"Retry-After": str(int(max(1, retry_after)))},
    )


def create_app(deps: AppDeps) -> FastAPI:
    s = deps.settings
    app = FastAPI(title="Agent Control Plane", version="1.0", docs_url="/docs", redoc_url=None)

    def conn_factory():  # noqa: ANN202
        return store.connect(s.db_path)

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def secure(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = uuid.uuid4().hex[:12]
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last line of defence: never leak a stack trace
            log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
            response = JSONResponse({"error": "Internal error. Please try again.", "request_id": rid}, status_code=500)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(RateLimited)
    async def _rate_limited(_: Request, exc: RateLimited):  # noqa: ANN202
        return _too_many(str(exc), exc.retry_after, exc.scope)

    @app.exception_handler(ACPError)
    async def _acp_error(_: Request, exc: ACPError):  # noqa: ANN202
        if isinstance(exc, PdfError):
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=PDF_STATUS.get(exc.code, 422))
        return JSONResponse({"error": str(exc)}, status_code=503)

    # ------------------------------------------------------------------ auth
    def current_user(request: Request, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
        ip = request.client.host if request.client else "unknown"
        wait = deps.auth_fail_limiter.peek(ip) if hasattr(deps.auth_fail_limiter, "peek") else 0.0
        if wait > 0:
            raise RateLimited("Too many failed sign-in attempts. Please wait.", wait, "auth")
        conn = conn_factory()
        try:
            user = auth.authenticate(conn, x_api_key)
        finally:
            conn.close()
        if user is None:
            deps.auth_fail_limiter.hit(ip)  # only failures count toward the limit
            raise HTTPException(401, "Missing or invalid API key.", headers={"WWW-Authenticate": "ApiKey"})
        return user

    # ------------------------------------------------------------------ helpers
    def doc_or_404(conn, doc_id: str, user: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
        from acp.kb import safe_id

        try:
            safe_id(doc_id)
        except ACPError:
            raise HTTPException(404, "Document not found.") from None
        doc = jobs.get_doc(conn, doc_id, user["id"])  # another user's document is indistinguishable from a missing one
        if doc is None:
            raise HTTPException(404, "Document not found.")
        return doc

    def public_doc(d: dict[str, Any], job: dict[str, Any] | None = None) -> dict[str, Any]:
        out = {k: d[k] for k in ("id", "filename", "pages", "chunks", "status", "error", "created_at")}
        if job:
            out["job"] = {"id": job["id"], "state": job["state"], "percent": job["percent"], "message": job["message"]}
        return out

    # ------------------------------------------------------------------ routes
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "queued_jobs": deps.runner.pending()}

    @app.get("/v1/me")
    def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        rem = deps.quota.remaining(user["id"], s.daily_asks, s.daily_uploads)
        return {
            "name": user["name"],
            "remaining_today": rem,
            "limits": {"asks_per_day": s.daily_asks, "uploads_per_day": s.daily_uploads},
        }

    @app.post("/v1/documents", status_code=202)
    def upload(
        request: Request, file: UploadFile = File(...), user: dict[str, Any] = Depends(current_user)
    ) -> JSONResponse:
        cap = s.max_upload_mb * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap + 4096:
            return JSONResponse({"error": f"File too large (limit {s.max_upload_mb} MB)."}, status_code=413)
        owner = user["id"]
        staging = s.kb_root / owner / "_incoming"
        staging.mkdir(parents=True, exist_ok=True)
        tmp = staging / f"{uuid.uuid4().hex}.part"
        sha, size = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as out:
                while chunk := file.file.read(READ_CHUNK):
                    size += len(chunk)
                    if size > cap:
                        return JSONResponse({"error": f"File too large (limit {s.max_upload_mb} MB)."}, status_code=413)
                    sha.update(chunk)
                    out.write(chunk)
            if size == 0:
                return JSONResponse({"error": "The file is empty."}, status_code=422)
            if not looks_like_pdf(tmp):
                return JSONResponse({"error": "This does not look like a PDF file."}, status_code=415)
            digest = sha.hexdigest()
            conn = conn_factory()
            try:
                existing = conn.execute("SELECT id FROM docs WHERE owner_id=? AND sha256=?", (owner, digest)).fetchone()
                if existing is None:  # only genuinely new files consume the daily upload allowance
                    wait = deps.quota.consume_upload(owner, s.daily_uploads)
                    if wait > 0:
                        return _too_many("Daily upload limit reached.", wait, "daily_uploads")
                name = Path(file.filename or "document.pdf").name or "document.pdf"
                doc_id, job_id, is_new = jobs.register_document(conn, owner, digest, name)
                if is_new:
                    d = deps.kb.doc_dir(owner, doc_id)
                    d.mkdir(parents=True, exist_ok=True)
                    tmp.replace(deps.kb.pdf_path(owner, doc_id))
                    deps.runner.submit(job_id)
                doc = jobs.get_doc(conn, doc_id, owner)
                job = jobs.get_job(conn, job_id) if job_id else None
                return JSONResponse(
                    {**public_doc(doc, job), "duplicate": not is_new}, status_code=202 if is_new else 200
                )
            finally:
                conn.close()
        finally:
            tmp.unlink(missing_ok=True)

    @app.get("/v1/documents")
    def list_documents(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        conn = conn_factory()
        try:
            rows = conn.execute(
                "SELECT * FROM docs WHERE owner_id=? ORDER BY created_at DESC", (user["id"],)
            ).fetchall()
            out = []
            for r in rows:
                job = conn.execute(
                    "SELECT id FROM jobs WHERE doc_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", (r["id"],)
                ).fetchone()
                out.append(public_doc(dict(r), jobs.get_job(conn, job["id"]) if job else None))
            return {"documents": out}
        finally:
            conn.close()

    @app.get("/v1/documents/{doc_id}")
    def get_document(doc_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        conn = conn_factory()
        try:
            doc = doc_or_404(conn, doc_id, user)
            job = conn.execute(
                "SELECT id FROM jobs WHERE doc_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", (doc_id,)
            ).fetchone()
            return public_doc(doc, jobs.get_job(conn, job["id"]) if job else None)
        finally:
            conn.close()

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        conn = conn_factory()
        try:
            job = jobs.get_job(conn, job_id)
            if job is None or jobs.get_doc(conn, job["doc_id"], user["id"]) is None:
                raise HTTPException(404, "Job not found.")
            return {k: job[k] for k in ("id", "doc_id", "state", "percent", "progress", "total", "message")}
        finally:
            conn.close()

    @app.post("/v1/documents/{doc_id}/ask")
    def ask(doc_id: str, body: AskBody, user: dict[str, Any] = Depends(current_user)) -> JSONResponse:
        owner = user["id"]
        conn = conn_factory()
        try:
            doc = doc_or_404(conn, doc_id, user)
            if doc["status"] != "ready":
                return JSONResponse(
                    {"error": f"This document is not ready yet (status: {doc['status']}).", "status": doc["status"]},
                    status_code=409,
                )
            wait = deps.ask_limiter.hit(owner)
            if wait > 0:
                return _too_many("You are asking too quickly. Please slow down.", wait, "rate")
            key = _cache_key(owner, doc_id, body.question)
            cached = conn.execute("SELECT response FROM answer_cache WHERE cache_key=?", (key,)).fetchone()
            remaining = lambda: deps.quota.remaining(owner, s.daily_asks, s.daily_uploads)["asks"]  # noqa: E731
            if cached is not None:  # identical question on the same document: free, instant, no tokens
                return JSONResponse(
                    {**json.loads(cached["response"]), "cached": True, "asks_remaining_today": remaining()}
                )
            wait = deps.quota.consume_ask(owner, s.daily_asks)
            if wait > 0:
                return _too_many("Daily question limit reached.", wait, "daily_asks")
        finally:
            conn.close()

        doc_settings = replace(
            s, top_k=6, domain_name="the uploaded document", citation_example=f"[{doc['filename']} p.37]"
        )
        retriever = DocRetriever(
            deps.kb, conn_factory, owner, doc_id, doc["filename"], mode="hybrid", reranker=_reranker(deps)
        )
        agent = Agent(deps.llm_for_user(), retriever, doc_settings, deps.tracing)
        try:
            final, run_id = agent.ask(body.question, user_id=owner, doc_id=doc_id)
        except RateLimited:
            deps.quota.refund_ask(owner)  # the question never ran: give the allowance back
            raise
        answer = final["answer"]
        retrieved = final.get("retrieved_docs", [])
        by_label = {d.metadata.get("citation"): d for d in retrieved}
        citations = []
        for label in extract_citations(answer):
            d = by_label.get(label)
            if d is not None:
                citations.append(
                    {
                        "label": label,
                        "page": d.metadata.get("page"),
                        "section": d.metadata.get("section"),
                        "snippet": d.page_content[:300],
                    }
                )
        payload = {
            "answer": answer,
            "refused": REFUSAL.rstrip(".").lower() in answer.lower(),
            "citations": citations,
            "attempts": final.get("search_attempts"),
            "trace_id": run_id,
        }
        c2 = conn_factory()
        try:
            with c2:
                c2.execute(
                    "INSERT OR REPLACE INTO answer_cache (cache_key, owner_id, doc_id, response, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (key, owner, doc_id, json.dumps(payload), jobs.now_iso()),
                )
        finally:
            c2.close()
        return JSONResponse({**payload, "cached": False, "asks_remaining_today": remaining()})

    @app.delete("/v1/documents/{doc_id}", status_code=204)
    def delete_document(
        doc_id: str, purge_traces: bool = True, user: dict[str, Any] = Depends(current_user)
    ) -> Response:
        owner = user["id"]
        conn = conn_factory()
        try:
            doc_or_404(conn, doc_id, user)
            deps.kb.delete_document(conn, owner, doc_id)
            with conn:
                conn.execute("DELETE FROM answer_cache WHERE doc_id=? AND owner_id=?", (doc_id, owner))
                conn.execute("DELETE FROM jobs WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM docs WHERE id=? AND owner_id=?", (doc_id, owner))
                if purge_traces:
                    conn.execute(
                        "DELETE FROM spans WHERE run_id IN (SELECT run_id FROM runs WHERE doc_id=? AND user_id=?)",
                        (doc_id, owner),
                    )
                    conn.execute("DELETE FROM runs WHERE doc_id=? AND user_id=?", (doc_id, owner))
        finally:
            conn.close()
        return Response(status_code=204)

    @app.get("/v1/traces/{trace_id}")
    def get_trace(trace_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        conn = conn_factory()
        try:
            run = conn.execute("SELECT * FROM runs WHERE run_id=? AND user_id=?", (trace_id, user["id"])).fetchone()
            if run is None:
                raise HTTPException(404, "Trace not found.")
            run = dict(run)
            run.pop("retrieved_json", None)
            spans = [dict(r) for r in conn.execute("SELECT * FROM spans WHERE run_id=? ORDER BY start_ms", (trace_id,))]
            for sp in spans:
                sp["attributes"] = json.loads(sp["attributes"] or "{}")
            return {"run": run, "spans": spans}
        finally:
            conn.close()

    # ------------------------------------------------------------------ the chat page
    if STATIC_DIR.is_dir() and (STATIC_DIR / "index.html").is_file():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _reranker(deps: AppDeps):  # noqa: ANN202
    if not deps.settings.rerank:
        return None
    from acp.rerank import CrossEncoderReranker

    return CrossEncoderReranker()
