"""Test doubles for the vector service. Pure Python, no numpy, no network.

InMemoryVectorStore behaves like Pinecone from the knowledge base's point of view (namespaces, doc filter, ids like
'<doc>#<i>', cosine ranking). It is only for offline unit tests; production uses PineconeStore.
"""

from __future__ import annotations

from collections.abc import Callable


class InMemoryVectorStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, list[float]]] = {}  # namespace -> id -> vector
        self.visible_calls = 0

    def upsert(self, namespace, doc_id, vectors, progress: Callable[[int, int], None] | None = None) -> None:
        ns = self.data.setdefault(namespace, {})
        for i, v in enumerate(vectors):
            ns[f"{doc_id}#{i}"] = list(v)
        if progress:
            progress(len(vectors), len(vectors))

    def wait_until_visible(self, namespace, doc_id, count) -> None:
        self.visible_calls += 1

    def query(self, namespace, doc_id, vector, top_k) -> list[int]:
        scored = []
        for vid, v in self.data.get(namespace, {}).items():
            if vid.startswith(f"{doc_id}#"):
                scored.append((sum(a * b for a, b in zip(v, vector, strict=True)), int(vid.split("#")[1])))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [i for _, i in scored[:top_k]]

    def delete_doc(self, namespace, doc_id) -> None:
        ns = self.data.get(namespace, {})
        for vid in [k for k in ns if k.startswith(f"{doc_id}#")]:
            del ns[vid]

    def count(self, namespace, doc_id) -> int:
        return sum(1 for k in self.data.get(namespace, {}) if k.startswith(f"{doc_id}#"))


class FailingVectorStore(InMemoryVectorStore):
    """Writes some vectors then fails, to prove a failed ingest cleans up after itself."""

    def upsert(self, namespace, doc_id, vectors, progress=None) -> None:
        super().upsert(namespace, doc_id, vectors[:1], None)
        raise RuntimeError("pinecone is down")
