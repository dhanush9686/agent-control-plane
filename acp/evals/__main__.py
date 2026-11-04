"""`python -m acp.evals` -- run the golden set and print the report."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from acp import history
from acp.config import get_settings
from acp.errors import ACPError
from acp.evals.golden import load_golden
from acp.evals.report import render_markdown
from acp.evals.runner import REPORT_PATH, judge_settings, run_eval
from acp.graph import AGENT_KINDS
from acp.llm import get_llm
from acp.retrieval import FaissRetriever
from acp.runner import Agent
from acp.tracing import Tracing


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="acp.evals", description="Run the golden-set evaluation.")
    p.add_argument("--golden", type=Path, help="Golden-set YAML (default evals/golden.yaml)")
    p.add_argument("--ids", nargs="*", help="Only these case ids")
    p.add_argument("--categories", nargs="*", help="Only these categories")
    p.add_argument("--limit", type=int, help="Only the first N cases (handy on free-tier rate limits)")
    p.add_argument("--pause", type=float, default=0.0, help="Seconds to sleep between questions")
    p.add_argument("--judge-model", help="Grader model (default: settings.judge_model; 'same' = the answering model)")
    p.add_argument(
        "--agent", choices=AGENT_KINDS, default="graph", help="graph = full agent, naive = one-shot RAG baseline"
    )
    p.add_argument("--agent-model", help="Override the answering model (e.g. openai/gpt-oss-20b)")
    p.add_argument("--workers", type=int, default=1, help="Questions to run in parallel (3-4 is safe on the free tier)")
    p.add_argument("--label", help="Free-text label stored with the run (e.g. 'baseline')")
    args = p.parse_args(argv)

    cases = load_golden(args.golden) if args.golden else load_golden()
    # Only the default golden set may write the file the regression gate reads (and the trend history);
    # other golden sets (e.g. SQuAD) get their own report so they can never clobber it.
    custom = args.golden is not None
    report_path = REPORT_PATH.with_name(f"eval_report_{args.golden.parent.name}.json") if custom else REPORT_PATH
    history_path = None if custom else history.HISTORY_PATH
    if args.ids:
        cases = [c for c in cases if c.id in args.ids]
    if args.categories:
        cases = [c for c in cases if c.category in args.categories]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        p.error("no cases selected")

    try:
        settings = get_settings()
        if args.agent_model:
            settings = replace(settings, model=args.agent_model)
        agent = Agent(get_llm(settings), FaissRetriever(settings), settings, Tracing(settings), kind=args.agent)
        judge_name = args.judge_model or settings.judge_model
        judge = get_llm(judge_settings(settings, None if judge_name == "same" else judge_name))
        report = run_eval(
            agent,
            judge,
            cases,
            settings,
            pause_s=args.pause,
            label=args.label,
            workers=args.workers,
            report_path=report_path,
            history_path=history_path,
        )
    except ACPError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("\n" + render_markdown(report["summary"]))
    print(f"\nSaved {report_path} and rows in {settings.db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
