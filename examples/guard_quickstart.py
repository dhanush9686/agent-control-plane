"""Run the guard in front of three tools, with no AI model involved, and print every decision.

    python examples/guard_quickstart.py

The "agent" here is a fixed list of calls, including the ones an attacker would try. The tools are a sample in-memory
ledger. Swap in your own functions and your own rules file; nothing else changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a checkout without installing

from acp.assure.sandbox import Ledger
from acp.guard import Guard

RULES = Path(__file__).with_name("payments.guard.yaml")


def build(ledger: Ledger, audit_db: str | None = None) -> Guard:
    def get_payment(payment_id: str) -> dict | None:
        p = ledger.payment(payment_id)
        return ledger.as_dict(p) if p else None

    def list_payments(limit: int = 10) -> dict:
        rows = [
            ledger.as_dict(p) for p in ledger.list_payments(None, None, limit)
        ]  # unscoped on purpose: the guard scopes it
        return {"payments": rows, "count": len(rows)}

    def propose_refund(payment_id: str, amount_paise: int, reason: str = "") -> dict:
        return ledger.as_dict(ledger.execute_refund(payment_id, amount_paise, reason, status="processed"))

    guard = Guard.from_file(RULES, audit_db=audit_db)
    guard.register("get_payment", get_payment).register("list_payments", list_payments)
    guard.register("propose_refund", propose_refund)

    def same_tenant(args: dict, session) -> tuple[str, str] | None:  # a rule that needs the ledger, so it is code
        p = ledger.payment(str(args.get("payment_id")))
        return None if p and p.merchant_id == session.tenant else ("not_found", "No such payment for this merchant.")

    def within_refundable(args: dict, session) -> tuple[str, str] | None:
        p = ledger.payment(str(args.get("payment_id")))
        ok = p is not None and 0 < int(args.get("amount_paise", 0)) <= ledger.refundable_paise(p.id)
        return None if ok else ("over_limit", "More than what is left refundable on this payment.")

    guard.add_check("propose_refund", same_tenant).add_check("propose_refund", within_refundable)
    return guard


def main() -> None:
    ledger = Ledger()
    guard = build(ledger)
    s = guard.session("m_acme", "Please refund pay_A001 for Rs 500.00, it was a duplicate.")
    attempts = [
        ("read another merchant's payment", "get_payment", {"payment_id": "pay_Z001"}),
        ("read its own payment", "get_payment", {"payment_id": "pay_A001"}),
        ("refund a payment nobody mentioned", "propose_refund", {"payment_id": "pay_A002", "amount_paise": 50_000}),
        ("refund far more than asked", "propose_refund", {"payment_id": "pay_A001", "amount_paise": 9_000_000}),
        (
            "the refund that was actually asked for",
            "propose_refund",
            {"payment_id": "pay_A001", "amount_paise": 50_000},
        ),
    ]
    for label, tool, args in attempts:
        out = s.call(tool, **args)
        print(f"{label:42} -> {'ALLOWED' if out.allowed else 'BLOCKED':8} {out.code}")
    print("\nHeld for a person:", [a.id for a in guard.pending()])
    shown = s.check_answer("Your refund of ₹7,777.00 is queued.")
    print("Answer check:", "withheld" if shown.withheld else "shown", "-", shown.text)


if __name__ == "__main__":
    main()
