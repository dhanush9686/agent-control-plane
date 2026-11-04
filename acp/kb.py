"""The knowledge-base store: one searchable index per uploaded document, owned by one user.

Analogy: a filing cabinet with one locked drawer per document. A drawer holds the vector index (FAISS file) and
the chunk text/page numbers (SQLite rows, also keyword-indexed). Deleting a document empties its drawer completely.

Layout:  <kb_root>/<owner_id>/<doc_id>/{source.pdf, vectors.npy}   (+ rows in the SQLite tables)

Why numpy and not FAISS: a 500-page document is ~2,500 vectors; exact search over them is a single matrix
multiplication (well under a millisecond), so a vector database buys nothing here. Dropping FAISS also removes a
real crash: on macOS, FAISS and PyTorch each bundle their own OpenMP runtime and abort when both run in one
process. Vectors are saved as a plain .npy file and loaded with allow_pickle=False (never unpickle user-derived data).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

import numpy as np

from acp.embeddings import Embedder
from acp.errors import ACPError
from acp.ingest.chunking import Chunk

_ID = re.compile(r"^[a-f0-9]{8,64}$")
EMBED_BATCH = 64


def safe_id(value: str) -> str:
    """Ids become directory names, so allow only lowercase hex (blocks '../' path tricks)."""
    if not _ID.match(value):
        raise ACPError("invalid id")
    return value


def citation_label(filename: str, page: int) -> str:
    """'report.pdf p.37'. Brackets/control characters are stripped so a filename cannot break citation syntax."""
    clean = re.sub(r"[\[\]【】\x00-\x1f\x7f]", "", Path(filename).name).strip() or "document.pdf"
    return f"{clean[:60]} p.{page}"


class KnowledgeBase:
    def __init__(self, root: Path, embedder: Embedder, cache_size: int = 3) -> None:
        self.root = Path(root)
        self.embedder = embedder
        self._cache: OrderedDict[tuple[str, float], np.ndarray] = OrderedDict()
        self._cache_size = cache_size

    # ------------------------------------------------------------------ paths
    def doc_dir(self, owner_id: str, doc_id: str) -> Path:
        return self.root / safe_id(owner_id) / safe_id(doc_id)

    def pdf_path(self, owner_id: str, doc_id: str) -> Path:
        return self.doc_dir(owner_id, doc_id) / "source.pdf"

    def index_path(self, owner_id: str, doc_id: str) -> Path:
        return self.doc_dir(owner_id, doc_id) / "vectors.npy"

    # ------------------------------------------------------------------ write
    def add_document(
        self,
        conn: sqlite3.Connection,
        owner_id: str,
        doc_id: str,
        chunks: list[Chunk],
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Embed every chunk (batched, reporting progress), write the vector file, then store rows + keyword index.

        The vector file is written first and the rows second, in one transaction, so a crash leaves either nothing
        searchable or everything searchable, never half.
        """
        total = len(chunks)
        parts = []
        for start in range(0, total, EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            parts.append(self.embedder.embed([c.text for c in batch]))
            if progress:
                progress(min(start + EMBED_BATCH, total), total)
        vectors = np.concatenate(parts).astype("float32") if parts else np.zeros((0, self.embedder.dim), "float32")
        d = self.doc_dir(owner_id, doc_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "vectors.tmp.npy"
        np.save(tmp, vectors, allow_pickle=False)
        tmp.replace(self.index_path(owner_id, doc_id))  # atomic rename: never a half-written index
        with conn:
            for c in chunks:
                cur = conn.execute(
                    "INSERT INTO chunks (doc_id, owner_id, idx, page, page_label, section, kind, text) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (doc_id, owner_id, c.idx, c.page, c.page_label, c.section, "text", c.text),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (rowid, text, doc_id, owner_id) VALUES (?,?,?,?)",
                    (cur.lastrowid, c.text, doc_id, owner_id),
                )

    def delete_document(self, conn: sqlite3.Connection, owner_id: str, doc_id: str) -> None:
        """Remove every trace of one document: vectors, source file, chunk rows, keyword index."""
        with conn:
            ids = [
                r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=? AND owner_id=?", (doc_id, owner_id))
            ]
            conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
            conn.execute("DELETE FROM chunks WHERE doc_id=? AND owner_id=?", (doc_id, owner_id))
        shutil.rmtree(self.doc_dir(owner_id, doc_id), ignore_errors=True)
        self._cache = OrderedDict((k, v) for k, v in self._cache.items() if safe_id(doc_id) not in k[0])

    # ------------------------------------------------------------------ read
    def load_index(self, owner_id: str, doc_id: str) -> np.ndarray:
        path = self.index_path(owner_id, doc_id)
        if not path.is_file():
            raise ACPError("This document has no search index (it may still be processing, or failed).")
        key = (str(path), path.stat().st_mtime)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        index = np.load(path, allow_pickle=False)
        self._cache[key] = index
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return index

    def dense_search(self, owner_id: str, doc_id: str, query: str, n: int) -> list[int]:
        """Chunk positions (idx) of the n nearest chunks, best first."""
        vectors = self.load_index(owner_id, doc_id)
        if len(vectors) == 0:
            return []
        scores = vectors @ self.embedder.embed([query])[0]  # cosine similarity (vectors are normalized)
        # Full stable sort: ~2,500 scores sort in microseconds, and ties resolve by position (deterministic results).
        return [int(i) for i in np.argsort(-scores, kind="stable")[:n]]
