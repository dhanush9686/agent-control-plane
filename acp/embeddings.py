"""Turn text into vectors (lists of numbers) so "similar meaning" becomes "nearby".

Analogy: GPS coordinates for sentences. An embedding model gives each chunk a position; a question gets a position
too; the nearest chunks are the likeliest answers. The default embedder is Amazon Titan Text Embeddings v2 on Bedrock
(the cheapest embedding model available; billed per input token). The `Embedder` protocol lets tests plug in a tiny
deterministic stand-in so they never call AWS or load PyTorch.
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


class BedrockEmbedder:
    """Amazon Titan Text Embeddings v2 through Bedrock. One request per text, run concurrently, order preserved.

    Titan v2 accepts a single `inputText` per call (no batch endpoint), so a 1,300-chunk document is 1,300 small
    requests; 8 run at a time. Vectors come back L2-normalized (`normalize: true`), so cosine similarity in Pinecone
    equals the dot product. `on_usage(model_id, input_tokens)` lets the service count embedding spend.
    """

    SUPPORTED_DIMS = (256, 512, 1024)

    def __init__(self, settings, client=None, on_usage=None, workers: int = 8) -> None:  # noqa: ANN001
        from acp.llm import BedrockLLM  # reuse the credential/region handling and error messages

        if settings.embed_dimensions not in self.SUPPORTED_DIMS:
            raise ValueError(
                f"Titan v2 dimensions must be one of {self.SUPPORTED_DIMS}, got {settings.embed_dimensions}"
            )
        self.dim = settings.embed_dimensions
        self.model = settings.bedrock_embed_model
        self._settings, self._on_usage, self._workers = settings, on_usage, workers
        self._client = client or BedrockLLM._make_client(settings)

    def _embed_one(self, text: str) -> tuple[list[float], int]:
        import json

        from acp.llm import _bedrock_retryable, call_with_retries

        body = json.dumps({"inputText": text, "dimensions": self.dim, "normalize": True})

        def go() -> dict:
            resp = self._client.invoke_model(
                modelId=self.model, body=body, contentType="application/json", accept="application/json"
            )
            return json.loads(resp["body"].read())

        def describe(exc: Exception | None) -> str:
            from acp.errors import ModelAccessError
            from acp.llm import _BEDROCK_NO_ACCESS, _bedrock_code

            if exc is not None and _bedrock_code(exc) in _BEDROCK_NO_ACCESS:
                raise ModelAccessError(
                    f"Amazon Bedrock embedding model '{self.model}' is not available to this AWS account in region "
                    f"{self._settings.aws_region or 'default'}. Enable it under Bedrock > Model access."
                ) from exc
            return f"{type(exc).__name__}: {str(exc)[:160]}"

        out, _, _ = call_with_retries(
            go,
            max_retries=self._settings.llm_max_retries,
            retryable=_bedrock_retryable,
            deadline_s=self._settings.llm_deadline_s,
            describe=describe,
        )
        return [float(x) for x in out["embedding"]], int(out.get("inputTextTokenCount", 0))

    def embed(self, texts: list[str]) -> list[list[float]]:
        from concurrent.futures import ThreadPoolExecutor

        if not texts:
            return []
        with ThreadPoolExecutor(max_workers=min(self._workers, len(texts))) as pool:
            results = list(pool.map(self._embed_one, texts))
        if self._on_usage:
            self._on_usage(self.model, sum(tokens for _, tokens in results))
        return [vec for vec, _ in results]


def make_embedder(settings, on_usage=None) -> Embedder:  # noqa: ANN001
    """The embedder named by ACP_EMBEDDING_PROVIDER: Bedrock Titan (default) or local MiniLM (offline fallback)."""
    if settings.embedding_provider == "bedrock":
        return BedrockEmbedder(settings, on_usage=on_usage)
    if settings.embedding_provider == "minilm":
        return MiniLMEmbedder.shared(settings.embedding_model)
    raise ValueError(f"Unknown ACP_EMBEDDING_PROVIDER {settings.embedding_provider!r}; use 'bedrock' or 'minilm'.")
