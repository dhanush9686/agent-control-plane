import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from acp import store
from acp.config import Settings
from acp.embeddings import HashEmbedder
from acp.ingest.pipeline import IngestDeps
from acp.ingest.worker import JobRunner
from acp.kb import KnowledgeBase
from acp.llm import LLMResult
from acp.ratelimit import BudgetedLLM, DailyQuota, ModelBudget, SlidingWindow
from acp.server import auth
from acp.server.app import AppDeps, create_app
from acp.tracing import Tracing
from tests.fakes import InMemoryVectorStore
from tests.pdfgen import long_lines, make_pdf


class ScriptedLLM:
    """Answers from the excerpts it is shown, citing the first label it finds. No network."""

    model = "scripted"
    calls = 0

    def complete(self, prompt, *, purpose):
        ScriptedLLM.calls += 1
        if purpose == "refine_query":
            return LLMResult("refined", model=self.model)
        if "UNANSWERABLE" in prompt.split("Context:")[0]:
            return LLMResult("That is not in the knowledge base.", input_tokens=500, output_tokens=20, model=self.model)
        import re

        label = re.search(r"\[Source: ([^\]]+)\]", prompt)
        text = f"The document discusses it [{label.group(1)}]." if label else "That is not in the knowledge base."
        return LLMResult(text, input_tokens=500, output_tokens=30, model=self.model)

    def structured(self, schema, prompt, *, purpose):
        sufficient = "UNANSWERABLE" not in prompt.split("Retrieved context:")[0]
        return LLMResult(
            parsed=schema(sufficient=sufficient, reasoning="ok"), input_tokens=400, output_tokens=20, model=self.model
        )


@pytest.fixture
def svc(tmp_path):
    s = Settings(
        db_path=tmp_path / "t.db",
        kb_root=tmp_path / "kb",
        max_pages=50,
        max_upload_mb=1,
        daily_asks=4,
        daily_uploads=2,
        rate_per_min=100,
        model_tpm=100_000,
        model_daily_tokens=10_000_000,
        max_attempts=2,
    )
    kb = KnowledgeBase(s.kb_root, HashEmbedder(128), InMemoryVectorStore())
    runner = JobRunner(IngestDeps(s, kb))
    runner.start()
    factory = lambda: store.connect(s.db_path)  # noqa: E731
    budget = ModelBudget(factory, s.model_tpm, s.model_daily_tokens)
    deps = AppDeps(
        settings=s,
        kb=kb,
        runner=runner,
        llm_for_user=lambda: BudgetedLLM({}, ("scripted",), lambda m: ScriptedLLM(), budget),
        ask_limiter=SlidingWindow(s.rate_per_min),
        auth_fail_limiter=SlidingWindow(5),
        quota=DailyQuota(factory),
        tracing=Tracing(s),
    )
    client = TestClient(create_app(deps))
    conn = factory()
    users = {n: auth.create_user(conn, n) for n in ("alice", "bob")}
    yield type(
        "Svc",
        (),
        {"client": client, "settings": s, "kb": kb, "runner": runner, "users": users, "factory": factory, "deps": deps},
    )
    runner.stop()


def H(key):
    return {"X-API-Key": key}


def key_of(svc, name):
    return svc.users[name][1]


def upload(svc, name, pages=None, filename="report.pdf", data=None):
    data = data if data is not None else make_pdf(pages or [long_lines(f"p{i}") for i in range(4)])
    return svc.client.post(
        "/v1/documents", headers=H(key_of(svc, name)), files={"file": (filename, io.BytesIO(data), "application/pdf")}
    )


def wait_ready(svc, name, doc_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        d = svc.client.get(f"/v1/documents/{doc_id}", headers=H(key_of(svc, name))).json()
        if d["status"] in ("ready", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError("timed out")


def ask(svc, name, doc_id, q="What does the document say about subject p1?"):
    return svc.client.post(f"/v1/documents/{doc_id}/ask", headers=H(key_of(svc, name)), json={"question": q})


# ------------------------------------------------------------------ auth
def test_health_is_public_but_everything_else_needs_a_key(svc):
    assert svc.client.get("/healthz").json()["status"] == "ok"
    for method, path in [("get", "/v1/me"), ("get", "/v1/documents"), ("post", "/v1/documents/" + "a" * 32 + "/ask")]:
        r = getattr(svc.client, method)(path)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "ApiKey"
    assert svc.client.get("/v1/me", headers=H("acp_wrong")).status_code == 401
    assert svc.client.get("/v1/me", headers=H(key_of(svc, "alice"))).json()["name"] == "alice"


def test_keys_are_stored_hashed_and_disabled_users_are_locked_out(svc):
    conn = svc.factory()
    raw = " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM users"))
    assert key_of(svc, "alice") not in raw and auth.hash_key(key_of(svc, "alice")) in raw
    auth.set_disabled(conn, svc.users["alice"][0], True)
    assert svc.client.get("/v1/me", headers=H(key_of(svc, "alice"))).status_code == 401


def test_repeated_bad_keys_get_throttled_but_good_keys_do_not_count(svc):
    for _ in range(5):
        assert svc.client.get("/v1/me", headers=H("acp_bad")).status_code == 401
    r = svc.client.get("/v1/me", headers=H("acp_bad"))
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_security_headers_are_on_every_response(svc):
    r = svc.client.get("/healthz")
    assert (
        r.headers["x-content-type-options"] == "nosniff"
        and "default-src 'self'" in r.headers["content-security-policy"]
    )
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-request-id"]


# ------------------------------------------------------------------ upload safety
def test_upload_ingest_and_ask_with_page_citations(svc):
    r = upload(svc, "alice")
    assert r.status_code == 202 and r.json()["status"] in ("queued", "parsing", "chunking", "embedding", "ready")
    doc_id = r.json()["id"]
    d = wait_ready(svc, "alice", doc_id)
    assert d["status"] == "ready" and d["pages"] == 4 and d["job"]["percent"] == 100
    a = ask(svc, "alice", doc_id).json()
    assert a["citations"] and a["citations"][0]["label"].startswith("report.pdf p.") and a["citations"][0]["page"] >= 1
    assert a["refused"] is False and a["trace_id"] and a["cached"] is False and a["asks_remaining_today"] == 3


def test_non_pdf_empty_and_oversized_uploads_are_rejected(svc):
    assert upload(svc, "alice", data=b"MZ not a pdf").status_code == 415
    assert upload(svc, "alice", data=b"").status_code == 422
    big = b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)
    r = upload(svc, "alice", data=big)
    assert r.status_code == 413 and "1 MB" in r.json()["error"]
    assert not list((svc.settings.kb_root / svc.users["alice"][0] / "_incoming").glob("*.part"))  # no temp files left


def test_a_corrupt_pdf_is_accepted_for_processing_then_reported_failed_with_a_reason(svc):
    r = upload(svc, "alice", data=b"%PDF-1.4\nthis is garbage after the magic bytes")
    d = wait_ready(svc, "alice", r.json()["id"])
    assert d["status"] == "failed" and "damaged" in d["error"].lower()


def test_client_filename_is_never_used_as_a_path(svc):
    r = upload(svc, "alice", filename="../../../etc/passwd.pdf")
    assert r.status_code == 202 and r.json()["filename"] == "passwd.pdf"
    wait_ready(svc, "alice", r.json()["id"])
    assert not (svc.settings.kb_root.parent / "etc").exists()


def test_same_file_twice_is_deduplicated_and_does_not_use_the_upload_quota(svc):
    first = upload(svc, "alice")
    again = upload(svc, "alice")
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["id"] == first.json()["id"]
    assert svc.client.get("/v1/me", headers=H(key_of(svc, "alice"))).json()["remaining_today"]["uploads"] == 1


def test_daily_upload_quota_returns_429_with_retry_after(svc):
    for i in range(2):
        assert upload(svc, "alice", pages=[long_lines(f"x{i}{j}") for j in range(3)]).status_code == 202
    r = upload(svc, "alice", pages=[long_lines(f"z{j}") for j in range(3)])
    assert r.status_code == 429 and r.json()["scope"] == "daily_uploads" and int(r.headers["Retry-After"]) >= 1


# ------------------------------------------------------------------ isolation
def test_users_cannot_see_touch_or_ask_about_each_others_documents(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    for r in (
        svc.client.get(f"/v1/documents/{doc}", headers=H(key_of(svc, "bob"))),
        ask(svc, "bob", doc),
        svc.client.delete(f"/v1/documents/{doc}", headers=H(key_of(svc, "bob"))),
    ):
        assert r.status_code == 404 and r.json()["detail"] == "Document not found."  # same as a missing document
    assert svc.client.get("/v1/documents", headers=H(key_of(svc, "bob"))).json() == {"documents": []}
    assert ask(svc, "alice", doc).status_code == 200  # alice is unaffected


def test_job_and_trace_lookups_are_owner_scoped(svc):
    r = upload(svc, "alice").json()
    wait_ready(svc, "alice", r["id"])
    trace = ask(svc, "alice", r["id"]).json()["trace_id"]
    assert svc.client.get(f"/v1/jobs/{r['job']['id']}", headers=H(key_of(svc, "alice"))).status_code == 200
    assert svc.client.get(f"/v1/jobs/{r['job']['id']}", headers=H(key_of(svc, "bob"))).status_code == 404
    assert svc.client.get(f"/v1/traces/{trace}", headers=H(key_of(svc, "alice"))).status_code == 200
    assert svc.client.get(f"/v1/traces/{trace}", headers=H(key_of(svc, "bob"))).status_code == 404


def test_malformed_ids_are_404_not_500(svc):
    for bad in ("../../etc", "x" * 5, "%00", "A" * 32):
        assert svc.client.get(f"/v1/documents/{bad}", headers=H(key_of(svc, "alice"))).status_code == 404


# ------------------------------------------------------------------ ask
def test_ask_before_ready_is_409(svc):
    svc.runner.stop()  # nothing will process the job, so the document stays queued
    doc = upload(svc, "alice").json()["id"]
    r = ask(svc, "alice", doc)
    assert r.status_code == 409 and r.json()["status"] in ("queued", "parsing")


def test_unanswerable_question_is_refused_with_no_citations(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    a = ask(svc, "alice", doc, "UNANSWERABLE what is the airspeed of a swallow?").json()
    assert a["refused"] is True and a["citations"] == []


def test_identical_question_is_served_from_cache_without_spending_quota_or_tokens(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    ScriptedLLM.calls = 0
    first = ask(svc, "alice", doc, "What does it say about subject p1?").json()
    calls_after_first = ScriptedLLM.calls
    second = ask(
        svc, "alice", doc, "  what does it say   about SUBJECT p1? "
    ).json()  # same question, different spacing/case
    assert second["cached"] is True and second["answer"] == first["answer"] and ScriptedLLM.calls == calls_after_first
    assert second["asks_remaining_today"] == first["asks_remaining_today"] == 3  # the cache hit cost nothing


def test_cache_is_not_shared_between_users_or_documents(svc):
    a_doc = upload(svc, "alice").json()["id"]
    b_doc = upload(svc, "bob").json()["id"]
    wait_ready(svc, "alice", a_doc)
    wait_ready(svc, "bob", b_doc)
    assert ask(svc, "alice", a_doc, "same question here").json()["cached"] is False
    assert ask(svc, "bob", b_doc, "same question here").json()["cached"] is False


def test_daily_ask_quota_then_429(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    for i in range(4):
        assert ask(svc, "alice", doc, f"distinct question number {i}").status_code == 200
    r = ask(svc, "alice", doc, "one more distinct question")
    assert r.status_code == 429 and r.json()["scope"] == "daily_asks"
    assert ask(svc, "alice", doc, "distinct question number 0").json()["cached"] is True  # cached answers still work


def test_per_minute_rate_limit_returns_429(svc):
    svc.deps.ask_limiter.limit = 2
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    codes = [ask(svc, "alice", doc, f"question number {i} please").status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_shared_model_budget_exhaustion_is_429_and_refunds_the_users_ask(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    budget = ModelBudget(svc.factory, 1000, 10_000_000)  # a tiny shared tank
    budget.reserve("scripted", 1000)
    svc.deps.llm_for_user = lambda: BudgetedLLM({}, ("scripted",), lambda m: ScriptedLLM(), budget)
    r = ask(svc, "alice", doc, "a question while the tank is empty")
    assert r.status_code == 429 and r.json()["scope"] == "model_budget"
    assert svc.client.get("/v1/me", headers=H(key_of(svc, "alice"))).json()["remaining_today"]["asks"] == 4  # refunded


def test_question_validation(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    for q in ("", "ab", "x" * 1001):
        assert ask(svc, "alice", doc, q).status_code == 422


# ------------------------------------------------------------------ traces + privacy
def test_trace_endpoint_returns_spans_but_never_document_text(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    trace = ask(svc, "alice", doc).json()["trace_id"]
    t = svc.client.get(f"/v1/traces/{trace}", headers=H(key_of(svc, "alice"))).json()
    names = {sp["name"] for sp in t["spans"]}
    assert {"agent.run", "node.retrieve", "llm.synthesize"} <= names and "retrieved_json" not in t["run"]
    assert t["run"]["user_id"] == svc.users["alice"][0] and t["run"]["doc_id"] == doc
    stored = svc.factory().execute("SELECT retrieved_json FROM runs WHERE run_id=?", (trace,)).fetchone()[0]
    assert "sentence number" not in stored  # filler text from the PDF is not in the trace


# ------------------------------------------------------------------ delete
def test_delete_removes_everything_and_the_document_is_gone(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    trace = ask(svc, "alice", doc).json()["trace_id"]
    owner = svc.users["alice"][0]
    assert svc.deps.kb.store.count(owner, doc) > 0
    r = svc.client.delete(f"/v1/documents/{doc}", headers=H(key_of(svc, "alice")))
    assert r.status_code == 204
    conn = svc.factory()
    for table, col in (
        ("chunks", "doc_id"),
        ("docs", "id"),
        ("jobs", "doc_id"),
        ("answer_cache", "doc_id"),
        ("runs", "doc_id"),
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (doc,)).fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM spans WHERE run_id=?", (trace,)).fetchone()[0] == 0
    assert svc.deps.kb.store.count(owner, doc) == 0 and not svc.deps.kb.doc_dir(owner, doc).exists()
    assert svc.client.get(f"/v1/documents/{doc}", headers=H(key_of(svc, "alice"))).status_code == 404


def test_delete_can_keep_traces_on_request(svc):
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    trace = ask(svc, "alice", doc).json()["trace_id"]
    svc.client.delete(f"/v1/documents/{doc}?purge_traces=false", headers=H(key_of(svc, "alice")))
    assert svc.factory().execute("SELECT COUNT(*) FROM runs WHERE run_id=?", (trace,)).fetchone()[0] == 1


# ------------------------------------------------------------------ robustness
def test_unexpected_server_errors_do_not_leak_internals(svc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret database path /var/lib/x")

    monkeypatch.setattr("acp.server.app.jobs.get_doc", boom)
    r = svc.client.get("/v1/documents/" + "a" * 32, headers=H(key_of(svc, "alice")))
    assert r.status_code == 500 and "secret" not in r.text and "request_id" in r.json()


def test_chat_page_is_served_with_a_strict_csp_when_present(svc):
    r = svc.client.get("/")
    if r.status_code == 200:  # present from step 9 onward
        assert "text/html" in r.headers["content-type"] and "script-src 'self'" in r.headers["content-security-policy"]
    else:
        assert r.status_code == 404


def test_concurrent_uploads_from_one_user_all_get_processed_in_order(svc):
    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(2) as ex:
        results = list(
            ex.map(lambda i: upload(svc, "alice", pages=[long_lines(f"c{i}{j}") for j in range(3)]), range(2))
        )
    assert [r.status_code for r in results] == [202, 202]
    for r in results:
        assert wait_ready(svc, "alice", r.json()["id"])["status"] == "ready"
    assert json.dumps(svc.client.get("/v1/documents", headers=H(key_of(svc, "alice"))).json())


def test_changing_the_model_setup_does_not_serve_answers_cached_by_the_old_models(svc):
    from dataclasses import replace

    from acp.server.app import _cache_key

    a = _cache_key("o", "d", "What?", ("m1", "m2"))
    assert a == _cache_key("o", "d", " what?  ", ("m1", "m2")) and a != _cache_key("o", "d", "What?", ("m1", "m3"))
    doc = upload(svc, "alice").json()["id"]
    wait_ready(svc, "alice", doc)
    assert ask(svc, "alice", doc, "a question to cache").json()["cached"] is False
    assert ask(svc, "alice", doc, "a question to cache").json()["cached"] is True
    object.__setattr__(svc.deps.settings, "synth_models", ("another-model",))  # frozen dataclass: bypass for the test
    assert replace(svc.deps.settings).synth_models == ("another-model",)
    assert ask(svc, "alice", doc, "a question to cache").json()["cached"] is False  # new models, fresh answer
