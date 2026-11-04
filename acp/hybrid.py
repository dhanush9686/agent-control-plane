"""Hybrid search: meaning-based (vectors) + word-based (BM25), merged, optionally re-scored.

Analogy: two librarians. One finds books "about the same thing" even in different words (dense vectors); the other
finds books containing the exact names, numbers and rare terms you typed (BM25 keywords). We take both shortlists
and merge them by rank (reciprocal-rank fusion), so a chunk that BOTH like floats to the top. Dense search alone
misses exact terms ("clause 14.2(b)"); keyword search alone misses paraphrases. Together they cover 500 pages.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable

from langchain_core.documents import Document

from acp.kb import KnowledgeBase, citation_label, safe_id
from acp.rerank import Reranker

CANDIDATES = 30  # shortlist size from each searcher before merging
RRF_K = 60  # standard damping constant for reciprocal-rank fusion
_WORD = re.compile(r"[A-Za-z0-9]{2,}")
_STOP = frozenset(
    "the a an and or of to in on for with is are was were be by as at it its this that these those what which who "
    "whom how why when where does do did from about into than then there their they them his her not".split()
)


def rrf(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal-rank fusion: score(item) = sum over lists of 1 / (k + rank). Rank 1 is best."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def fts_query(question: str) -> str | None:
    """Turn free text into a safe FTS5 query: quoted words OR-ed together (no syntax errors from punctuation)."""
    words = [w.lower() for w in _WORD.findall(question) if w.lower() not in _STOP]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words)) or None


class DocRetriever:
    """Search ONE document owned by ONE user. Implements the `Retriever` protocol (search(query, k)).

    Isolation is enforced twice: every SQL query filters by owner_id AND doc_id, and the vector index is a file
    inside that owner's own directory.
    """

    def __init__(
        self,
        kb: KnowledgeBase,
        conn_factory: Callable[[], sqlite3.Connection],
        owner_id: str,
        doc_id: str,
        filename: str,
        mode: str = "hybrid",
        reranker: Reranker | None = None,
    ) -> None:
        if mode not in ("dense", "bm25", "hybrid"):
            raise ValueError(f"unknown mode {mode!r}")
        self.kb, self._conn_factory = kb, conn_factory
        self.owner_id, self.doc_id, self.filename = safe_id(owner_id), safe_id(doc_id), filename
        self.mode, self.reranker = mode, reranker

    def _bm25(self, conn: sqlite3.Connection, question: str, n: int) -> list[int]:
        """Chunk positions (idx), best BM25 match first."""
        q = fts_query(question)
        if q is None:
            return []
        rows = conn.execute(
            "SELECT c.idx FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
            "WHERE chunks_fts MATCH ? AND f.doc_id = ? AND f.owner_id = ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (q, self.doc_id, self.owner_id, n),
        ).fetchall()
        return [r[0] for r in rows]

    def _documents(self, conn: sqlite3.Connection, idxs: list[int]) -> list[Document]:
        if not idxs:
            return []
        marks = ",".join("?" * len(idxs))
        rows = conn.execute(
            f"SELECT id, idx, page, page_label, section, kind, text FROM chunks "
            f"WHERE doc_id=? AND owner_id=? AND idx IN ({marks})",
            [self.doc_id, self.owner_id, *idxs],
        ).fetchall()
        by_idx = {r["idx"]: r for r in rows}
        docs = []
        for i in idxs:
            r = by_idx.get(i)
            if r is None:
                continue
            docs.append(
                Document(
                    page_content=r["text"],
                    metadata={
                        "source": self.filename,
                        "citation": citation_label(self.filename, r["page"]),
                        "page": r["page"],
                        "page_label": r["page_label"],
                        "section": r["section"],
                        "doc_id": self.doc_id,
                        "chunk_id": r["id"],
                        "idx": r["idx"],
                        "kind": r["kind"],
                    },
                )
            )
        return docs

    def candidates(self, query: str, n: int = CANDIDATES) -> list[int]:
        """Fused, ordered chunk positions before any reranking (also used by the retrieval evals)."""
        conn = self._conn_factory()
        try:
            dense = self.kb.dense_search(self.owner_id, self.doc_id, query, n) if self.mode != "bm25" else []
            sparse = self._bm25(conn, query, n) if self.mode != "dense" else []
        finally:
            conn.close()
        if self.mode == "dense":
            return dense
        if self.mode == "bm25":
            return sparse
        fused = rrf([dense, sparse])
        return sorted(fused, key=lambda i: -fused[i])

    def search(self, query: str, k: int) -> list[Document]:
        order = self.candidates(query)
        conn = self._conn_factory()
        try:
            if self.reranker is not None and order:
                pool = self._documents(conn, order[:CANDIDATES])
                return [d for d, _ in self.reranker.rerank(query, pool)][:k]
            return self._documents(conn, order[:k])
        finally:
            conn.close()
