"""Regression gate: exit 1 if eval scores fall below thresholds (use in CI).

Analogy: the bouncer at the door of a release. Reads results/eval_report.json
and evals/thresholds.json; any metric outside its limit blocks the release.
Usage: python scripts/check_regression.py [report.json] [thresholds.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check(summary: dict, thresholds: dict) -> list[str]:
    """Return a list of human-readable violations (empty = gate passes)."""
    problems = []
    for metric, rule in thresholds.items():
        value = summary.get(metric)
        if value is None:
            problems.append(f"{metric}: missing from report")
            continue
        if "min" in rule and value < rule["min"]:
            problems.append(f"{metric}={value} is below minimum {rule['min']}")
        if "max" in rule and value > rule["max"]:
            problems.append(f"{metric}={value} is above maximum {rule['max']}")
    return problems


def main(argv: list[str]) -> int:
    report_path = Path(argv[1]) if len(argv) > 1 else ROOT / "results" / "eval_report.json"
    thr_path = Path(argv[2]) if len(argv) > 2 else ROOT / "evals" / "thresholds.json"
    if not report_path.is_file():
        print(f"error: no report at {report_path}; run `make eval` first", file=sys.stderr)
        return 2
    summary = json.loads(report_path.read_text())["summary"]
    problems = check(summary, json.loads(thr_path.read_text()))
    for p in problems:
        print(f"FAIL {p}")
    print("regression gate: " + ("FAILED" if problems else "passed"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
