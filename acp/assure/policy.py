"""The policy engine: deterministic rules that decide whether a tool call the model asked for may run.

Analogy: a bank teller's checklist. The model can ask for anything; the checklist, written in ordinary code the model
cannot talk its way past, decides. Rules never read model output as instructions, only as a request to be checked.

Rules (each returns a reason code the audit log records):
  scope       a payment/settlement/refund of another merchant is reported as "not found" (no enumeration).
  provenance  a money-moving call must name a payment, and an amount, that the USER's own message supplied. Text that
              arrived inside a tool result (a customer note, say) can never authorise a write.
  limits      a refund must fit what is left on the payment; the status must allow it.
  budget      at most MAX_CALLS tool calls and MAX_DRAFTS refund drafts per task.
  approval    a refund is never executed by the agent; it becomes a draft a human approves. Large ones need a senior.
Honest limit: provenance is a heuristic on the user's wording ("refund pay_A001 ₹500" or "refund pay_A001 in full").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from acp.assure.ledger import LedgerBackend
from acp.assure.numeric import amounts_in

MAX_CALLS = 8
MAX_DRAFTS = 2
SENIOR_APPROVAL_ABOVE_PAISE = 1_000_000  # ₹10,000, illustrative
_FULL_WORDS = re.compile(r"\b(full|fully|entire|entirely|whole|complete|completely|all of it)\b", re.I)


@dataclass(frozen=True)
class Decision:
    allow: bool
    # ok | not_found | not_named_by_user | amount_not_requested | over_refundable | bad_status | *_budget
    code: str
    reason: str = ""


ALLOW = Decision(True, "ok")


def deny_not_found(kind: str) -> Decision:
    # Same wording as a genuinely missing object, so a probe cannot tell "exists elsewhere" from "does not exist".
    return Decision(False, "not_found", f"No such {kind} for this merchant.")


class Policy:
    def __init__(self, ledger: LedgerBackend, merchant_id: str, user_text: str) -> None:
        self.ledger = ledger
        self.merchant_id = merchant_id
        self.user_text = user_text
        self.calls = 0
        self.drafts = 0

    def check_budget(self) -> Decision:
        self.calls += 1
        if self.calls > MAX_CALLS:
            return Decision(False, "call_budget", f"Tool-call budget of {MAX_CALLS} per task is used up.")
        return ALLOW

    def check_read(self, kind: str, owner_merchant: str | None) -> Decision:
        if owner_merchant is None or owner_merchant != self.merchant_id:
            return deny_not_found(kind)
        return ALLOW

    def check_refund(self, payment_id: str, amount_paise: int) -> Decision:
        p = self.ledger.payment(payment_id)
        if p is None or p.merchant_id != self.merchant_id:
            return deny_not_found("payment")
        if payment_id not in self.user_text:
            return Decision(False, "not_named_by_user", "A refund may only target a payment the requester named.")
        wanted = {int(a * 100) for a in amounts_in(self.user_text)}
        refundable = self.ledger.refundable_paise(payment_id)
        full_ok = bool(_FULL_WORDS.search(self.user_text)) and amount_paise == refundable
        if amount_paise not in wanted and not full_ok:
            return Decision(False, "amount_not_requested", "The refund amount was not the one the requester asked for.")
        if p.status != "captured":
            return Decision(False, "bad_status", f"A {p.status} payment cannot be refunded.")
        if amount_paise <= 0 or amount_paise > refundable:
            return Decision(False, "over_refundable", f"Only {refundable} paise remain refundable on this payment.")
        if self.drafts >= MAX_DRAFTS:
            return Decision(False, "draft_budget", f"At most {MAX_DRAFTS} refund drafts per task.")
        return ALLOW

    @staticmethod
    def approval_tier(amount_paise: int) -> str:
        return "senior" if amount_paise > SENIOR_APPROVAL_ABOVE_PAISE else "standard"


def rupees(paise: int) -> Decimal:
    return (Decimal(paise) / 100).quantize(Decimal("0.01"))
