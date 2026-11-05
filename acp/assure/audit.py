"""Tamper-evident audit log: each row's hash covers the previous row's hash, so editing, deleting or reordering any
past row breaks every hash after it.

Analogy: a numbered ledger book where each page carries a fingerprint of the page before. Tear one out or rewrite one
and the fingerprints stop matching from that page on.

Honest limit: this is tamper-*evident*, not tamper-proof. Someone who can rewrite the whole table can rebuild a
consistent chain. The production step is to publish the head hash somewhere the database admin cannot write (a
write-once bucket, a separate account, a timestamping service); `head()` returns the value to anchor.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from acp import store
from acp.assure.pii import redact

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _redact_deep(obj: Any) -> Any:
    """Mask card/identity data in every string of a nested value before it is written to the log."""
    if isinstance(obj, str):
        return redact(obj)[0]
    if isinstance(obj, dict):
        return {k: _redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_deep(v) for v in obj]
    return obj


def _digest(prev_hash: str, ts: str, actor: str, action: str, run_id: str | None, detail: str) -> str:
    body = _canonical({"ts": ts, "actor": actor, "action": action, "run_id": run_id, "detail": detail})
    return hashlib.sha256(f"{prev_hash}|{body}".encode()).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    rows: int
    first_bad_seq: int | None = None
    reason: str = ""


class AuditLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, actor: str, action: str, detail: dict[str, Any], run_id: str | None = None) -> str:
        """Add one entry; returns its hash. The read of the previous hash and the insert share one write lock."""
        ts = datetime.now(UTC).isoformat(timespec="microseconds")
        body = _canonical(_redact_deep(detail))
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            h = _digest(prev, ts, actor, action, run_id, body)
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, run_id, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ts, actor, action, run_id, body, prev, h),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return h

    def head(self) -> str:
        row = self._conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        return row["hash"] if row else GENESIS

    def entries(self, run_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM audit_log" + (" WHERE run_id = ?" if run_id else "") + " ORDER BY seq"
        return [dict(r) for r in self._conn.execute(q, (run_id,) if run_id else ())]

    def verify(self, expected_head: str | None = None) -> VerifyResult:
        """Walk the chain. Pass the externally anchored head to also detect truncation of the newest rows."""
        prev, n = GENESIS, 0
        rows = self._conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        for r in rows:
            n += 1
            if r["prev_hash"] != prev:
                return VerifyResult(False, n, r["seq"], "chain link broken (a row was removed or reordered)")
            if _digest(prev, r["ts"], r["actor"], r["action"], r["run_id"], r["detail"]) != r["hash"]:
                return VerifyResult(False, n, r["seq"], "row content does not match its hash (edited)")
            prev = r["hash"]
        if expected_head is not None and prev != expected_head:
            return VerifyResult(False, n, None, "head differs from the anchored value (rows added or truncated)")
        return VerifyResult(True, n)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from acp.config import Settings

    ap = argparse.ArgumentParser(prog="python -m acp.assure.audit", description="Verify the audit hash chain.")
    ap.add_argument("command", choices=["verify", "head"])
    ap.add_argument("--db", default=None)
    ap.add_argument("--expected-head", default=None)
    args = ap.parse_args(argv)
    conn = store.connect(args.db or Settings().db_path)
    log = AuditLog(conn)
    if args.command == "head":
        print(log.head())
        return 0
    res = log.verify(args.expected_head)
    print(
        f"audit chain: {'OK' if res.ok else 'BROKEN'} ({res.rows} rows)"
        + ("" if res.ok else f" - {res.reason}, seq {res.first_bad_seq}")
    )
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
