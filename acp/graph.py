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


def build_graph(llm: LLM, retriever: Retriever, settings: Settings, wrap_node: Callable | None = None):
    """retrieve -> judge -> (refine loop) -> synthesize."""
    nodes = make_nodes(llm, retriever, settings)
    g = StateGraph(AgentState)
    for name, fn in nodes.items():
        g.add_node(name, wrap_node(name, fn) if wrap_node else fn)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "judge")
    g.add_conditional_edges(
        "judge", make_router(settings.max_attempts), {"refine_query": "refine_query", "synthesize": "synthesize"}
    )
    g.add_edge("refine_query", "retrieve")
    g.add_edge("synthesize", END)
    return g.compile()
