"""Command line: `python -m acp.cli "your question"`."""

from __future__ import annotations

import argparse
import sys

from acp.errors import ACPError
from acp.graph import build_graph
from acp.config import get_settings
from acp.llm import get_llm
from acp.nodes import extract_citations
from acp.retrieval import FaissRetriever
from acp.state import initial_state

DEMO_QUESTIONS = [
    "Who kills Garuda, and how does Rocky's story end?",
    "Who plays Deva in Salaar?",
    "What is the plot of Dragon?",
    "Are KGF and Salaar set in the same universe?",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acp", description="Ask the film knowledge-base agent a question.")
    parser.add_argument("question", nargs="*", help="Question to ask (omit with --demo)")
    parser.add_argument("--demo", action="store_true", help="Run four built-in demo questions")
    args = parser.parse_args(argv)
    questions = DEMO_QUESTIONS if args.demo else [" ".join(args.question)]
    if not questions[0].strip():
        parser.error("give a question, or use --demo")

    try:
        settings = get_settings()
        graph = build_graph(get_llm(settings), FaissRetriever(settings), settings)
        for q in questions:
            final = graph.invoke(initial_state(q))
            print(f"\nQ: {q}")
            print(f"A: {final['answer']}")
            print(
                f"   [attempts={final['search_attempts']} chunks={len(final['retrieved_docs'])} "
                f"sufficient={final['sufficient_context']} cited={extract_citations(final['answer'])}]"
            )
    except ACPError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
