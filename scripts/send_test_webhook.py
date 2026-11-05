"""Send a FIXTURE webhook to a local or deployed receiver, signed with your own webhook secret.

This is a development tool: the payload is made up here, so it is not Razorpay-authentic. Real events come from
paying through Razorpay Checkout in test mode (the dashboard's Live events page has a button for it).

  RAZORPAY_WEBHOOK_SECRET=... python scripts/send_test_webhook.py URL/api/webhooks/razorpay [--failed] [--note "text"]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--failed", action="store_true")
    ap.add_argument("--amount", type=int, default=250_000, help="in paise")
    ap.add_argument("--tenant", default="m_acme")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    if len(secret) < 8:
        print("Set RAZORPAY_WEBHOOK_SECRET in your environment (the same value you gave Razorpay and Vercel).")
        return 1
    status = "failed" if args.failed else "captured"
    pid = "pay_Fx" + uuid.uuid4().hex[:10]
    body = json.dumps(
        {
            "event": f"payment.{status}",
            "created_at": int(time.time()),
            "payload": {
                "payment": {
                    "entity": {
                        "id": pid,
                        "amount": args.amount,
                        "currency": "INR",
                        "status": status,
                        "method": "upi",
                        "created_at": int(time.time()),
                        "error_code": "BAD_REQUEST_ERROR" if args.failed else None,
                        "notes": {"tenant": args.tenant, "memo": args.note},
                    }  # fmt: skip
                }
            },
        }
    ).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sig,
        "X-Razorpay-Event-Id": "fx_" + uuid.uuid4().hex[:16],
    }
    req = urllib.request.Request(args.url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - URL is the one you passed
            print(r.status, r.read().decode(), "payment", pid)
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
