"""Publish a sync's results to Upstash Redis so the deployed dashboard can show them without a redeploy.

Analogy: the noticeboard. The sync job pins fresh results to it; the dashboard function only ever reads it, with a
read-only key. Everything pinned has been through the card/ID masker first.

Keys (all JSON, all refreshed with a 120-day expiry so an abandoned deployment does not keep data forever):
  acp:live:latest    the newest sync's summary, provenance and per-source results
  acp:live:runs      newest-first list of individual runs (capped at 500)
  acp:live:history   newest-first list of one-line sync summaries for the trend chart (capped at 200)
  acp:live:snapshot  the sanitised description of the Razorpay snapshot (counts and hash, never payment contents)
  acp:live:audit     the audit chain's rows (hashes only), head and verification result

No third-party dependency: plain `urllib` against Upstash's REST API. Write credentials come from the environment of
whoever runs the sync; the dashboard must be given the read-only token instead (see docs/LIVE_DATA.md).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from acp.assure.audit import redact_deep
from acp.assure.scoring import CRITICAL, Scored

PREFIX = "acp:live:"
KEY_LATEST, KEY_RUNS, KEY_HISTORY = PREFIX + "latest", PREFIX + "runs", PREFIX + "history"
KEY_SNAPSHOT, KEY_AUDIT = PREFIX + "snapshot", PREFIX + "audit"
RUNS_CAP, HISTORY_CAP = 500, 200
EXPIRE_S = 120 * 24 * 3600
MAX_REQUEST_BYTES = 600_000  # Upstash limits request size; stay well under it
MAX_VALUE_BYTES = 400_000
ANSWER_MAX = 600

# (url, headers, body) -> (http status, response bytes)
Transport = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


class UpstashError(Exception):
    """A publish failed; the message never contains the token."""


def _urllib_transport(timeout: float = 20.0) -> Transport:
    def go(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https URL from the user's own config
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    return go


class UpstashClient:
    def __init__(self, url: str, token: str, transport: Transport | None = None) -> None:
        if not url.startswith("https://") or not token:
            raise UpstashError("Upstash needs an https REST URL and a token.")
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._transport = transport or _urllib_transport()

    def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        """Run commands in order (several requests if the batch is large). Returns each command's result."""
        results: list[Any] = []
        batch: list[list[Any]] = []
        size = 0
        for cmd in commands:
            n = len(json.dumps(cmd))
            if batch and size + n > MAX_REQUEST_BYTES:
                results += self._send(batch)
                batch, size = [], 0
            batch.append(cmd)
            size += n
        if batch:
            results += self._send(batch)
        return results

    def _send(self, batch: list[list[Any]]) -> list[Any]:
        status, body = self._transport(self._url + "/pipeline", self._headers, json.dumps(batch).encode())
        if status != 200:
            raise UpstashError(f"Upstash returned HTTP {status} (check the REST URL and token).")
        try:
            items = json.loads(body.decode())
        except ValueError as e:
            raise UpstashError("Upstash returned a non-JSON response.") from e
        out = []
        for item in items:
            if isinstance(item, dict) and item.get("error"):
                raise UpstashError(f"Upstash rejected a command: {str(item['error'])[:120]}")
            out.append(item.get("result") if isinstance(item, dict) else item)
        return out

    def get_json(self, key: str) -> Any:
        raw = self.pipeline([["GET", key]])[0]
        return json.loads(raw) if raw else None


def client_from_env(env: dict[str, str] | None = None, transport: Transport | None = None) -> UpstashClient | None:
    """Write-capable client from the environment, or None when it is not configured. Never uses a read-only token."""
    e = env if env is not None else os.environ
    url = e.get("UPSTASH_REDIS_REST_URL") or e.get("KV_REST_API_URL")
    token = e.get("UPSTASH_REDIS_REST_TOKEN") or e.get("KV_REST_API_TOKEN")
    return UpstashClient(url, token, transport) if url and token else None


# ------------------------------------------------------------------ records
def _true_keys(d: dict[str, bool]) -> list[str]:
    return [k for k, v in d.items() if v]


def run_record(s: Scored) -> dict[str, Any]:
    """One run, trimmed to what the dashboard shows. Already-masked fields only."""
    return {
        "run_id": s.run_id,
        "started_at": s.started_at,
        "source": s.ledger_source,
        "mode": s.mode,
        "scenario_id": s.scenario_id,
        "category": s.category,
        "passed": s.passed,
        "failed_checks": s.failed_checks,
        "attempted": _true_keys(s.attempted),
        "effective": _true_keys(s.effective),
        "critical": any(s.effective.get(k) for k in CRITICAL),
        "withheld": s.withheld,
        "tool_calls": s.tool_calls,
        "denied_calls": s.denied_calls,
        "latency_ms": round(s.latency_ms),
        "cost_usd": round(s.cost_usd, 6),
        "model": s.model,
        "answer": s.answer[:ANSWER_MAX],
        "pii_found": s.pii_found,
        "unsupported_amounts": s.unsupported_amounts,
        "events": s.events,
        "error": s.error,
    }


def _history_line(finished_at: str, model: str, summaries: dict[str, dict[str, Any]], cost: float) -> dict[str, Any]:
    line: dict[str, Any] = {"finished_at": finished_at, "model": model, "cost_usd": round(cost, 6), "sources": {}}
    for src, by_mode in summaries.items():
        g, u = by_mode["guarded"], by_mode.get("unguarded")
        line["sources"][src] = {
            "n": g["n"],
            "guarded_pass": g["pass"]["rate"],
            "guarded_critical": g["critical_effective"]["k"],
            "unguarded_pass": u["pass"]["rate"] if u else None,
            "unguarded_critical": u["critical_effective"]["k"] if u else None,
        }
    return line


PROVENANCE = [
    "Data: Razorpay TEST mode. Real API shapes and test payments; no real money and no real customers.",
    "Tenancy is emulated: a Razorpay account is one merchant, so payments are tagged with a tenant in their notes.",
    "The agent's refund requests are recorded in memory only. Nothing is ever sent to Razorpay.",
    "Decisions are made by a real model on Amazon Bedrock; the policy, masking and numeric checks are ordinary code.",
    "Capture is periodic (a scheduled or manual sync), not a stream of production traffic.",
    "Small samples: intervals are wide and repeats of a scenario are not independent.",
]


def build_payload(
    *,
    results: dict[str, dict[str, list[Scored]]],
    summaries: dict[str, dict[str, Any]],
    model: str,
    started_at: str,
    finished_at: str,
    repeats: int,
    cost_usd: float,
    aborted: bool,
    snapshot: dict[str, Any] | None,
    audit: dict[str, Any],
    expected_interval_hours: int = 168,
) -> dict[str, Any]:
    """Everything one sync publishes, as plain data. `results[source][mode]` are the scored runs."""
    runs = sorted(
        (run_record(s) for by_mode in results.values() for rows in by_mode.values() for s in rows),
        key=lambda r: (r["started_at"], r["run_id"]),
    )
    latest = {
        "schema": 1,
        "model": model,
        "started_at": started_at,
        "finished_at": finished_at,
        "expected_interval_hours": expected_interval_hours,
        "repeats": repeats,
        "cost_usd": round(cost_usd, 6),
        "aborted": aborted,
        "sources": {src: {"summary": s} for src, s in summaries.items()},
        "snapshot": snapshot,
        "provenance": PROVENANCE,
    }
    return redact_deep(
        {
            "latest": latest,
            "runs": runs,
            "history": _history_line(finished_at, model, summaries, cost_usd),
            "snapshot": snapshot,
            "audit": audit,
        }
    )


def _dump(obj: Any, key: str) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) > MAX_VALUE_BYTES:
        raise UpstashError(f"{key} is {len(text.encode())} bytes, over the {MAX_VALUE_BYTES} byte limit.")
    return text


def commands_for(payload: dict[str, Any]) -> list[list[Any]]:
    """The exact Redis commands a publish would send (also used for dry runs and tests)."""
    cmds: list[list[Any]] = [
        ["SET", KEY_LATEST, _dump(payload["latest"], KEY_LATEST), "EX", EXPIRE_S],
        ["SET", KEY_AUDIT, _dump(payload["audit"], KEY_AUDIT), "EX", EXPIRE_S],
    ]
    if payload.get("snapshot"):
        cmds.append(["SET", KEY_SNAPSHOT, _dump(payload["snapshot"], KEY_SNAPSHOT), "EX", EXPIRE_S])
    runs = [_dump(r, KEY_RUNS) for r in payload["runs"]]
    for i in range(0, len(runs), 25):  # oldest first, so the newest ends up at the head of the list
        cmds.append(["LPUSH", KEY_RUNS, *runs[i : i + 25]])
    cmds += [["LTRIM", KEY_RUNS, 0, RUNS_CAP - 1], ["EXPIRE", KEY_RUNS, EXPIRE_S]]
    cmds += [
        ["LPUSH", KEY_HISTORY, _dump(payload["history"], KEY_HISTORY)],
        ["LTRIM", KEY_HISTORY, 0, HISTORY_CAP - 1],
        ["EXPIRE", KEY_HISTORY, EXPIRE_S],
    ]
    return cmds


def publish(client: UpstashClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Write a sync, then read the newest summary back to prove it landed. Returns a small receipt."""
    client.pipeline(commands_for(payload))
    back = client.get_json(KEY_LATEST) or {}
    ok = back.get("finished_at") == payload["latest"]["finished_at"]
    if not ok:
        raise UpstashError("Published, but reading acp:live:latest back did not return the new sync.")
    return {
        "published_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs": len(payload["runs"]),
        "verified": ok,
    }
