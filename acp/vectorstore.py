"""Vector storage: where each chunk's "coordinates" live so we can find the nearest ones to a question.

Analogy: a map room. Every chunk of every document is pinned on a giant map by its meaning. Pinecone is the hosted
map room. Each user gets their own room (a *namespace*), and each document's pins are tagged with its id, so a
search is always "inside THIS user's room, only pins tagged with THIS document".

Only vectors and ids go to Pinecone. The chunk TEXT, page numbers and sections stay in our own database, so the
text of a user's document is never stored in the vector service (the vectors are derived from it, which is a
privacy note worth stating plainly).
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from typing import Any, Protocol

from acp.errors import ACPError

UPSERT_BATCH = 100  # vectors per request (keeps each request far below Pinecone's 2 MB limit at 384 dimensions)
DELETE_BATCH = 500
INDEX_NAME_DEFAULT = "acp-docs"
VISIBLE_TIMEOUT_S = 60.0


class VectorStoreError(ACPError):
    """The vector service failed or is unreachable; message is user-friendly."""


class VectorStore(Protocol):
    """The only four things the knowledge base asks of a vector service."""

    def upsert(
        self,
        namespace: str,
        doc_id: str,
        vectors: list[list[float]],
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Store vector i of the document under the id '<doc_id>#<i>'."""
        ...

    def wait_until_visible(self, namespace: str, doc_id: str, count: int) -> None:
        """Block until a freshly written document can actually be searched (see PineconeStore)."""
        ...

    def query(self, namespace: str, doc_id: str, vector: list[float], top_k: int) -> list[int]:
        """Positions (idx) of the nearest chunks of this document, best first."""
        ...

    def delete_doc(self, namespace: str, doc_id: str) -> None:
        """Remove every vector of this document."""
        ...


def vector_id(doc_id: str, idx: int) -> str:
    return f"{doc_id}#{idx}"


def _retry(fn: Callable[[], Any], what: str, attempts: int = 5) -> Any:
    """Call a Pinecone operation, retrying transient failures (rate limit, 5xx, network) with backoff."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classify below
            last = exc
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            name = type(exc).__name__.lower()
            transient = status in (429, 500, 502, 503, 504) or any(
                w in name for w in ("timeout", "connection", "unavail")
            )
            if not transient or attempt == attempts - 1:
                break
            time.sleep(min(2**attempt + random.random(), 20))
    raise VectorStoreError(
        f"The vector database failed while {what} ({type(last).__name__}). Please retry in a moment."
    ) from last


class PineconeStore:
    """VectorStore backed by a Pinecone serverless index (cosine metric, so normalized vectors score as cosine)."""

    def __init__(self, index: Any, sleep: Callable[[float], None] = time.sleep) -> None:
        self._index = index  # a pinecone Index (or a test double with the same methods)
        self._sleep = sleep

    # ---------------------------------------------------------------- construction
    @classmethod
    def connect(
        cls,
        dimension: int,
        index_name: str | None = None,
        cloud: str | None = None,
        region: str | None = None,
        api_key: str | None = None,
    ) -> PineconeStore:
        """Open the index, creating it (serverless, cosine) if this is the first run. Never prints the key."""
        key = api_key or os.environ.get("PINECONE_API_KEY")
        if not key:
            raise VectorStoreError("PINECONE_API_KEY is not set. Add it to your .env file.")
        from pinecone import Pinecone, ServerlessSpec

        # One Pinecone index holds one vector size, so the default name carries it: acp-docs-1024, acp-docs-384, ...
        name = index_name or os.environ.get("PINECONE_INDEX") or f"{INDEX_NAME_DEFAULT}-{dimension}"
        pc = Pinecone(api_key=key)
        try:
            if not pc.has_index(name):
                pc.create_index(
                    name=name,
                    dimension=dimension,
                    metric="cosine",
                    spec=ServerlessSpec(
                        cloud=cloud or os.environ.get("PINECONE_CLOUD", "aws"),
                        region=region or os.environ.get("PINECONE_REGION", "us-east-1"),
                    ),
                )
            desc = pc.describe_index(name)
            deadline = time.time() + 120
            while not _is_ready(desc) and time.time() < deadline:
                time.sleep(2)
                desc = pc.describe_index(name)
            if int(desc.dimension) != dimension:
                raise VectorStoreError(
                    f"Pinecone index '{name}' has dimension {desc.dimension} but the embedder produces {dimension}. "
                    "Use a different PINECONE_INDEX name or delete the old index."
                )
            return cls(pc.Index(host=desc.host))
        except VectorStoreError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = f"Could not open the Pinecone index '{name}' ({type(e).__name__}: {str(e)[:120]})."
            raise VectorStoreError(msg) from e

    # ---------------------------------------------------------------- VectorStore
    def upsert(
        self,
        namespace: str,
        doc_id: str,
        vectors: list[list[float]],
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        total = len(vectors)
        for start in range(0, total, UPSERT_BATCH):
            batch = [
                {"id": vector_id(doc_id, start + i), "values": v, "metadata": {"doc_id": doc_id}}
                for i, v in enumerate(vectors[start : start + UPSERT_BATCH])
            ]
            _retry(lambda b=batch: self._index.upsert(vectors=b, namespace=namespace), "saving the document")
            if progress:
                progress(min(start + UPSERT_BATCH, total), total)

    def wait_until_visible(self, namespace: str, doc_id: str, count: int) -> None:
        """Pinecone serverless is eventually consistent: a vector written a moment ago may not appear in queries yet.
        Wait until the first and last vector can be fetched, so we never mark a document 'ready' while questions
        would silently return nothing."""
        if count == 0:
            return
        want = [vector_id(doc_id, 0), vector_id(doc_id, count - 1)]
        deadline = time.time() + VISIBLE_TIMEOUT_S
        while True:
            got = _retry(
                lambda: self._index.fetch(ids=want, namespace=namespace), "checking the document is searchable"
            )
            if len(getattr(got, "vectors", {}) or {}) >= len(set(want)):
                return
            if time.time() > deadline:
                raise VectorStoreError("The vector database is slow to index this document; please retry shortly.")
            self._sleep(1.0)

    def query(self, namespace: str, doc_id: str, vector: list[float], top_k: int) -> list[int]:
        res = _retry(
            lambda: self._index.query(
                vector=vector, top_k=top_k, namespace=namespace, filter={"doc_id": {"$eq": doc_id}}
            ),
            "searching the document",
        )
        out = []
        for m in res.matches:
            _, _, pos = m.id.rpartition("#")
            if pos.isdigit():
                out.append(int(pos))
        return out

    def delete_doc(self, namespace: str, doc_id: str) -> None:
        """Delete by id prefix (delete-by-metadata is not available on every serverless plan).

        Pinecone's list() yields pages shaped like ListResponse(vectors=[ListItem(id=...), ...]), and delete()
        rejects an empty id list, so a document with no vectors is simply a no-op.
        """
        ids: list[str] = []
        pages = _retry(lambda: list(self._index.list(prefix=f"{doc_id}#", namespace=namespace)), "listing vectors")
        for page in pages:
            ids.extend(item.id for item in page.vectors)
        for i in range(0, len(ids), DELETE_BATCH):
            chunk = ids[i : i + DELETE_BATCH]
            _retry(lambda c=chunk: self._index.delete(ids=c, namespace=namespace), "deleting the document")


def _is_ready(desc: Any) -> bool:
    status = desc.status
    return bool(status["ready"] if isinstance(status, dict) else getattr(status, "ready", False))
