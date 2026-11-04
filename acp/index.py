"""Chunk the corpus and build the vector index.

Analogy: cutting a book into index cards (chunks) and filing each card by
meaning (embedding) so we can later find the cards closest to a question.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document

from acp.config import Settings, get_settings
from acp.errors import ACPError


def load_and_chunk(path: Path) -> list[Document]:
    """Split on '=== Title ===' headers, then on blank lines (one chunk per paragraph)."""
    raw = Path(path).read_text(encoding="utf-8")
    documents: list[Document] = []
    for section in raw.split("=== ")[1:]:
        header, _, body = section.partition("===")
        source = header.strip()
        for para in (p.strip() for p in body.strip().split("\n\n")):
            if para:
                documents.append(Document(page_content=para, metadata={"source": source}))
    return documents


def build_index(settings: Settings | None = None) -> int:
    """Embed every chunk and save a FAISS index. Returns the chunk count."""
    from langchain_community.vectorstores import FAISS
    from langchain_huggingface import HuggingFaceEmbeddings

    s = settings or get_settings()
    docs = load_and_chunk(s.corpus_path)
    if not docs:
        raise ACPError(f"No chunks created from {s.corpus_path}: check the '=== Title ===' headers.")
    store = FAISS.from_documents(docs, HuggingFaceEmbeddings(model_name=s.embedding_model))
    s.index_path.parent.mkdir(parents=True, exist_ok=True)
    store.save_local(str(s.index_path))
    return len(docs)
