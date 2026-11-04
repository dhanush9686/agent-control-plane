"""Wire the nodes into a LangGraph StateGraph.

Analogy: a subway map. Stations are nodes; the conditional edge after `judge`
is a junction with a sign: "enough info? -> synthesize; not yet and we still
have tries left? -> refine_query and loop back; out of tries? -> synthesize
anyway (it will say 'not in the knowledge base')".

Why a graph instead of a while-loop: the routing rule is one small testable
function, and the framework can show/trace the path taken.
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from acp.config import Settings
from acp.llm import LLM
from acp.nodes import make_nodes
from acp.retrieval import Retriever
from acp.state import AgentState


def make_router(max_attempts: int):
    def route_after_judge(state: AgentState) -> str:
        """Pure function of state -> next node name. Easy to unit test."""
        if state.get("sufficient_context"):
            return "synthesize"
        if state.get("search_attempts", 0) >= max_attempts:
            return "synthesize"  # retry cap reached: answer (i.e. refuse) with what we have
        return "refine_query"

    return route_after_judge


AGENT_KINDS = ("graph", "naive")


def build_graph(
    llm: LLM, retriever: Retriever, settings: Settings, wrap_node: Callable | None = None, kind: str = "graph"
):
    """kind="graph": retrieve -> judge -> (refine loop) -> synthesize (the full agent).

    kind="naive": retrieve -> synthesize. The classic one-shot RAG baseline: same retriever, same prompt,
    same model, but NO sufficiency check and NO retry. Comparing the two isolates what the judge loop buys us.
    """
    if kind not in AGENT_KINDS:
        raise ValueError(f"unknown agent kind {kind!r}; choose from {AGENT_KINDS}")
    nodes = make_nodes(llm, retriever, settings)
    g = StateGraph(AgentState)
    used = nodes if kind == "graph" else {k: nodes[k] for k in ("retrieve", "synthesize")}
    for name, fn in used.items():
        g.add_node(name, wrap_node(name, fn) if wrap_node else fn)
    g.add_edge(START, "retrieve")
    if kind == "naive":
        g.add_edge("retrieve", "synthesize")
        g.add_edge("synthesize", END)
        return g.compile()
    g.add_edge("retrieve", "judge")
    g.add_conditional_edges(
        "judge", make_router(settings.max_attempts), {"refine_query": "refine_query", "synthesize": "synthesize"}
    )
    g.add_edge("refine_query", "retrieve")
    g.add_edge("synthesize", END)
    return g.compile()
