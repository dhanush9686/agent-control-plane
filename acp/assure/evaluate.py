"""Run the scenario suite against the agent with the guards on and off, report with confidence intervals, and gate.

    python -m acp.assure.evaluate --label baseline                 # both modes, default model
    python -m acp.assure.evaluate --model anthropic.claude-haiku-4-5-20251001-v1:0 --label haiku
    python -m acp.assure.evaluate --gate                           # exit 1 if evals/assure/thresholds.json is violated

"Guarded" is the real configuration. "Unguarded" is the same tools and the same prompt with no policy layer, no
redaction and no numeric check, so the difference between the two is the value of enforcement.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from acp import store
from acp.assure.agent import AssuranceAgent
from acp.assure.audit import AuditLog
from acp.assure.sandbox import Ledger
from acp.assure.scoring import ALL_KINDS, CRITICAL, Scored, score
from acp.config import ROOT, Settings, bedrock_profile_prefix
from acp.evals.report import wilson

SCENARIOS = ROOT / "evals" / "assure" / "scenarios.yaml"
THRESHOLDS = ROOT / "evals" / "assure" / "thresholds.json"
RESULTS = ROOT / "results"


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text())


def resolve_model(name: str, region: str | None) -> str:
    """Add the regional inference-profile prefix when a bare Bedrock model id is given."""
    known = ("apac.", "us.", "eu.", "global.", "jp.", "au.", "ca.", "in.", "arn:")
    return name if name.startswith(known) else f"{bedrock_profile_prefix(region)}.{name}"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _rate(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": wilson(k, n)}


def summarise(rows: list[Scored]) -> dict[str, Any]:
    n = len(rows)
    crit_eff = [r for r in rows if any(r.effective[k] for k in CRITICAL)]
    crit_att = [r for r in rows if any(r.attempted[k] for k in CRITICAL)]
    per_cat: dict[str, dict[str, int]] = {}
    for r in rows:
        c = per_cat.setdefault(r.category, {"n": 0, "passed": 0})
        c["n"] += 1
        c["passed"] += int(r.passed)
    return {
        "n": n,
        "pass": _rate(sum(r.passed for r in rows), n),
        "critical_effective": _rate(len(crit_eff), n),
        "critical_attempted": _rate(len(crit_att), n),
        "by_kind": {
            k: {"attempted": sum(r.attempted[k] for r in rows), "effective": sum(r.effective[k] for r in rows)}
            for k in ALL_KINDS
        },
        "withheld": sum(r.withheld for r in rows),
        "denied_tool_calls": sum(r.denied_calls for r in rows),
        "errors": sum(1 for r in rows if r.error),
        "avg_latency_ms": round(sum(r.latency_ms for r in rows) / n, 1) if n else None,
        "avg_cost_usd": round(sum(r.cost_usd for r in rows) / n, 6) if n else None,
        "per_category": per_cat,
        "failures": [{"id": r.scenario_id, "why": r.failed_checks} for r in rows if not r.passed],
    }


def render(report: dict[str, Any]) -> str:
    g, u = report["summary"]["guarded"], report["summary"].get("unguarded")

    def cell(s: dict[str, Any] | None, key: str) -> str:
        if not s:
            return "-"
        r = s[key]
        ci = r["ci95"]
        return f"{r['k']}/{r['n']} = {_pct(r['rate'])}" + (f" (CI {_pct(ci[0])}-{_pct(ci[1])})" if ci else "")

    lines = [
        f"Model: {report['model']}   {g['n'] // report['repeats']} scenarios x {report['repeats']} repeats"
        f" = {g['n']} runs",
        "",
        "| Measure | Guarded (real) | Unguarded (baseline) |",
        "|---|---|---|",
        f"| Scenarios passed | {cell(g, 'pass')} | {cell(u, 'pass')} |",
        f"| Critical outcome reached (effective) | {cell(g, 'critical_effective')} | {cell(u, 'critical_effective')} |",
        "| Model asked for a critical violation (attempted) | "
        f"{cell(g, 'critical_attempted')} | {cell(u, 'critical_attempted')} |",
        f"| Answers withheld by the numeric guard | {g['withheld']} | {u['withheld'] if u else '-'} |",
        f"| Tool calls denied by policy | {g['denied_tool_calls']} | {u['denied_tool_calls'] if u else '-'} |",
        f"| Avg latency / cost per task | {g['avg_latency_ms']} ms / ${g['avg_cost_usd']} | "
        + (f"{u['avg_latency_ms']} ms / ${u['avg_cost_usd']} |" if u else "- |"),
        "",
        "| Violation kind | Guarded attempted / effective | Unguarded attempted / effective |",
        "|---|---|---|",
    ]
    for k in ALL_KINDS:
        gk, uk = g["by_kind"][k], (u["by_kind"][k] if u else None)
        lines.append(
            f"| {k} | {gk['attempted']} / {gk['effective']} | "
            + (f"{uk['attempted']} / {uk['effective']} |" if uk else "- |")
        )
    lines += ["", "| Category | Guarded pass | Unguarded pass |", "|---|---|---|"]
    for cat, c in g["per_category"].items():
        uc = (u or {}).get("per_category", {}).get(cat)
        lines.append(f"| {cat} | {c['passed']}/{c['n']} | " + (f"{uc['passed']}/{uc['n']} |" if uc else "- |"))
    if g["failures"]:
        lines += ["", "Guarded failures:"] + [f"- {f['id']}: {'; '.join(f['why'])}" for f in g["failures"]]
    return "\n".join(lines)


def gate(report: dict[str, Any], thresholds: dict[str, Any] | None = None) -> list[str]:
    t = thresholds or json.loads(THRESHOLDS.read_text())
    g = report["summary"]["guarded"]
    problems = []
    if g["critical_effective"]["k"] > t["max_critical_effective_guarded"]:
        problems.append(f"{g['critical_effective']['k']} scenarios reached a critical outcome with guards on")
    if (g["pass"]["rate"] or 0) < t["min_pass_rate_guarded"]:
        problems.append(f"guarded pass rate {_pct(g['pass']['rate'])} is below {_pct(t['min_pass_rate_guarded'])}")
    if g["errors"]:
        problems.append(f"{g['errors']} runs errored")
    return problems


def run_suite(
    llm: Any,
    scenarios: list[dict[str, Any]],
    *,
    modes: list[str],
    repeats: int,
    settings: Settings,
    audit: AuditLog | None,
    pause: float = 0.0,
    log: Any = print,
) -> dict[str, list[Scored]]:
    out: dict[str, list[Scored]] = {m: [] for m in modes}
    for sc in scenarios:
        for mode in modes:
            for rep in range(repeats):
                ledger = Ledger()  # fresh every run: a refund in one scenario must not leak into the next
                agent = AssuranceAgent(
                    llm, ledger, guarded=(mode == "guarded"), audit=audit if mode == "guarded" else None
                )
                traj = agent.run(sc["prompt"], sc.get("merchant", "m_acme"), run_id=f"{sc['id']}-{mode[0]}{rep}")
                cost = settings.cost_usd(traj.input_tokens, traj.output_tokens, traj.model or None)
                s = score(traj, sc, ledger, cost)
                out[mode].append(s)
                log(f"  {sc['id']:<12} {mode:<9} {'PASS' if s.passed else 'FAIL'}  {'; '.join(s.failed_checks)[:110]}")
                if pause:
                    time.sleep(pause)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m acp.assure.evaluate")
    ap.add_argument("--model", default=None, help="Bedrock model id (regional prefix added if missing)")
    ap.add_argument("--modes", nargs="+", default=["guarded", "unguarded"], choices=["guarded", "unguarded"])
    ap.add_argument("--only", default=None, help="comma-separated scenario ids or categories")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--label", default="latest")
    ap.add_argument("--pause", type=float, default=0.0)
    ap.add_argument("--gate", action="store_true", help="exit 1 when evals/assure/thresholds.json is violated")
    ap.add_argument("--db", default=None, help="SQLite file for the audit log (default: a fresh file under results/)")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.model:
        settings = replace(settings, model=resolve_model(args.model, settings.aws_region))
    from acp.llm import BedrockLLM

    llm = BedrockLLM(settings)
    scenarios = load_scenarios()
    if args.only:
        want = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in want or s["category"] in want]

    RESULTS.mkdir(exist_ok=True)
    db = Path(args.db) if args.db else RESULTS / f"assure-{args.label}-audit.db"
    if not args.db and db.exists():
        db.unlink()
    conn = store.connect(db)
    audit = AuditLog(conn)

    print(f"Running {len(scenarios)} scenarios x {args.modes} x {args.repeats} on {settings.model}")
    rows = run_suite(
        llm, scenarios, modes=args.modes, repeats=args.repeats, settings=settings, audit=audit, pause=args.pause
    )
    chain = audit.verify()
    report = {
        "label": args.label,
        "model": settings.model,
        "repeats": args.repeats,
        "summary": {m: summarise(r) for m, r in rows.items() if r},
        "audit": {"rows": chain.rows, "chain_ok": chain.ok, "head": audit.head()},
        "results": {m: [r.to_dict() for r in rs] for m, rs in rows.items()},
    }
    (RESULTS / f"assure-{args.label}.json").write_text(json.dumps(report, indent=1, default=str))
    print()
    print(render(report))
    print(f"\nAudit log: {chain.rows} entries, chain {'intact' if chain.ok else 'BROKEN'}, head {audit.head()[:16]}...")
    if args.gate:
        problems = gate(report)
        print("GATE: " + ("FAIL - " + "; ".join(problems) if problems else "PASS"))
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
