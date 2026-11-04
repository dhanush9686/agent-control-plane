import pytest
from langchain_core.documents import Document

from acp import store
from acp.embeddings import HashEmbedder
from acp.errors import ACPError
from acp.hybrid import DocRetriever, fts_query, rrf
from acp.ingest.chunking import Chunk
from acp.kb import KnowledgeBase, citation_label, safe_id
from tests.fakes import FailingVectorStore, InMemoryVectorStore

OWNER_A, OWNER_B = "a" * 16, "b" * 16
DOC_A, DOC_B = "d1" * 8, "d2" * 8


class ZeroEmbedder:
    """Every text gets a nearly identical vector, so dense search is useless: proves keyword search rescues it."""

    dim = 8

    def embed(self, texts):
        return [[1.0] + [0.0] * (self.dim - 1) for _ in texts]


def _chunks(n=20, needle_page=7):
    out = []
    for i in range(n):
        page = i // 2 + 1
        text = f"General discussion of topic{i} and filler words about nothing special on page {page}."
        if page == needle_page and i % 2 == 0:
            text += " The indemnification cap is zyxwv42 dollars under clause 14.2."
        out.append(Chunk(i, page, None, f"Section {page // 4}", text))
    return out


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "t.db"
    kb = KnowledgeBase(tmp_path / "kb", HashEmbedder(256), InMemoryVectorStore())
    conn = store.connect(db)
    kb.add_document(conn, OWNER_A, DOC_A, _chunks())
    conn.close()
    return kb, db


def _retriever(kb, db, mode="hybrid", owner=OWNER_A, doc=DOC_A, filename="contract.pdf", **kw):
    return DocRetriever(kb, lambda: store.connect(db), owner, doc, filename, mode=mode, **kw)


def test_rrf_rewards_agreement_between_lists():
    scores = rrf([[1, 2, 3], [3, 2, 1]])
    assert scores[2] == pytest.approx(2 / 62)
    assert scores[1] == pytest.approx(1 / 61 + 1 / 63) == pytest.approx(scores[3])
    assert rrf([[5], [5], [6]])[5] > rrf([[5], [5], [6]])[6]


def test_fts_query_is_safe_and_drops_stopwords():
    assert fts_query("What is the indemnification cap?") == '"indemnification" OR "cap"'
    assert fts_query("the of and") is None and fts_query("") is None and fts_query("?! ... --") is None


@pytest.mark.parametrize(
    "hostile",
    [
        '" OR 1=1 -- ) NEAR(',
        "a* AND NOT b",
        "col:text ^start",
        '"unbalanced',
        "NEAR(a b, 2)",
        "{x} [y] (z)",
        "foo; DROP TABLE chunks",
    ],
)
def test_hostile_questions_never_break_the_keyword_search(tmp_path, hostile):
    conn = store.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO chunks_fts (rowid, text, doc_id, owner_id) VALUES (1, 'near and drop tables', 'd', 'o')")
    q = fts_query(hostile)
    if q is not None:
        conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (q,)).fetchall()  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 1


def test_search_returns_page_numbered_documents(env):
    kb, db = env
    docs = _retriever(kb, db).search("indemnification cap zyxwv42", 3)
    top = docs[0]
    assert "zyxwv42" in top.page_content
    assert top.metadata["page"] == 7 and top.metadata["citation"] == "contract.pdf p.7"
    assert top.metadata["section"] == "Section 1" and top.metadata["doc_id"] == DOC_A


def test_keyword_search_rescues_a_useless_embedder(tmp_path):
    db = tmp_path / "t.db"
    kb = KnowledgeBase(tmp_path / "kb", ZeroEmbedder(), InMemoryVectorStore())
    conn = store.connect(db)
    kb.add_document(conn, OWNER_A, DOC_A, _chunks())
    conn.close()
    hybrid = _retriever(kb, db, "hybrid").search("zyxwv42 indemnification", 3)
    dense = _retriever(kb, db, "dense").search("zyxwv42 indemnification", 3)
    assert any("zyxwv42" in d.page_content for d in hybrid)
    assert not any("zyxwv42" in d.page_content for d in dense[:1]) or dense[0].metadata["idx"] == 0  # arbitrary order


def test_modes_are_distinct_and_validated(env):
    kb, db = env
    for mode in ("dense", "bm25", "hybrid"):
        assert _retriever(kb, db, mode).search("indemnification", 2)
    with pytest.raises(ValueError):
        _retriever(kb, db, "magic")


def test_other_users_cannot_search_my_document(env):
    kb, db = env
    intruder = _retriever(kb, db, owner=OWNER_B, doc=DOC_A)  # B guesses A's doc id
    assert intruder.search("indemnification cap zyxwv42", 5) == []  # B's namespace is empty; A's text is unreachable
    conn = store.connect(db)  # and the keyword (SQL) path filters by owner too
    assert intruder._bm25(conn, "indemnification cap", 10) == []
    mine = _retriever(kb, db).search("indemnification cap zyxwv42", 3)
    assert mine and all(d.metadata["doc_id"] == DOC_A for d in mine)  # sanity: the rightful owner still finds it


def test_two_documents_never_mix(env, tmp_path):
    kb, db = env
    conn = store.connect(db)
    other = [Chunk(0, 1, None, None, "Completely different file about gardening and the zyxwv42 tomato.")]
    kb.add_document(conn, OWNER_A, DOC_B, other)
    conn.close()
    docs = _retriever(kb, db, doc=DOC_B, filename="garden.pdf").search("zyxwv42", 5)
    assert len(docs) == 1 and "gardening" in docs[0].page_content and docs[0].metadata["doc_id"] == DOC_B


def test_delete_removes_files_rows_and_keyword_hits(env):
    kb, db = env
    d = kb.doc_dir(OWNER_A, DOC_A)
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.pdf").write_bytes(b"%PDF-")
    assert kb.store.count(OWNER_A, DOC_A) == 20
    conn = store.connect(db)
    kb.delete_document(conn, OWNER_A, DOC_A)
    assert not d.exists() and kb.store.count(OWNER_A, DOC_A) == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'indemnification'").fetchone()[0] == 0


def test_progress_covers_embedding_then_upload_and_ends_at_total(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb", HashEmbedder(64), InMemoryVectorStore())
    seen = []
    kb.add_document(
        store.connect(tmp_path / "t.db"), OWNER_A, DOC_A, _chunks(150, needle_page=1), lambda d, t: seen.append((d, t))
    )
    done = [d for d, _ in seen]
    assert done == sorted(done)  # never goes backwards
    assert all(t == 300 for _, t in seen) and seen[-1] == (300, 300)  # 150 chunks: embed half, upload half
    assert any(d <= 150 for d in done) and any(d > 150 for d in done)  # both phases reported


def test_vectors_are_normalized_and_one_per_chunk_in_the_store(env):
    kb, db = env
    stored = kb.store.data[OWNER_A]
    assert len(stored) == 20 and all(k.startswith(f"{DOC_A}#") for k in stored)
    assert all(abs(sum(x * x for x in v) - 1.0) < 1e-6 for v in stored.values())


def test_text_never_goes_to_the_vector_store(env):
    kb, _ = env
    for vec in kb.store.data[OWNER_A].values():
        assert all(isinstance(x, float) for x in vec)  # numbers only; chunk text stays in our database


def test_failed_upload_leaves_no_rows_and_no_vectors(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb", HashEmbedder(64), FailingVectorStore())
    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(RuntimeError):
        kb.add_document(conn, OWNER_A, DOC_A, _chunks(10, needle_page=1))
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    assert kb.store.count(OWNER_A, DOC_A) == 0  # the one vector that was written got removed


def test_ingest_waits_for_the_vector_service_to_make_the_document_searchable(env):
    kb, _ = env
    assert kb.store.visible_calls == 1


def test_ids_cannot_escape_the_storage_directory(tmp_path):
    kb = KnowledgeBase(tmp_path, HashEmbedder(8), InMemoryVectorStore())
    for bad in ("../etc", "a/b", "", "XYZ!", "short"):
        with pytest.raises(ACPError):
            safe_id(bad)
    with pytest.raises(ACPError):
        kb.doc_dir("../../x", DOC_A)


def test_citation_label_strips_characters_that_could_forge_a_citation():
    assert citation_label("Q3 report.pdf", 12) == "Q3 report.pdf p.12"
    assert citation_label("evil[Source: x].pdf", 1) == "evilSource: x.pdf p.1"
    assert citation_label("/tmp/../etc/passwd", 3) == "passwd p.3"
    assert citation_label("", 1) == "document.pdf p.1"


class _ReverseReranker:
    def rerank(self, query, docs):
        return [(d, float(i)) for i, d in enumerate(reversed(docs))]


def test_reranker_gets_the_shortlist_and_decides_the_final_order(env):
    kb, db = env
    plain = _retriever(kb, db).search("indemnification cap zyxwv42", 3)
    reranked = _retriever(kb, db, reranker=_ReverseReranker()).search("indemnification cap zyxwv42", 3)
    assert isinstance(reranked[0], Document) and len(reranked) == 3
    assert reranked[0].metadata["idx"] != plain[0].metadata["idx"]


def test_dense_search_ranks_by_cosine_similarity(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb", HashEmbedder(256), InMemoryVectorStore())
    conn = store.connect(tmp_path / "t.db")
    kb.add_document(conn, OWNER_A, DOC_A, _chunks(40, needle_page=99))
    got = kb.dense_search(OWNER_A, DOC_A, "topic17 filler words", 5)
    assert got[0] == 17 and len(got) == 5 and len(set(got)) == 5


def test_knowledge_base_never_imports_faiss_torch_or_numpy():
    """Regression: FAISS + PyTorch in one process aborts on macOS; and the project uses Pinecone, not local math."""
    import subprocess
    import sys

    code = "import sys, acp.kb, acp.hybrid; print([m for m in ('faiss','torch','numpy') if m in sys.modules])"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.stdout.strip() == "[]", r.stdout + r.stderr


def test_dense_search_on_an_empty_document_returns_nothing(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb", HashEmbedder(16), InMemoryVectorStore())
    kb.add_document(store.connect(tmp_path / "t.db"), OWNER_A, DOC_A, [])
    assert kb.dense_search(OWNER_A, DOC_A, "anything", 5) == []
