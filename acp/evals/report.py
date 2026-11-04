"""Turn per-question results into the headline numbers + per-category table.

Definitions (also printed in the README so nobody has to guess):
  answered_correctly_rate  of questions that SHOULD be answered, share answered with right content, no banned phrase
  hallucination_rate       share of non-refused answers with >=1 claim the judge found unsupported
  refusal_correctness      share of ALL questions where refuse-vs-answer matched the expectation
  citation_rate            share of non-refused answers with valid citations
  pass_rate                share of questions with no failure of any kind
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if not r.get("error")]
    should_answer = [r for r in ok if r["expect"] == "answer"]
    answered = [r for r in ok if not r["refused"]]
    summary: dict[str, Any] = {
        "n_cases": len(results),
        "n_errors": len(results) - len(ok),
        "answered_correctly_rate": _rate(
            sum(1 for r in should_answer if not r["behavior_failures"]), len(should_answer)
        ),
        "hallucination_rate": _rate(sum(1 for r in answered if r["hallucinated"]), len(answered)),
        "refusal_correctness": _rate(sum(1 for r in ok if r["refused"] == (r["expect"] == "refuse")), len(ok)),
        "citation_rate": _rate(sum(1 for r in answered if r["citation_ok"]), len(answered)),
        "pass_rate": _rate(sum(1 for r in ok if r["passed"]), len(results)),
        "avg_grounding_score": round(sum(r["grounding_score"] for r in answered) / len(answered), 4)
        if answered
        else None,
        "avg_latency_ms": round(sum(r["latency_ms"] for r in ok) / len(ok), 1) if ok else None,
        "avg_cost_usd": round(sum(r["cost_usd"] for r in ok) / len(ok), 6) if ok else None,
        "total_cost_usd": round(sum(r["cost_usd"] for r in ok), 6),
    }
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    summary["per_category"] = {
        cat: {
            "n": len(rs),
            "pass_rate": _rate(sum(1 for r in rs if r.get("passed")), len(rs)),
            "hallucinations": sum(1 for r in rs if r.get("hallucinated")),
            "avg_latency_ms": round(
                sum(r["latency_ms"] for r in rs if not r.get("error"))
                / max(1, sum(1 for r in rs if not r.get("error"))),
                1,
            ),
        }
        for cat, rs in sorted(by_cat.items())
    }
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v * 100:.1f}%"

    lines = [
        "| Metric | Value |",
        "|---|---|",
        f"| Questions | {summary['n_cases']} ({summary['n_errors']} errors) |",
        f"| Answered-correctly rate | {pct(summary['answered_correctly_rate'])} |",
        f"| Hallucination rate | {pct(summary['hallucination_rate'])} |",
        f"| Refusal correctness | {pct(summary['refusal_correctness'])} |",
        f"| Citation rate | {pct(summary['citation_rate'])} |",
        f"| Overall pass rate | {pct(summary['pass_rate'])} |",
        f"| Avg latency | {summary['avg_latency_ms']} ms |",
        f"| Avg cost / question (est.) | ${summary['avg_cost_usd']} |",
        "",
        "| Category | n | Pass rate | Hallucinations | Avg latency (ms) |",
        "|---|---|---|---|---|",
    ]
    for cat, c in summary["per_category"].items():
        lines.append(f"| {cat} | {c['n']} | {pct(c['pass_rate'])} | {c['hallucinations']} | {c['avg_latency_ms']} |")
    return "\n".join(lines)
