"""Run one question end-to-end, traced.

Analogy: the front desk. It opens a new case folder (run_id), lets the agent
work, then staples the final answer and the evidence onto the folder.
"""

from __future__ import annotations

import json
from typing import Any

from acp.config import Settings
from acp.graph import build_graph
from acp.llm import LLM
from acp.retrieval import Retriever
from acp.state import AgentState, initial_state
from acp.tracing import TracedLLM, Tracing, trace_node


class Agent:
    """A ready-to-ask agent. Pass `tracing=None` to run without recording anything."""

    def __init__(self, llm: LLM, retriever: Retriever, settings: Settings, tracing: Tracing | None = None) -> None:
        self.settings = settings
        self.tracing = tracing
        if tracing is not None:
            llm = TracedLLM(llm, tracing)
        self._graph = build_graph(
            llm,
            retriever,
            settings,
            wrap_node=(lambda n, f: trace_node(n, f, tracing)) if tracing else None,
        )

    def ask(
        self, question: str, *, user_id: str | None = None, doc_id: str | None = None
    ) -> tuple[AgentState, str | None]:
        """Returns (final_state, run_id). run_id is None when tracing is off.

        `user_id` / `doc_id` tag the trace so a user can later see (and delete) only their own runs.
        """
        if self.tracing is None:
            return self._graph.invoke(initial_state(question)), None
        with self.tracing.run(question, user_id=user_id, doc_id=doc_id) as (run_id, sp):
            final = self._graph.invoke(initial_state(question))
            sp.attr(
                answer=final.get("answer"),
                attempts=final.get("search_attempts"),
                sufficient=int(bool(final.get("sufficient_context"))),
                retrieved_json=json.dumps(self._retrieved_for_trace(final.get("retrieved_docs", []))),
            )
            return final, run_id

    def _retrieved_for_trace(self, docs: list[Any]) -> list[dict[str, Any]]:
        """What to remember about the retrieved chunks.

        Chunks from a user's uploaded document keep only ids and page numbers unless ACP_STORE_CHUNK_TEXT=1, so a
        trace cannot become a second copy of their private document. Chunks without a citation keep their text.
        """
        out = []
        for d in docs:
            if "citation" in d.metadata and not self.settings.store_chunk_text:
                out.append(
                    {
                        "source": d.metadata.get("source"),
                        "citation": d.metadata.get("citation"),
                        "page": d.metadata.get("page"),
                        "chunk_id": d.metadata.get("chunk_id"),
                    }
                )
            else:
                out.append({"source": d.metadata.get("source"), "text": d.page_content})
        return out


def summarize_run(db_path: Any, run_id: str) -> dict[str, Any] | None:
    from acp import store

    conn = store.connect(db_path)
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
