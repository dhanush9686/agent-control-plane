"""Turn text into vectors (lists of numbers) so "similar meaning" becomes "nearby".

Analogy: GPS coordinates for sentences. MiniLM (a small free model that runs on your CPU) gives each chunk a
position; a question gets a position too; the nearest chunks are the likeliest answers. The `Embedder` protocol
lets tests plug in a tiny deterministic stand-in so they never load PyTorch.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalized vector (a list of `dim` floats) per text."""
        ...


class MiniLMEmbedder:
    """sentence-transformers/all-MiniLM-L6-v2 on CPU. Load once per process; it is the slow, memory-hungry part."""

    _cache: dict[str, MiniLMEmbedder] = {}

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2", batch_size: int = 64) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device="cpu")
        self.dim = int(self._model.get_embedding_dimension())
        self.batch_size = batch_size

    @classmethod
    def shared(cls, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> MiniLMEmbedder:
        if model_name not in cls._cache:
            cls._cache[model_name] = cls(model_name)
        return cls._cache[model_name]

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False)
        return vecs.tolist()


class HashEmbedder:
    """Deterministic bag-of-words stand-in for tests and offline demos: no model, no download.

    Each word is hashed into one of `dim` buckets, so texts sharing words get similar vectors. It is NOT good
    at meaning (no synonyms) but is enough to test the plumbing (index, filters, fusion, citations).
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                h = int.from_bytes(hashlib.md5(w.encode()).digest()[:4], "little")
                v[h % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out
