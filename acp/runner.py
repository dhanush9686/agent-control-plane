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
from acp.tracing import Tracing, TracedLLM, trace_node


class Agent:
    """A ready-to-ask agent. Pass `tracing=None` to run without recording anything."""

    def __init__(self, llm: LLM, retriever: Retriever, settings: Settings, tracing: Tracing | None = None) -> None:
        self.settings = settings
        self.tracing = tracing
        if tracing is not None:
            llm = TracedLLM(llm, tracing)
        self._graph = build_graph(llm, retriever, settings, wrap_node=(lambda n, f: trace_node(n, f, tracing)) if tracing else None)

    def ask(self, question: str) -> tuple[AgentState, str | None]:
        """Returns (final_state, run_id). run_id is None when tracing is off."""
        if self.tracing is None:
            return self._graph.invoke(initial_state(question)), None
        with self.tracing.run(question) as (run_id, sp):
            final = self._graph.invoke(initial_state(question))
            sp.attr(
                answer=final.get("answer"),
                attempts=final.get("search_attempts"),
                sufficient=int(bool(final.get("sufficient_context"))),
                retrieved_json=json.dumps(
                    [{"source": d.metadata.get("source"), "text": d.page_content} for d in final.get("retrieved_docs", [])]
                ),
            )
            return final, run_id


def summarize_run(db_path: Any, run_id: str) -> dict[str, Any] | None:
    from acp import store

    conn = store.connect(db_path)
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
