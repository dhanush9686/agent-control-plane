"""What the agent needs from a search backend.

Analogy: the librarian's job description. Anything that can take a question and return the k closest document
excerpts qualifies; the document service plugs in hybrid (vector + keyword) search, tests plug in a fake.
"""

from __future__ import annotations

from typing import Protocol

from langchain_core.documents import Document


class Retriever(Protocol):
    """Anything with .search(query, k) works."""

    def search(self, query: str, k: int) -> list[Document]: ...
