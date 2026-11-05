"""Check a Razorpay TEST account is reachable and see what it contains. Read-only; prints counts and field names only.

    python scripts/razorpay_probe.py

Reads RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET from the environment (or a gitignored .env). Refuses live keys.
Never prints a secret, a customer value, or a payment's contents.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from acp.assure.razorpay import RazorpayClient, RazorpayError, fetch_snapshot, snapshot_summary  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    key, secret = os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key or not secret:
        print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test-mode keys) in your environment or .env, then retry.")
        return 2
    try:
        client = RazorpayClient(key, secret)
        print(f"Key: {client.key_hint} (test mode)")
        status, data = client.get("/payments", {"count": 1})
        print(f"GET /payments           -> HTTP {status}")
        items = data.get("items") or []
        if items:
            print("  fields on a payment object:", ", ".join(sorted(items[0])))
        snap = fetch_snapshot(client)
        s = snapshot_summary(snap)
        print(f"Payments usable         : {s['payments']}  {s['statuses']}")
        print(f"Payments by tenant tag  : {s['payments_by_tenant']}  (tag payments with notes.tenant = m_acme / m_zen)")
        print(f"Refunds                 : {s['refunds']}")
        print(f"Settlements available   : {s['settlements_available']} ({s['settlements']} returned)")
        notes = sum(1 for p in snap["payments"] if p["note"])
        print(f"Payments with a memo note: {notes}")
        ready = s["payments"] >= 8 and len(s["payments_by_tenant"]) >= 2
        print("\nReady for a live sync." if ready else "\nNot enough data yet: see docs/LIVE_DATA.md (seeding).")
        return 0
    except RazorpayError as e:
        print(f"Razorpay error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
