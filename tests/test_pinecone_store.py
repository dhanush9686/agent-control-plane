"""PineconeStore against a mock index (offline), then against the REAL service (live, opt-in)."""

import os
import types

import pytest

from acp.vectorstore import UPSERT_BATCH, PineconeStore, VectorStoreError, _retry, vector_id


class MockIndex:
    """Mimics the parts of a Pinecone index we use, including eventual consistency (writes appear after N reads)."""

    def __init__(self, delay_reads: int = 0, fail_first: int = 0, fail_status: int = 503):
        self.vectors: dict[str, dict[str, dict]] = {}
        self.delay, self.reads = delay_reads, 0
        self.fail_first, self.fail_status = fail_first, fail_status
        self.upsert_batches: list[int] = []
        self.calls: list[str] = []

    def _maybe_fail(self):
        if self.fail_first > 0:
            self.fail_first -= 1
            err = RuntimeError("boom")
            err.status = self.fail_status
            raise err

    def upsert(self, *, vectors, namespace):
        self._maybe_fail()
        self.upsert_batches.append(len(vectors))
        for v in vectors:
            self.vectors.setdefault(namespace, {})[v["id"]] = v

    def fetch(self, *, ids, namespace):
        self.reads += 1
        found = {}
        if self.reads > self.delay:
            found = {i: self.vectors.get(namespace, {}).get(i) for i in ids if i in self.vectors.get(namespace, {})}
        return types.SimpleNamespace(vectors=found)

    def query(self, *, vector, top_k, namespace, filter):
        want = filter["doc_id"]["$eq"]
        self.calls.append(f"query:{namespace}:{want}")
        rows = [
            (sum(a * b for a, b in zip(v["values"], vector, strict=True)), v["id"])
            for v in self.vectors.get(namespace, {}).values()
            if v["metadata"]["doc_id"] == want
        ]
        rows.sort(reverse=True)
        return types.SimpleNamespace(matches=[types.SimpleNamespace(id=i, score=s) for s, i in rows[:top_k]])

    def list(self, *, prefix, namespace):
        ids = sorted(i for i in self.vectors.get(namespace, {}) if i.startswith(prefix))
        for k in range(0, len(ids), 3):  # pages of 3, shaped exactly like the real ListResponse
            yield types.SimpleNamespace(vectors=[types.SimpleNamespace(id=i) for i in ids[k : k + 3]])

    def delete(self, *, ids, namespace):
        if not ids:  # the real service rejects an empty list (PineconeValueError: ids must be a non-empty list)
            raise ValueError("ids must be a non-empty list")
        for i in ids:
            self.vectors.get(namespace, {}).pop(i, None)


def _vecs(n, dim=4):
    return [[1.0 if j == i % dim else 0.0 for j in range(dim)] for i in range(n)]


def test_upsert_batches_ids_and_metadata():
    idx = MockIndex()
    seen = []
    PineconeStore(idx).upsert("ownerx", "doc1", _vecs(250), lambda d, t: seen.append((d, t)))
    assert idx.upsert_batches == [UPSERT_BATCH, UPSERT_BATCH, 50]
    stored = idx.vectors["ownerx"]
    assert len(stored) == 250 and stored[vector_id("doc1", 249)]["metadata"] == {"doc_id": "doc1"}
    assert seen[-1] == (250, 250) and [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_query_maps_ids_back_to_positions_in_rank_order():
    idx = MockIndex()
    store = PineconeStore(idx)
    store.upsert("ownerx", "doc1", _vecs(8))
    assert store.query("ownerx", "doc1", [0.0, 1.0, 0.0, 0.0], 3) == [1, 5, 0] or store.query(
        "ownerx", "doc1", [0.0, 1.0, 0.0, 0.0], 2
    )[0] in (1, 5)
    assert idx.calls[-1] == "query:ownerx:doc1"


def test_query_never_returns_another_documents_or_users_vectors():
    idx = MockIndex()
    store = PineconeStore(idx)
    store.upsert("alice", "docA", _vecs(4))
    store.upsert("alice", "docB", _vecs(4))
    store.upsert("bob", "docA", _vecs(4))
    assert len(store.query("alice", "docA", [1.0, 0, 0, 0], 50)) == 4  # only alice's docA
    assert store.query("carol", "docA", [1.0, 0, 0, 0], 50) == []  # an empty namespace


def test_wait_until_visible_polls_through_eventual_consistency():
    idx = MockIndex(delay_reads=3)
    sleeps = []
    store = PineconeStore(idx, sleep=sleeps.append)
    store.upsert("o", "d", _vecs(5))
    store.wait_until_visible("o", "d", 5)
    assert idx.reads == 4 and len(sleeps) == 3  # three "not yet" reads, then visible


def test_wait_until_visible_gives_up_with_a_friendly_error(monkeypatch):
    import acp.vectorstore as vs

    idx = MockIndex(delay_reads=10**6)
    monkeypatch.setattr(vs, "VISIBLE_TIMEOUT_S", -1.0)
    with pytest.raises(VectorStoreError, match="slow to index"):
        PineconeStore(idx, sleep=lambda s: None).wait_until_visible("o", "d", 5)


def test_delete_removes_only_that_documents_vectors_across_pages():
    idx = MockIndex()
    store = PineconeStore(idx)
    store.upsert("o", "docA", _vecs(10))
    store.upsert("o", "docB", _vecs(4))
    store.delete_doc("o", "docA")
    assert sorted(idx.vectors["o"]) == [vector_id("docB", i) for i in range(4)]


def test_transient_errors_are_retried_then_succeed(monkeypatch):
    monkeypatch.setattr("acp.vectorstore.time.sleep", lambda s: None)
    idx = MockIndex(fail_first=2, fail_status=429)
    PineconeStore(idx).upsert("o", "d", _vecs(3))
    assert len(idx.vectors["o"]) == 3


def test_permanent_errors_surface_as_a_friendly_error_without_retry_storm(monkeypatch):
    monkeypatch.setattr("acp.vectorstore.time.sleep", lambda s: None)
    calls = []

    def boom():
        calls.append(1)
        e = RuntimeError("bad request")
        e.status = 400
        raise e

    with pytest.raises(VectorStoreError, match="searching"):
        _retry(boom, "searching")
    assert len(calls) == 1


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    with pytest.raises(VectorStoreError, match="PINECONE_API_KEY"):
        PineconeStore.connect(dimension=384)


# ----------------------------------------------------------------- LIVE: the real service
live = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="live test: run `RUN_LIVE=1 pytest -m live`")


@pytest.mark.live
@live
def test_real_pinecone_roundtrip_isolation_and_cleanup():
    from dotenv import load_dotenv

    from acp.config import ROOT

    load_dotenv(ROOT / ".env")
    store = PineconeStore.connect(dimension=4, index_name=os.environ.get("PINECONE_TEST_INDEX", "acp-test-dim4"))
    ns, other_ns, doc = "f" * 16, "e" * 16, "c" * 16
    vecs = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0.6, 0.8, 0, 0]]
    try:
        store.upsert(ns, doc, vecs)
        store.wait_until_visible(ns, doc, len(vecs))
        got = store.query(ns, doc, [0.0, 1.0, 0, 0], 4)
        assert got[0] == 1 and set(got) == {0, 1, 2, 3}
        assert store.query(other_ns, doc, [0.0, 1.0, 0, 0], 4) == []  # another user's namespace sees nothing
        assert store.query(ns, "a" * 16, [0.0, 1.0, 0, 0], 4) == []  # another document sees nothing
    finally:
        store.delete_doc(ns, doc)
    import time

    for _ in range(20):  # deletes are also eventually consistent
        if store.query(ns, doc, [0.0, 1.0, 0, 0], 4) == []:
            break
        time.sleep(1)
    assert store.query(ns, doc, [0.0, 1.0, 0, 0], 4) == []


def test_deleting_a_document_with_no_vectors_is_a_no_op_not_an_error():
    """Real-service finding: delete(ids=[]) is rejected, so deleting an empty/unknown document must not call it."""
    idx = MockIndex()
    PineconeStore(idx).delete_doc("nobody", "nothing")  # must not raise
    assert idx.vectors == {}
