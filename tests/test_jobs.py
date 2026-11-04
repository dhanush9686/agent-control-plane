import hashlib

import pytest

from acp import store
from acp.config import Settings
from acp.embeddings import HashEmbedder
from acp.errors import ACPError
from acp.hybrid import DocRetriever
from acp.ingest import jobs
from acp.ingest.parse_worker import parse_pdf
from acp.ingest.pdf import PdfError
from acp.ingest.pipeline import GENERIC_FAILURE, IngestDeps, recover_interrupted, run_job
from acp.ingest.worker import JobRunner
from acp.kb import KnowledgeBase
from tests.fakes import FailingVectorStore, InMemoryVectorStore
from tests.pdfgen import long_lines, make_pdf

OWNER = "a" * 16


def _setup(tmp_path, vstore=None, **settings_kw):
    s = Settings(db_path=tmp_path / "t.db", kb_root=tmp_path / "kb", max_pages=50, parse_timeout_s=120, **settings_kw)
    kb = KnowledgeBase(s.kb_root, HashEmbedder(128), vstore or InMemoryVectorStore())
    return s, kb, IngestDeps(s, kb)


def _upload(s, kb, pages, name="report.pdf", owner=OWNER):
    """What the API does: store the bytes, register doc + job."""
    data = make_pdf(pages) if not isinstance(pages, bytes) else pages
    conn = store.connect(s.db_path)
    doc_id, job_id, is_new = jobs.register_document(conn, owner, hashlib.sha256(data).hexdigest(), name)
    if is_new:
        kb.doc_dir(owner, doc_id).mkdir(parents=True, exist_ok=True)
        kb.pdf_path(owner, doc_id).write_bytes(data)
    return conn, doc_id, job_id, is_new


def _pages(n=6):
    return [long_lines(f"p{i}", 30) for i in range(n)]


def test_successful_job_reaches_ready_and_the_document_is_searchable(tmp_path):
    s, kb, deps = _setup(tmp_path)
    conn, doc_id, job_id, _ = _upload(s, kb, _pages())
    run_job(job_id, deps)
    doc, job = jobs.get_doc(conn, doc_id), jobs.get_job(conn, job_id)
    assert doc["status"] == "ready" and doc["pages"] == 6 and doc["chunks"] > 6
    assert job["state"] == "ready" and job["percent"] == 100
    hits = DocRetriever(kb, lambda: store.connect(s.db_path), OWNER, doc_id, "report.pdf").search("p3 subject p312", 3)
    assert hits and hits[0].metadata["page"] == 4 and hits[0].metadata["citation"] == "report.pdf p.4"
    assert not (kb.doc_dir(OWNER, doc_id) / "work").exists()  # temp parse output cleaned up


def test_progress_updates_are_monotonic_across_the_whole_job(tmp_path, monkeypatch):
    s, kb, deps = _setup(tmp_path)
    conn, doc_id, job_id, _ = _upload(s, kb, _pages(10))
    seen = []
    real = jobs.update_job

    def spy(c, jid, state, progress=0, total=0, message=None):
        real(c, jid, state, progress, total, message)
        seen.append(jobs.get_job(c, jid)["percent"])

    monkeypatch.setattr("acp.ingest.pipeline.jobs.update_job", spy)
    run_job(job_id, deps)
    assert seen == sorted(seen) and seen[0] <= 20 and max(seen) <= 99
    assert jobs.get_job(conn, job_id)["percent"] == 100


def test_percent_math():
    assert jobs.overall_percent("queued", 0, 0) == 0
    assert jobs.overall_percent("parsing", 1, 2) == 10
    assert jobs.overall_percent("embedding", 0, 10) == 25 and jobs.overall_percent("embedding", 10, 10) == 99
    assert jobs.overall_percent("ready", 5, 5) == 100 and jobs.overall_percent("failed", 5, 5) == 0
    assert jobs.overall_percent("embedding", 99, 10) == 99  # over-reporting is clamped


@pytest.mark.parametrize(
    "bad,code_text",
    [
        (b"%PDF-1.4\nnot a real pdf body at all", "damaged"),
        (make_pdf([["x"], ["y"], ["z"]]), "scanned"),
    ],
)
def test_bad_files_fail_with_a_friendly_message_and_leave_nothing_behind(tmp_path, bad, code_text):
    s, kb, deps = _setup(tmp_path)
    conn, doc_id, job_id, _ = _upload(s, kb, bad)
    run_job(job_id, deps)
    doc, job = jobs.get_doc(conn, doc_id), jobs.get_job(conn, job_id)
    assert doc["status"] == "failed" and job["state"] == "failed" and code_text in doc["error"].lower()
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert not kb.doc_dir(OWNER, doc_id).exists()  # the stored PDF is removed too


def test_too_many_pages_is_refused_with_the_limit_in_the_message(tmp_path):
    s, kb, deps = _setup(tmp_path)
    conn, doc_id, job_id, _ = _upload(s, kb, _pages(60))
    run_job(job_id, deps)
    assert "60 pages" in jobs.get_doc(conn, doc_id)["error"] and "limit is 50" in jobs.get_doc(conn, doc_id)["error"]


def test_vector_service_failure_cleans_up_and_hides_internals(tmp_path):
    s, kb, deps = _setup(tmp_path, vstore=FailingVectorStore())
    conn, doc_id, job_id, _ = _upload(s, kb, _pages())
    run_job(job_id, deps)
    doc = jobs.get_doc(conn, doc_id)
    assert doc["status"] == "failed" and doc["error"] == GENERIC_FAILURE and "pinecone" not in doc["error"].lower()
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert kb.store.count(OWNER, doc_id) == 0


def test_friendly_errors_from_dependencies_are_shown_to_the_user(tmp_path):
    s, kb, _ = _setup(tmp_path)

    def parse_refuses(*a):
        raise PdfError("encrypted", "This PDF is password-protected. Remove the password and upload it again.")

    conn, doc_id, job_id, _ = _upload(s, kb, _pages())
    run_job(job_id, IngestDeps(s, kb, parse=parse_refuses))
    assert "password" in jobs.get_doc(conn, doc_id)["error"]


def test_a_crashing_parser_is_contained(tmp_path):
    s, kb, _ = _setup(tmp_path)

    def parse_crashes(*a):
        raise MemoryError("boom")

    conn, doc_id, job_id, _ = _upload(s, kb, _pages())
    run_job(job_id, IngestDeps(s, kb, parse=parse_crashes))  # must not raise
    assert jobs.get_doc(conn, doc_id)["error"] == GENERIC_FAILURE


def test_same_bytes_from_same_owner_reuse_the_document_but_other_owners_get_their_own(tmp_path):
    s, kb, deps = _setup(tmp_path)
    conn, doc1, job1, new1 = _upload(s, kb, _pages())
    run_job(job1, deps)
    _, doc2, job2, new2 = _upload(s, kb, _pages())  # same bytes, same owner
    assert (doc2, new2) == (doc1, False) and job2 == job1
    _, doc3, _, new3 = _upload(s, kb, _pages(), owner="b" * 16)
    assert doc3 != doc1 and new3 is True


def test_uploading_again_after_a_failure_retries_with_a_fresh_job(tmp_path):
    s, kb, deps = _setup(tmp_path)
    bad = b"%PDF-1.4\ngarbage"
    conn, doc_id, job1, _ = _upload(s, kb, bad)
    run_job(job1, deps)
    assert jobs.get_doc(conn, doc_id)["status"] == "failed"
    conn2, doc_again, job2, is_new = _upload(s, kb, bad)
    assert doc_again == doc_id and is_new and job2 != job1
    assert jobs.get_doc(conn2, doc_id)["status"] == "queued" and jobs.get_doc(conn2, doc_id)["error"] is None


def test_retrying_a_job_never_double_inserts_chunks(tmp_path):
    s, kb, deps = _setup(tmp_path)
    conn, doc_id, job_id, _ = _upload(s, kb, _pages())
    run_job(job_id, deps)
    n = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0]
    run_job(job_id, deps)  # e.g. recovered after a crash that happened right at the end
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0] == n
    assert kb.store.count(OWNER, doc_id) == n


def test_documents_are_owner_scoped_on_lookup(tmp_path):
    s, kb, deps = _setup(tmp_path)
    conn, doc_id, _, _ = _upload(s, kb, _pages())
    assert jobs.get_doc(conn, doc_id, OWNER) is not None
    assert jobs.get_doc(conn, doc_id, "b" * 16) is None  # looks exactly like a missing document
    assert jobs.get_doc(conn, "f" * 32) is None


def test_restart_recovery_requeues_when_the_pdf_exists_and_fails_when_it_does_not(tmp_path):
    s, kb, deps = _setup(tmp_path)
    conn, doc_a, job_a, _ = _upload(s, kb, _pages(4))
    jobs.update_job(conn, job_a, "embedding", 3, 10)  # "the server died here"
    conn2, doc_b, job_b, _ = _upload(s, kb, _pages(5))
    jobs.update_job(conn2, job_b, "parsing", 0, 1)
    kb.pdf_path(OWNER, doc_b).unlink()  # this one lost its file
    conn3, doc_c, job_c, _ = _upload(s, kb, _pages(3))
    run_job(job_c, deps)  # finished earlier: must be left alone
    again = recover_interrupted(deps)
    assert again == [job_a]
    assert jobs.get_doc(conn, doc_b)["status"] == "failed" and "restart" in jobs.get_doc(conn, doc_b)["error"]
    assert jobs.get_doc(conn, doc_c)["status"] == "ready"


def test_worker_processes_jobs_in_order_one_at_a_time_and_survives_failures(tmp_path):
    s, kb, deps = _setup(tmp_path)
    runner = JobRunner(deps)
    runner.start()
    uploads = [_upload(s, kb, p) for p in (_pages(3), b"%PDF-1.4\nbroken", _pages(4))]
    for _, _, job_id, _ in uploads:
        runner.submit(job_id)
    runner.drain()
    runner.stop()
    states = [jobs.get_job(uploads[0][0], u[2])["state"] for u in uploads]
    assert states == ["ready", "failed", "ready"]  # the bad file did not stop the ones behind it


def test_worker_recovers_interrupted_jobs_on_start(tmp_path):
    s, kb, deps = _setup(tmp_path)
    conn, doc_id, job_id, _ = _upload(s, kb, _pages(4))
    jobs.update_job(conn, job_id, "embedding", 1, 8)
    runner = JobRunner(deps)
    runner.start()
    runner.drain()
    runner.stop()
    assert jobs.get_doc(conn, doc_id)["status"] == "ready"


def test_real_subprocess_parse_is_the_default_parser():
    assert IngestDeps.__dataclass_fields__["parse"].default is parse_pdf
    assert issubclass(PdfError, ACPError)
