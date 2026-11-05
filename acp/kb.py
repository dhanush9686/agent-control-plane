"""The knowledge-base store: one searchable index per uploaded document, owned by one user.

Analogy: a filing cabinet with one locked drawer per document. A drawer holds the vector index (a Pinecone
namespace) and the chunk text/page numbers (SQLite rows, also keyword-indexed). Deleting a document empties its
drawer completely.

Layout:  <kb_root>/<owner_id>/<doc_id>/source.pdf  on disk; chunk rows in SQLite; vectors in Pinecone
(namespace = owner_id, vector id = '<doc_id>#<position>'). Chunk TEXT never leaves our database.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from acp.embeddings import Embedder
from acp.errors import ACPError
from acp.ingest.chunking import Chunk
from acp.vectorstore import VectorStore

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
    def __init__(self, root: Path, embedder: Embedder, store: VectorStore) -> None:
        self.root = Path(root)
        self.embedder = embedder
        self.store = store

    # ------------------------------------------------------------------ paths
    def doc_dir(self, owner_id: str, doc_id: str) -> Path:
        return self.root / safe_id(owner_id) / safe_id(doc_id)

    def pdf_path(self, owner_id: str, doc_id: str) -> Path:
        return self.doc_dir(owner_id, doc_id) / "source.pdf"

    # ------------------------------------------------------------------ write
    def add_document(
        self,
        conn: sqlite3.Connection,
        owner_id: str,
        doc_id: str,
        chunks: list[Chunk],
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Embed every chunk (batched), save rows, upload vectors, and wait until the document is searchable.

        Order matters. Rows are saved first and the vectors second; if uploading fails we remove the rows again, so
        a failed ingest leaves nothing behind. Progress covers the two slow phases: embedding, then uploading.
        """
        owner_id, doc_id = safe_id(owner_id), safe_id(doc_id)
        total = len(chunks)
        vectors: list[list[float]] = []
        for start in range(0, total, EMBED_BATCH):
            vectors += self.embedder.embed([c.text for c in chunks[start : start + EMBED_BATCH]])
            if progress:
                progress(len(vectors), total * 2)  # first half of the bar: embedding
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
        try:
            self.store.upsert(
                owner_id, doc_id, vectors, (lambda done, t: progress(total + done, total * 2)) if progress else None
            )
            self.store.wait_until_visible(owner_id, doc_id, total)
        except BaseException:
            self._delete_rows(conn, owner_id, doc_id)
            try:
                self.store.delete_doc(owner_id, doc_id)  # best effort: remove any vectors that did get written
            except Exception:  # noqa: BLE001
                pass
            raise

    @staticmethod
    def _delete_rows(conn: sqlite3.Connection, owner_id: str, doc_id: str) -> None:
        with conn:
            ids = [
                r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=? AND owner_id=?", (doc_id, owner_id))
            ]
            conn.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
            conn.execute("DELETE FROM chunks WHERE doc_id=? AND owner_id=?", (doc_id, owner_id))

    def delete_document_data_only(self, conn: sqlite3.Connection, owner_id: str, doc_id: str) -> None:
        """Remove rows and vectors but keep the uploaded PDF (so a retried job starts clean)."""
        owner_id, doc_id = safe_id(owner_id), safe_id(doc_id)
        self.store.delete_doc(owner_id, doc_id)
        self._delete_rows(conn, owner_id, doc_id)

    def delete_document(self, conn: sqlite3.Connection, owner_id: str, doc_id: str) -> None:
        """Remove every trace of one document: vectors, source file, chunk rows, keyword index."""
        owner_id, doc_id = safe_id(owner_id), safe_id(doc_id)
        self.store.delete_doc(owner_id, doc_id)
        self._delete_rows(conn, owner_id, doc_id)
        shutil.rmtree(self.doc_dir(owner_id, doc_id), ignore_errors=True)

    # ------------------------------------------------------------------ read
    def dense_search(self, owner_id: str, doc_id: str, query: str, n: int) -> list[int]:
        """Chunk positions (idx) of the n nearest chunks, best first."""
        return self.store.query(safe_id(owner_id), safe_id(doc_id), self.embedder.embed([query])[0], n)
