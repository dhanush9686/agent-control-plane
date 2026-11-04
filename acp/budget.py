"""Context budget: make sure what we send to the model fits under the free tier's tokens-per-minute cap.

Analogy: packing a suitcase with a weight limit. Retrieval returns the best chunks first; if the bag is too heavy
we shorten the long items and leave out the least useful ones from the end, never the best ones.
"""

from __future__ import annotations

import math

from langchain_core.documents import Document

CHARS_PER_TOKEN = 3.5  # a deliberately pessimistic English estimate (the usual figure is ~4), so we under-pack
PER_CHUNK_CHARS = 1000
OVERHEAD_TOKENS = 450  # instructions + question + answer headroom around the context


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def fit_documents(
    docs: list[Document], budget_tokens: int, per_chunk_chars: int = PER_CHUNK_CHARS
) -> tuple[list[Document], int]:
    """Return (documents that fit, number dropped). `docs` must be ordered best first.

    1. every chunk is truncated to `per_chunk_chars` (a 900-character chunk is already under it),
    2. chunks are kept from the front until the budget (minus fixed overhead) is used up,
    3. at least one chunk is always kept, so an answer is never starved of all context.
    """
    available = max(budget_tokens - OVERHEAD_TOKENS, 1)
    kept: list[Document] = []
    used = 0
    for d in docs:
        text = d.page_content
        if len(text) > per_chunk_chars:
            text = text[:per_chunk_chars].rsplit(" ", 1)[0] + " ..."
            d = Document(page_content=text, metadata=dict(d.metadata))
        cost = (
            estimate_tokens(text)
            + estimate_tokens(str(d.metadata.get("citation") or d.metadata.get("source", "")))
            + 12
        )
        if kept and used + cost > available:
            break
        kept.append(d)
        used += cost
    return kept, len(docs) - len(kept)
