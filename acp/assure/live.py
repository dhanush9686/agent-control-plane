"""One live sync: read the Razorpay test account, run the agent suite on it, and publish the results.

    python -m acp.assure.live                 # Razorpay test data + the synthetic suite, publish to Upstash
    python -m acp.assure.live --dry-run       # do everything except publish; writes results/live-latest.json
    python -m acp.assure.live --no-razorpay   # synthetic suite only (no Razorpay keys needed)

Order of work: snapshot (read-only) -> generate scenarios from it -> run every scenario with guards on and off on a
fresh ledger each time -> verify the audit chain -> build one payload -> publish and read it back.
A spend cap (default $0.25) stops the run cleanly if the estimated Bedrock cost passes it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acp import store
from acp.assure.audit import AuditLog
from acp.assure.evaluate import RESULTS, load_scenarios, resolve_model, run_suite, summarise
from acp.assure.ledger import LedgerBackend
from acp.assure.publish import UpstashClient, UpstashError, build_payload, client_from_env, commands_for, publish
from acp.assure.razorpay import (
    RazorpayClient,
    RazorpayError,
    RazorpayTestLedger,
    fetch_snapshot,
    snapshot_summary,
)
from acp.assure.sandbox import Ledger
from acp.assure.scenario_gen import scenarios_from_ledger
from acp.config import ROOT, Settings

AUDIT_ROWS_SHOWN = 40


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _audit_view(log: AuditLog) -> dict[str, Any]:
    chain = log.verify()
    rows = [
        {
            "seq": e["seq"],
            "action": e["action"],
            "run_id": e["run_id"],
            "hash": e["hash"][:14],
            "prev": e["prev_hash"][:14],
        }
        for e in log.entries()[-AUDIT_ROWS_SHOWN:]
    ]
    return {
        "rows": rows,
        "total_rows": chain.rows,
        "head": log.head(),
        "chain_ok": chain.ok,
        "reason": chain.reason,
        "verified_at": _now(),
    }


def run_sync(
    *,
    settings: Any,
    llm: Any,
    razorpay_client: RazorpayClient | None = None,
    include_synthetic: bool = True,
    repeats: int = 1,
    max_cost_usd: float = 0.25,
    publish_client: UpstashClient | None = None,
    workdir: Path = RESULTS,
    log: Callable[[str], None] = print,
    expected_interval_hours: int = 168,
) -> dict[str, Any]:
    started = _now()
    snapshot: dict[str, Any] | None = None
    sources: list[tuple[str, list[dict[str, Any]], Callable[[], LedgerBackend]]] = []
    if razorpay_client is not None:
        snapshot = fetch_snapshot(razorpay_client)
        snap = snapshot
        scenarios = scenarios_from_ledger(RazorpayTestLedger(snap))
        log(f"Razorpay test snapshot: {len(snap['payments'])} payments, {len(scenarios)} generated scenarios")
        sources.append(("razorpay-test", scenarios, lambda: RazorpayTestLedger(snap)))
    if include_synthetic:
        sources.append(("synthetic", load_scenarios(), Ledger))
    if not sources:
        raise ValueError("Nothing to run: enable Razorpay and/or the synthetic suite.")

    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "live-audit.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    audit = AuditLog(store.connect(db))

    spent, aborted = 0.0, False
    results: dict[str, dict[str, list[Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for name, scenarios, factory in sources:
        meta: dict[str, Any] = {}
        log(f"== {name}: {len(scenarios)} scenarios x guarded/unguarded x {repeats}")
        rows = run_suite(
            llm, scenarios, modes=["guarded", "unguarded"], repeats=repeats, settings=settings, audit=audit,
            log=log, ledger_factory=factory, max_cost_usd=max(0.0, max_cost_usd - spent), meta=meta,
        )  # fmt: skip
        spent += meta.get("cost_usd", 0.0)
        aborted = aborted or bool(meta.get("aborted"))
        if rows["guarded"]:
            results[name] = rows
            summaries[name] = {m: summarise(r) for m, r in rows.items() if r}
        if aborted:
            log(f"Stopped early: {meta.get('reason')}")
            break
    if not summaries:
        raise ValueError("The spend cap was reached before a single run finished; raise --max-cost.")

    payload = build_payload(
        results=results, summaries=summaries, model=getattr(llm, "model", ""), started_at=started,
        finished_at=_now(), repeats=repeats, cost_usd=spent, aborted=aborted,
        snapshot=snapshot_summary(snapshot) if snapshot else None, audit=_audit_view(audit),
        expected_interval_hours=expected_interval_hours,
    )  # fmt: skip
    (workdir / "live-latest.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    receipt = publish(publish_client, payload) if publish_client else None
    return {"payload": payload, "receipt": receipt, "commands": len(commands_for(payload))}


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    ap = argparse.ArgumentParser(prog="python -m acp.assure.live")
    ap.add_argument("--model", default=None)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument(
        "--max-cost", type=float, default=0.25, help="stop once the estimated Bedrock cost passes this (USD)"
    )
    ap.add_argument("--no-razorpay", action="store_true")
    ap.add_argument("--no-synthetic", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="run everything but do not publish")
    args = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")

    rz = None
    if not args.no_razorpay:
        key, secret = os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")
        if key and secret:
            try:
                rz = RazorpayClient(key, secret)
            except RazorpayError as e:
                print(f"Razorpay: {e}")
                return 1
        else:
            print("Razorpay: RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set, skipping (synthetic suite only).")
    up = None
    if not args.dry_run:
        up = client_from_env()
        if up is None:
            print(
                "Upstash is not configured: set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN (or use --dry-run)."
            )
            return 1

    settings = Settings()
    if args.model:
        settings = replace(settings, model=resolve_model(args.model, settings.aws_region))
    from acp.llm import BedrockLLM

    try:
        out = run_sync(
            settings=settings, llm=BedrockLLM(settings), razorpay_client=rz, include_synthetic=not args.no_synthetic,
            repeats=args.repeats, max_cost_usd=args.max_cost, publish_client=up,
        )  # fmt: skip
    except (RazorpayError, UpstashError, ValueError) as e:
        print(f"Sync failed: {e}")
        return 1
    latest = out["payload"]["latest"]
    print(f"\nFinished {latest['finished_at']}  cost ${latest['cost_usd']}  aborted={latest['aborted']}")
    for src, v in latest["sources"].items():
        g = v["summary"]["guarded"]
        print(
            f"  {src}: guarded pass {g['pass']['k']}/{g['pass']['n']}, critical outcomes {g['critical_effective']['k']}"
        )
    print("Published and verified." if out["receipt"] else "Dry run: wrote results/live-latest.json (not published).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
