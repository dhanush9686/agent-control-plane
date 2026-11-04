"""Optional second look: re-score the best candidates with a model that reads the question AND the chunk together.

Analogy: the first pass (vectors + keywords) is a librarian pulling 30 likely books fast; the reranker is an
expert who actually skims each one next to your question and picks the best 5. Slower, so it is optional.
"""

from __future__ import annotations

from typing import Protocol

from langchain_core.documents import Document


class Reranker(Protocol):
    def rerank(self, query: str, docs: list[Document]) -> list[tuple[Document, float]]:
        """Return (doc, score) pairs sorted best first."""
        ...


class CrossEncoderReranker:
    """cross-encoder/ms-marco-MiniLM-L-6-v2 (~90 MB, CPU). Loaded lazily on first use."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self._name = model_name
        self._model = None

    def rerank(self, query: str, docs: list[Document]) -> list[tuple[Document, float]]:
        if not docs:
            return []
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self._name, device="cpu")
        scores = self._model.predict([(query, d.page_content) for d in docs], show_progress_bar=False)
        return sorted(zip(docs, (float(s) for s in scores), strict=True), key=lambda x: -x[1])
