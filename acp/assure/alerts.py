"""Alerts: tell a person when the guardrails block something or an approval is waiting.

Analogy: the doorbell. The worker presses it; what rings (Slack, or anything that accepts a JSON POST) is up to you.

Set ALERT_WEBHOOK_URL to a Slack "incoming webhook" URL (or any https endpoint that accepts {"text": "..."}). With it
unset, nothing is sent. Email is not built in: most teams already route Slack or webhook alerts to their phone.

Safety: https only; at most MAX_PER_HOUR alerts an hour (counted in Redis, so it holds across worker restarts); the
text is masked for card and identity data and carries ids and amounts only, never customer notes; a failure to send
is logged and never stops the worker.
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
from acp.assure.publish import PREFIX, UpstashClient

MAX_PER_HOUR = 20
Post = Callable[[str, bytes], int]  # (url, body) -> http status


def _urllib_post(url: str, body: bytes) -> int:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 - https URL the operator configured
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def webhook_url(env: dict[str, str] | None = None) -> str | None:
    url = (env if env is not None else os.environ).get("ALERT_WEBHOOK_URL", "").strip()
    return url if url.startswith("https://") else None


def send_alert(
    client: UpstashClient | None,
    text: str,
    *,
    env: dict[str, str] | None = None,
    post: Post = _urllib_post,
    log: Callable[[str], None] = print,
) -> bool:
    """Send one alert. Returns True only if the endpoint accepted it. Never raises."""
    url = webhook_url(env)
    if url is None:
        return False
    try:
        if client is not None:
            hour = datetime.now(UTC).strftime("%Y%m%d%H")
            key = f"{PREFIX}alert:rate:{hour}"
            n = client.pipeline([["INCR", key], ["EXPIRE", key, 7200]])[0]
            if int(n) > MAX_PER_HOUR:
                log(f"alert skipped: more than {MAX_PER_HOUR} an hour")
                return False
        body = json.dumps({"text": redact_deep(text)[:1500]}).encode()
        status = post(url, body)
        if not 200 <= status < 300:
            log(f"alert endpoint answered HTTP {status}")
            return False
        return True
    except Exception as e:  # noqa: BLE001 - an alert problem must never stop the worker
        log(f"alert failed: {type(e).__name__}")
        return False


def _inr(paise: Any) -> str:
    try:
        return f"₹{int(paise) / 100:,.2f}"
    except (TypeError, ValueError):
        return "an amount"


def approval_text(rec: dict[str, Any], dashboard: str | None = None) -> str:
    link = f"\nReview: {dashboard.rstrip('/')}/app/approvals" if dashboard else ""
    return (
        f"Approval needed ({rec.get('tier', 'standard')}): refund {_inr(rec.get('amount_paise'))} on "
        f"{rec.get('payment_id')} for {rec.get('tenant')}. Source: {rec.get('source', {}).get('kind')} "
        f"{rec.get('source', {}).get('id')}.{link}"
    )


def block_text(where: str, ref: str, codes: list[str], dashboard: str | None = None) -> str:
    link = f"\nSee: {dashboard.rstrip('/')}/app" if dashboard else ""
    return f"Guardrails blocked {len(codes)} attempt(s) in {where} {ref}: {', '.join(sorted(set(codes)))}.{link}"
