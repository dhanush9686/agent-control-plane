"""The event worker: take a Razorpay test-mode webhook event off the queue, run the agent on it, publish each step.

Analogy: the night-shift clerk. The webhook function (on Vercel) only checks the delivery's signature and drops a slip
in the in-tray. This worker picks up one slip at a time, handles it with the same guarded agent as everywhere else, and
pins a progress note to the noticeboard after every step so the dashboard can show it happening.

Keys (all JSON, 7-day expiry):
  acp:live:queue          event ids waiting for the worker (webhook LPUSHes, worker RPOPs, so oldest first)
  acp:live:events         newest-first event ids for the dashboard (capped at 100)
  acp:live:event:<id>     one event: state, whitelisted payment, steps, runs, verdict, cost
  acp:live:spend:<date>   dollars spent today across events (the daily cap lives here, so it survives restarts)

No money moves and nothing is sent to Razorpay: the payment comes from the webhook payload, the ledger is in memory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from acp import store
from acp.assure import approvals, report
from acp.assure.agent import AssuranceAgent
from acp.assure.audit import AuditLog, redact_deep
from acp.assure.evaluate import resolve_model
from acp.assure.publish import PREFIX, UpstashClient, UpstashError, client_from_env, run_record
from acp.assure.razorpay import RazorpayTestLedger, snapshot_from_webhook, snapshot_hash
from acp.assure.scenario_gen import scenarios_for_payment
from acp.assure.scoring import score
from acp.assure.tools import ToolEvent
from acp.config import ROOT, Settings

KEY_QUEUE, KEY_EVENTS = PREFIX + "queue", PREFIX + "events"
EVENT_TTL_S = 7 * 24 * 3600
HOUSEKEEPING_EVERY_S = 30  # while polling: how often to record approval decisions and refresh the report

# A fixed fixture used only to check the agent cannot read another merchant's payment. It is not Razorpay data.
PROBE_ID = "pay_ProbeOther001"
OTHER_TENANT = {"m_acme": "m_zen", "m_zen": "m_acme"}


def key_event(event_id: str) -> str:
    return f"{PREFIX}event:{event_id}"


def key_spend(day: str) -> str:
    return f"{PREFIX}spend:{day}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ledger_for_event(payment: dict[str, Any]) -> tuple[RazorpayTestLedger, str] | None:
    """In-memory ledger with the event's payment plus the other-tenant probe fixture."""
    snap = snapshot_from_webhook(payment)
    if snap is None:
        return None
    mine = snap["payments"][0]
    other = OTHER_TENANT.get(mine["tenant"], "m_zen")
    snap["payments"].append(
        {
            "id": PROBE_ID,
            "tenant": other,
            "amount_paise": 99_900,
            "status": "captured",
            "method": "upi",
            "created": mine["created"],
            "failure_code": None,
            "refunded_paise": 0,
            "note": "",
        }  # fmt: skip
    )
    snap["sha256"] = snapshot_hash(snap)
    return RazorpayTestLedger(snap), mine["id"]


class EventRecord:
    """One event's record in Redis. Every change is written straight away, redacted, with the expiry refreshed."""

    def __init__(self, client: UpstashClient, event_id: str, data: dict[str, Any], key: str | None = None) -> None:
        self.client, self.id, self.data = client, event_id, data
        self.key = key or key_event(event_id)

    def save(self) -> None:
        body = json.dumps(redact_deep(self.data), ensure_ascii=False)
        self.client.pipeline([["SET", self.key, body, "EX", EVENT_TTL_S]])

    def step(self, text: str, tone: str = "neutral", **extra: Any) -> None:
        self.data.setdefault("steps", []).append({"t": _now(), "text": text, "tone": tone, **extra})
        self.save()


def describe(ev: ToolEvent) -> tuple[str, str]:
    args = ", ".join(f"{k}={v}" for k, v in ev.args.items())
    if not ev.allowed:
        return f"{ev.name}({args}): blocked, {ev.code}", "good"
    if ev.effect == "executed":
        return f"{ev.name}({args}): money moved without approval", "bad"
    if ev.effect == "draft":
        return f"{ev.name}({args}): draft, waits for a human", "warn"
    return f"{ev.name}({args}): allowed", "neutral"


def spent_today(client: UpstashClient, day: str) -> float:
    raw = client.pipeline([["GET", key_spend(day)]])[0]
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def add_spend(client: UpstashClient, day: str, usd: float) -> None:
    client.pipeline([["INCRBYFLOAT", key_spend(day), f"{usd:.6f}"], ["EXPIRE", key_spend(day), 3 * 24 * 3600]])


def process_event(
    client: UpstashClient,
    llm: Any,
    settings: Any,
    event_id: str,
    audit: AuditLog | None,
    *,
    daily_cap_usd: float = 0.25,
    log: Callable[[str], None] = print,
) -> str:
    """Run one queued event to completion. Returns the final state: done, skipped or failed. Never raises."""
    raw = client.pipeline([["GET", key_event(event_id)]])[0]
    if not raw:
        log(f"{event_id}: record missing or expired, dropped")
        return "failed"
    rec = EventRecord(client, event_id, json.loads(raw))
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        if spent_today(client, day) >= daily_cap_usd:
            rec.data["state"] = "skipped"
            rec.step(f"Skipped: today's ${daily_cap_usd:.2f} spend cap is used up", "warn")
            return "skipped"
        built = ledger_for_event(rec.data.get("payment") or {})
        if built is None:
            rec.data["state"] = "failed"
            rec.step("Could not read a payment from this event", "bad")
            return "failed"
        ledger, pid = built
        probe = PROBE_ID if ledger.payment(PROBE_ID) else None
        scenarios = scenarios_for_payment(ledger, pid, probe)
        rec.data.update(state="running", started_at=_now(), runs=[], cost_usd=0.0)
        rec.step("Agent picked the event up", "neutral")
        total = 0.0
        drafts: list[dict[str, Any]] = []
        for sc in scenarios:
            rec.step(f"Check: {sc['prompt']}", "neutral", scenario=sc["id"])
            runs: list[dict[str, Any]] = []
            for mode in ("guarded", "unguarded"):
                fresh, _ = ledger_for_event(rec.data["payment"])  # type: ignore[misc]
                stream = (
                    (lambda e: rec.step(*describe(e), tool=e.name, allowed=e.allowed)) if mode == "guarded" else None
                )
                agent = AssuranceAgent(
                    llm, fresh, guarded=(mode == "guarded"), audit=audit if mode == "guarded" else None, on_event=stream
                )
                traj = agent.run(sc["prompt"], sc["merchant"], run_id=f"{event_id[:24]}-{sc['id']}-{mode[0]}")
                cost = settings.cost_usd(traj.input_tokens, traj.output_tokens, traj.model or None)
                total += cost
                if mode == "guarded":
                    drafts += [e.payload for e in traj.events if e.effect == "draft"]
                s = score(traj, sc, fresh, cost)
                s.mode = mode
                runs.append(run_record(s))
            g, u = runs
            rec.data["runs"] += runs
            rec.data["cost_usd"] = round(total, 6)
            if g["effective"]:
                rec.step("With guardrails, a mistake got through: " + ", ".join(g["effective"]), "bad")
            elif g["attempted"]:
                rec.step("Attack attempted and stopped: " + ", ".join(g["attempted"]), "good")
            else:
                rec.step(
                    "Answered correctly" if g["passed"] else "Answer failed a check: " + "; ".join(g["failed_checks"]),
                    "good" if g["passed"] else "warn",
                )
            if u["effective"]:
                rec.step("Without guardrails this would have gone wrong: " + ", ".join(u["effective"]), "warn")
        add_spend(client, day, total)
        # The system's own cross-tenant probe runs on every event, so only other kinds of attempt are worth an alert.
        blocked = [
            k for r in rec.data["runs"] if r["mode"] == "guarded" for k in r["attempted"] if k != "cross_merchant"
        ]
        mine = ledger.payment(pid)
        approvals.queue_and_alert(
            client,
            {"kind": "event", "id": event_id, "title": pid},
            mine.merchant_id if mine else "",
            drafts,
            blocked,
            log=log,
        )
        guarded_runs = [r for r in rec.data["runs"] if r["mode"] == "guarded"]
        rec.data["verdict"] = {
            "checks": len(guarded_runs),
            "passed": sum(1 for r in guarded_runs if r["passed"]),
            "mistakes_with_guardrails": sum(1 for r in guarded_runs if r["effective"]),
            "mistakes_without": sum(1 for r in rec.data["runs"] if r["mode"] == "unguarded" and r["effective"]),
        }
        if audit:
            rec.data["audit_head"] = audit.head()
        rec.data.update(state="done", finished_at=_now())
        rec.step(f"Done. Cost ${total:.5f}", "neutral")
        return "done"
    except Exception as e:  # noqa: BLE001 - one bad event must not stop the worker
        rec.data["state"] = "failed"
        rec.data["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        try:
            rec.step("The worker hit an error and stopped this event", "bad")
        except UpstashError:
            pass
        return "failed"


def _housekeeping(client: UpstashClient, audit: AuditLog | None, log: Callable[[str], None]) -> None:
    """When idle: copy new approval decisions into the audit chain. Never raises."""
    try:
        n = approvals.sync_decisions(client, audit)
        if n:
            log(f"recorded {n} approval decision(s) in the audit chain")
        if audit is not None and report.publish_report(client, audit):
            log("published a fresh audit report")
    except Exception as e:  # noqa: BLE001 - housekeeping must not stop the worker
        log(f"approval sync failed: {type(e).__name__}")


def drain(
    client: UpstashClient, llm: Any, settings: Any, audit: AuditLog | None, *, once: bool, poll_s: float,
    daily_cap_usd: float, log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
) -> int:  # fmt: skip
    """Process both queues (webhook events first, then simulator jobs). With `once`, stop when both are empty."""
    from acp.assure.simulate import KEY_SIM_QUEUE, process_job

    handled, last_housekeeping = 0, float("-inf")
    while True:
        try:
            event_id = client.pipeline([["RPOP", KEY_QUEUE]])[0]
            job_id = None if event_id else client.pipeline([["RPOP", KEY_SIM_QUEUE]])[0]
        except UpstashError as e:
            log(f"Queue read failed: {e}")
            if once:
                return handled
            sleep(max(poll_s, 5))
            continue
        if not event_id and not job_id:
            if once or time.monotonic() - last_housekeeping >= HOUSEKEEPING_EVERY_S:
                _housekeeping(client, audit, log)
                last_housekeeping = time.monotonic()
            if once:
                return handled
            sleep(poll_s)
            continue
        if event_id:
            state = process_event(client, llm, settings, str(event_id), audit, daily_cap_usd=daily_cap_usd, log=log)
            log(f"{event_id}: {state}")
        else:
            state = process_job(client, lambda: llm, settings, str(job_id), audit, daily_cap_usd=daily_cap_usd, log=log)
            log(f"simulator job {job_id}: {state}")
        handled += 1


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    ap = argparse.ArgumentParser(prog="python -m acp.assure.ingest")
    ap.add_argument("--once", action="store_true", help="process what is queued, then exit")
    ap.add_argument("--poll-seconds", type=float, default=3.0)
    ap.add_argument(
        "--max-daily-usd", type=float, default=0.25, help="stop running agents once today's cost passes this"
    )
    ap.add_argument("--model", default=None)
    args = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")

    client = client_from_env()
    if client is None:
        print("Upstash is not configured: set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (or the KV_ names).")
        return 1
    from acp.llm import BedrockLLM

    settings = Settings()
    if args.model:
        settings = replace(settings, model=resolve_model(args.model, settings.aws_region))
    db = ROOT / "results" / "ingest-audit.db"  # persistent on purpose: the chain continues across events
    db.parent.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(store.connect(db))
    mode = "once" if args.once else f"polling every {args.poll_seconds:.0f}s"
    print(f"Worker ready ({mode}); daily cap ${args.max_daily_usd:.2f}.")
    try:
        n = drain(
            client,
            BedrockLLM(settings),
            settings,
            audit,
            once=args.once,
            poll_s=args.poll_seconds,
            daily_cap_usd=args.max_daily_usd,
        )
    except KeyboardInterrupt:
        print("Stopped.")
        return 0
    print(f"Handled {n} event(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
