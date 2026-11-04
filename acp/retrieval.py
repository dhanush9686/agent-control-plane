"""Search the vector index.

Analogy: the librarian. You hand over a question, they return the k index
cards that are closest in meaning.
"""

from __future__ import annotations

from typing import Protocol

from langchain_core.documents import Document

from acp.config import Settings
from acp.errors import EmptyIndexError


class Retriever(Protocol):
    """Anything with .search(query, k) works -- tests plug in a fake one."""

    def search(self, query: str, k: int) -> list[Document]: ...


class FaissRetriever:
    def __init__(self, settings: Settings) -> None:
        if not (settings.index_path / "index.faiss").is_file():
            raise EmptyIndexError(
                f"No search index at {settings.index_path}. Build it first: `make index` (or `python build_index.py`)."
            )
        # Imported here so unit tests never pay the cost of loading torch.
        from langchain_community.vectorstores import FAISS
        from langchain_huggingface import HuggingFaceEmbeddings

        embeddings = HuggingFaceEmbeddings(model_name=settings.embedding_model)
        self._store = FAISS.load_local(
            str(settings.index_path), embeddings, allow_dangerous_deserialization=True
        )  # safe here: the index is a file we built ourselves, never downloaded

    def search(self, query: str, k: int) -> list[Document]:
        return self._store.similarity_search(query, k=k)
