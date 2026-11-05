"""The approvals queue: refunds the agent drafted wait here for a person to approve or reject them.

Analogy: the "to be signed" tray. The agent can only ever put a form in the tray. A person with the right role signs
or declines it on the dashboard. The worker then copies each signature into the tamper-evident audit log, so the
decision is part of the same chain as the agent's own actions.

Keys (all JSON, 7-day expiry). Three keys per approval so the two writers never overwrite each other:
  acp:live:approvals              newest-first approval ids (capped at 100)
  acp:live:approval:<id>          the request, written once by the worker
  acp:live:approval:<id>:decision the person's decision, written once by the dashboard (SET NX: the first decision wins)
  acp:live:approval:<id>:audit    the audit-log entry hash, written by the worker after it records the decision

No money moves here. The ledger behind the demo is a sample, so approving records a decision and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from acp.assure import alerts
from acp.assure.audit import AuditLog, redact_deep
from acp.assure.publish import PREFIX, UpstashClient

KEY_LIST = PREFIX + "approvals"
LIST_CAP = 100
TTL_S = 7 * 24 * 3600


def key_record(aid: str) -> str:
    return f"{PREFIX}approval:{aid}"


def key_decision(aid: str) -> str:
    return f"{PREFIX}approval:{aid}:decision"


def key_audit(aid: str) -> str:
    return f"{PREFIX}approval:{aid}:audit"


def approval_id(source_id: str, draft: dict[str, Any]) -> str:
    basis = f"{source_id}|{draft.get('idempotency_key') or draft.get('payment_id')}|{draft.get('amount_paise')}"
    return "apr_" + hashlib.sha256(basis.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def publish_drafts(
    client: UpstashClient, source: dict[str, str], tenant: str, drafts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Queue each draft refund for a person. Returns the requests that are new (a re-run adds nothing twice)."""
    created: list[dict[str, Any]] = []
    for d in drafts:
        aid = approval_id(source["id"], d)
        rec = redact_deep(
            {
                "id": aid,
                "tenant": tenant,
                "payment_id": d.get("payment_id"),
                "amount_paise": d.get("amount_paise"),
                "reason": str(d.get("reason", ""))[:200],
                "tier": d.get("approval_tier", "standard"),
                "source": {"kind": source["kind"], "id": source["id"], "title": source.get("title", "")[:80]},
                "created_at": _now(),
            }
        )
        stored = client.pipeline([["SET", key_record(aid), json.dumps(rec), "NX", "EX", TTL_S]])[0]
        if stored == "OK":
            client.pipeline(
                [["LPUSH", KEY_LIST, aid], ["LTRIM", KEY_LIST, 0, LIST_CAP - 1], ["EXPIRE", KEY_LIST, TTL_S]]
            )
            created.append(rec)
    return created


def sync_decisions(client: UpstashClient, audit: AuditLog | None) -> int:
    """Copy every decision the dashboard recorded into the audit chain, once. Returns how many were recorded."""
    if audit is None:
        return 0
    ids = [i for i in (client.pipeline([["LRANGE", KEY_LIST, 0, LIST_CAP - 1]])[0] or []) if isinstance(i, str)]
    if not ids:
        return 0
    cmds = [["MGET", *(f(i) for i in ids)] for f in (key_record, key_decision, key_audit)]
    records, decisions, audits = client.pipeline(cmds)
    done = 0
    for aid, raw_rec, raw_dec, raw_aud in zip(ids, records, decisions, audits, strict=True):
        if not (raw_rec and raw_dec) or raw_aud:
            continue
        try:
            rec, dec = json.loads(raw_rec), json.loads(raw_dec)
        except ValueError:
            continue
        h = audit.append(
            str(dec.get("by", "unknown"))[:80],
            "approval_decided",
            {
                "approval_id": aid,
                "decision": dec.get("decision"),
                "tier": rec.get("tier"),
                "payment_id": rec.get("payment_id"),
                "amount_paise": rec.get("amount_paise"),
                "note": str(dec.get("note", ""))[:300],
                "decided_at": dec.get("at"),
            },
            aid,
        )
        client.pipeline([["SET", key_audit(aid), json.dumps({"hash": h, "at": _now()}), "EX", TTL_S]])
        done += 1
    return done


def read_all(client: UpstashClient) -> list[dict[str, Any]]:
    """Every approval with its decision and audit state merged in, newest first (used by the report)."""
    ids = [i for i in (client.pipeline([["LRANGE", KEY_LIST, 0, LIST_CAP - 1]])[0] or []) if isinstance(i, str)]
    if not ids:
        return []
    cmds = [["MGET", *(f(i) for i in ids)] for f in (key_record, key_decision, key_audit)]
    records, decisions, audits = client.pipeline(cmds)
    out = []
    for raw_rec, raw_dec, raw_aud in zip(records, decisions, audits, strict=True):
        if not raw_rec:
            continue
        try:
            rec = json.loads(raw_rec)
            dec = json.loads(raw_dec) if raw_dec else None
            aud = json.loads(raw_aud) if raw_aud else None
        except ValueError:
            continue
        out.append({**rec, "state": dec["decision"] if dec else "pending", "decision": dec, "audit": aud})
    return out


def queue_and_alert(
    client: UpstashClient,
    source: dict[str, str],
    tenant: str,
    drafts: list[dict[str, Any]],
    blocked: list[str],
    *,
    post: alerts.Post = alerts._urllib_post,
    log: Callable[[str], None] = print,
) -> int:
    """After a guarded run: queue its draft refunds for approval, then alert on new approvals and blocked attempts."""
    where = os.environ.get("DASHBOARD_URL") or None
    new = publish_drafts(client, source, tenant, drafts)
    for rec in new:
        alerts.send_alert(client, alerts.approval_text(rec, where), post=post, log=log)
    if blocked:
        alerts.send_alert(client, alerts.block_text(source["kind"], source["id"], blocked, where), post=post, log=log)
    return len(new)
