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


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion k/n.

    Analogy: "we saw 17 of 19 pass" is not "the true rate is 89%". The interval is the plausible range
    of the true rate given so few questions. It stays sensible near 0% and 100% (unlike the naive +/- formula).
    """
    if not n:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


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
    counts = {
        "answered_correctly_rate": (sum(1 for r in should_answer if not r["behavior_failures"]), len(should_answer)),
        "hallucination_rate": (sum(1 for r in answered if r["hallucinated"]), len(answered)),
        "refusal_correctness": (sum(1 for r in ok if r["refused"] == (r["expect"] == "refuse")), len(ok)),
        "citation_rate": (sum(1 for r in answered if r["citation_ok"]), len(answered)),
        "pass_rate": (sum(1 for r in ok if r["passed"]), len(results)),
    }
    summary["counts"] = {m: {"k": k, "n": n} for m, (k, n) in counts.items()}
    summary["ci95"] = {m: wilson(k, n) for m, (k, n) in counts.items()}
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

    def with_ci(metric: str) -> str:
        ci = summary.get("ci95", {}).get(metric)
        return pct(summary[metric]) + (f" (95% CI {pct(ci[0])}-{pct(ci[1])})" if ci else "")

    lines = [
        "| Metric | Value |",
        "|---|---|",
        f"| Questions | {summary['n_cases']} ({summary['n_errors']} errors) |",
        f"| Answered-correctly rate | {with_ci('answered_correctly_rate')} |",
        f"| Hallucination rate | {with_ci('hallucination_rate')} |",
        f"| Refusal correctness | {with_ci('refusal_correctness')} |",
        f"| Citation rate | {with_ci('citation_rate')} |",
        f"| Overall pass rate | {with_ci('pass_rate')} |",
        f"| Avg latency | {summary['avg_latency_ms']} ms |",
        f"| Avg cost / question (est.) | ${summary['avg_cost_usd']} |",
        "",
        "| Category | n | Pass rate | Hallucinations | Avg latency (ms) |",
        "|---|---|---|---|---|",
    ]
    for cat, c in summary["per_category"].items():
        lines.append(f"| {cat} | {c['n']} | {pct(c['pass_rate'])} | {c['hallucinations']} | {c['avg_latency_ms']} |")
    return "\n".join(lines)
