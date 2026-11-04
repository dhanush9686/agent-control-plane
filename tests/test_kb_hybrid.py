import faiss
import numpy as np
import pytest
from langchain_core.documents import Document

from acp import store
from acp.embeddings import HashEmbedder
from acp.errors import ACPError
from acp.hybrid import DocRetriever, fts_query, rrf
from acp.ingest.chunking import Chunk
from acp.kb import KnowledgeBase, citation_label, safe_id

OWNER_A, OWNER_B = "a" * 16, "b" * 16
DOC_A, DOC_B = "d1" * 8, "d2" * 8


class ZeroEmbedder:
    """Every text gets a nearly identical vector, so dense search is useless: proves keyword search rescues it."""

    dim = 8

    def embed(self, texts):
        v = np.zeros((len(texts), self.dim), dtype="float32")
        v[:, 0] = 1.0
        return v


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
    kb = KnowledgeBase(tmp_path / "kb", HashEmbedder(256))
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
    kb = KnowledgeBase(tmp_path / "kb", ZeroEmbedder())
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


def test_other_users_cannot_search_my_document(env, tmp_path):
    kb, db = env
    intruder = _retriever(kb, db, owner=OWNER_B, doc=DOC_A)  # B guesses A's doc id
    with pytest.raises(ACPError):  # no index in B's directory
        intruder.search("indemnification", 3)
    conn = store.connect(db)  # and even straight SQL through the retriever's filter returns nothing for B
    assert intruder._bm25(conn, "indemnification cap", 10) == []


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
    assert (d / "index.faiss").is_file()
    conn = store.connect(db)
    kb.delete_document(conn, OWNER_A, DOC_A)
    assert not d.exists()
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'indemnification'").fetchone()[0] == 0


def test_progress_is_monotonic_and_reaches_total(tmp_path):
    kb = KnowledgeBase(tmp_path / "kb", HashEmbedder(64))
    seen = []
    kb.add_document(
        store.connect(tmp_path / "t.db"), OWNER_A, DOC_A, _chunks(150, needle_page=1), lambda d, t: seen.append((d, t))
    )
    assert [d for d, _ in seen] == sorted(d for d, _ in seen) and seen[-1] == (150, 150) and len(seen) == 3


def test_index_is_a_plain_faiss_file_that_survives_restart(env, tmp_path):
    kb, db = env
    path = kb.index_path(OWNER_A, DOC_A)
    assert faiss.read_index(str(path)).ntotal == 20  # readable with no pickle involved
    assert not list(path.parent.glob("*.tmp"))
    fresh = KnowledgeBase(kb.root, HashEmbedder(256))  # a new process loading the same files
    assert DocRetriever(fresh, lambda: store.connect(db), OWNER_A, DOC_A, "c.pdf").search("indemnification", 2)


def test_ids_cannot_escape_the_storage_directory(tmp_path):
    kb = KnowledgeBase(tmp_path, HashEmbedder(8))
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
