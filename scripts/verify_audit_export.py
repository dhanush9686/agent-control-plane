#!/usr/bin/env python3
"""Check an exported audit report without trusting the system that made it.

    python scripts/verify_audit_export.py report.json

Uses only the Python standard library and shares no code with the rest of the project, so a reviewer can read it in
two minutes and run it on a machine that has never seen the application.

For every entry it recomputes  SHA-256( previous_hash + "|" + canonical_json({ts, actor, action, run_id, detail }) )
and checks that it equals the entry's hash and that each entry's previous_hash is the hash before it. The first
entry must follow the report's stated anchor. Finally the last hash must equal the stated head when the report lists
the whole tail of the chain.

Exit status 0 means the entries are internally consistent; 1 means a mismatch (the entry is named).
This proves the export was not edited after it was made. It cannot prove the system's own log was never rewritten
before export: for that, compare the head hash with a copy you recorded earlier somewhere the system cannot write.
"""

from __future__ import annotations

import hashlib
import json
import sys


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(prev: str, e: dict) -> str:
    body = canonical(
        {"ts": e["ts"], "actor": e["actor"], "action": e["action"], "run_id": e["run_id"], "detail": e["detail"]}
    )
    return hashlib.sha256(f"{prev}|{body}".encode()).hexdigest()


def verify(report: dict) -> tuple[bool, str]:
    entries = report.get("entries", [])
    if not entries:
        return True, "no entries listed; nothing to check"
    prev = report["integrity"]["anchor_prev_hash"]
    for e in entries:
        if e["prev_hash"] != prev:
            return False, f"entry {e['seq']}: does not follow the entry before it (removed or reordered)"
        if digest(prev, e) != e["hash"]:
            return False, f"entry {e['seq']}: content does not match its hash (edited)"
        prev = e["hash"]
    total, listed = report["scope"]["entries_total"], report["scope"]["entries_listed"]
    if listed == total or entries[-1]["seq"] == total:
        if prev != report["integrity"]["head"]:
            return False, "the last entry's hash is not the stated head"
    return True, f"{len(entries)} entries consistent; head {prev}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as f:
        ok, msg = verify(json.load(f))
    print(("OK: " if ok else "BROKEN: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
