"""Fail (exit 1) if a live sync shows the guardrails regressing. Run by the weekly workflow after `acp.assure.live`.

For every data source in the sync it applies the same thresholds as the offline benchmark gate
(evals/assure/thresholds.json): no guarded critical outcome, a minimum guarded pass rate, no errored runs. It also fails
if the sync stopped early on its spend cap, because a partial result is not evidence.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acp.assure.evaluate import gate  # noqa: E402


def check(payload: dict, thresholds: dict | None = None) -> list[str]:
    latest = payload["latest"]
    problems = []
    if latest.get("aborted"):
        problems.append("the sync stopped early on its spend cap, so the result is partial")
    for name, src in latest["sources"].items():
        for p in gate({"summary": src["summary"]}, thresholds):
            problems.append(f"{name}: {p}")
    return problems


def main(argv: list[str] | None = None) -> int:
    path = Path((argv or sys.argv[1:] or ["results/live-latest.json"])[0])
    problems = check(json.loads(path.read_text()))
    if problems:
        print("Guardrail regression in the live sync:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("Live sync passes the guardrail thresholds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
