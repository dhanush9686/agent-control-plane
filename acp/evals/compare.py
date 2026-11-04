"""Compare two evaluation runs on the SAME golden questions (agent vs agent).

Analogy: a taste test where both cooks make the same 58 dishes. Counting wins alone can mislead with a
small sample, so we pair the results per dish and use a sign test: only dishes where exactly one cook
failed carry information ("discordant pairs"). The p-value says how surprising that split would be if
the two cooks were really equally good.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from math import comb
from typing import Any

from acp import store
from acp.config import ROOT, get_settings
from acp.evals.report import aggregate

OUT = ROOT / "results" / "comparison.json"


def sign_test_p(b: int, c: int) -> float:
    """Two-sided exact sign (McNemar) test on discordant pair counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def load_eval(conn: sqlite3.Connection, eval_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM eval_runs WHERE eval_id=?", (eval_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown eval_id {eval_id}")
    results = [
        json.loads(r["result"]) for r in conn.execute("SELECT result FROM eval_results WHERE eval_id=?", (eval_id,))
    ]
    return {"eval_id": eval_id, "summary": json.loads(row["summary"]), "results": {r["case_id"]: r for r in results}}


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    shared = sorted(set(a["results"]) & set(b["results"]))
    if not shared:
        raise ValueError("the two evals share no questions")
    only_a = [c for c in shared if a["results"][c]["passed"] and not b["results"][c]["passed"]]
    only_b = [c for c in shared if b["results"][c]["passed"] and not a["results"][c]["passed"]]

    def side(e: dict[str, Any]) -> dict[str, Any]:
        sub = [e["results"][c] for c in shared]
        s = aggregate(sub)
        calls = [r.get("llm_calls") for r in sub if r.get("llm_calls") is not None]
        return {
            "eval_id": e["eval_id"],
            "agent": e["summary"].get("agent", "graph"),
            "model": e["summary"].get("agent_model"),
            "label": e["summary"].get("label"),
            "summary": s,
            "avg_llm_calls": round(sum(calls) / len(calls), 2) if calls else None,
        }

    return {
        "n_shared": len(shared),
        "a": side(a),
        "b": side(b),
        "only_a_passes": only_a,
        "only_b_passes": only_b,
        "sign_test_p": round(sign_test_p(len(only_a), len(only_b)), 4),
    }


def render_markdown(c: dict[str, Any]) -> str:
    A, B = c["a"], c["b"]

    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v * 100:.1f}%"

    def name(x: dict[str, Any]) -> str:
        return f"{x['agent']} ({x['model']})"

    rows = ["answered_correctly_rate", "hallucination_rate", "refusal_correctness", "citation_rate", "pass_rate"]
    lines = [f"| Metric | {name(A)} | {name(B)} |", "|---|---|---|"]
    for m in rows:
        lines.append(f"| {m} | {pct(A['summary'][m])} | {pct(B['summary'][m])} |")
    lines.append(f"| avg latency (ms) | {A['summary']['avg_latency_ms']} | {B['summary']['avg_latency_ms']} |")
    lines.append(f"| avg cost / q (est. USD) | {A['summary']['avg_cost_usd']} | {B['summary']['avg_cost_usd']} |")
    lines.append(f"| avg LLM calls / q | {A['avg_llm_calls']} | {B['avg_llm_calls']} |")
    lines += [
        "",
        f"Paired on {c['n_shared']} questions: only {A['agent']} passed {len(c['only_a_passes'])}, "
        f"only {B['agent']} passed {len(c['only_b_passes'])}; sign-test p = {c['sign_test_p']}.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="acp.evals.compare")
    p.add_argument("eval_a")
    p.add_argument("eval_b")
    args = p.parse_args(argv)
    conn = store.connect(get_settings().db_path)
    try:
        out = compare(load_eval(conn, args.eval_a), load_eval(conn, args.eval_b))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    OUT.write_text(json.dumps(out, indent=2))
    print(render_markdown(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
