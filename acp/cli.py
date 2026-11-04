"""Command line: `python -m acp.cli "your question"`."""

from __future__ import annotations

import argparse
import sys

from acp.config import get_settings
from acp.errors import ACPError
from acp.llm import get_llm
from acp.nodes import extract_citations
from acp.retrieval import FaissRetriever
from acp.runner import Agent, summarize_run
from acp.tracing import Tracing

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
    parser.add_argument("--no-trace", action="store_true", help="Do not write a trace to SQLite")
    args = parser.parse_args(argv)
    questions = DEMO_QUESTIONS if args.demo else [" ".join(args.question)]
    if not questions[0].strip():
        parser.error("give a question, or use --demo")

    try:
        settings = get_settings()
        tracing = None
        if not args.no_trace:
            try:
                tracing = Tracing(settings)
            except Exception as e:  # noqa: BLE001 - tracing must never stop the agent
                print(f"warning: tracing disabled ({e})", file=sys.stderr)
        agent = Agent(get_llm(settings), FaissRetriever(settings), settings, tracing)
        for q in questions:
            final, run_id = agent.ask(q)
            print(f"\nQ: {q}")
            print(f"A: {final['answer']}")
            print(
                f"   [attempts={final['search_attempts']} chunks={len(final['retrieved_docs'])} "
                f"sufficient={final['sufficient_context']} cited={extract_citations(final['answer'])}]"
            )
            if run_id:
                r = summarize_run(settings.db_path, run_id)
                if r:
                    print(
                        f"   trace run_id={run_id} latency={r['latency_ms']:.0f}ms llm_calls={r['llm_calls']} "
                        f"tokens={r['input_tokens']}+{r['output_tokens']} cost=${r['cost_usd']:.5f} "
                        f"-> {settings.db_path}"
                    )
    except ACPError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
