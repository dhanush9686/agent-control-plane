"""What the agent, the policy engine and the scorer need from a ledger, and the record types they exchange.

Analogy: the plug standard. The synthetic sandbox and the Razorpay test-mode adapter are two different power
stations; anything that fits this plug can run the same agent, the same policy rules and the same scorer.

Money is always an integer number of paise. Nothing here moves money: a backend may record a refund request
(draft) or a simulated execution in memory, but must never send one to an external system.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class Payment:
    id: str
    merchant_id: str
    amount_paise: int
    status: str  # captured | failed | authorized
    method: str  # upi | card | netbanking | wallet ...
    created: str
    failure_code: str | None = None
    refunded_paise: int = 0
    card_last4: str | None = None
    note: str = ""  # free text written by the customer: UNTRUSTED input
    settlement_id: str | None = None


@dataclass
class Refund:
    id: str
    payment_id: str
    merchant_id: str
    amount_paise: int
    status: str  # processed | pending_approval | failed
    reason: str = ""


@dataclass
class Settlement:
    id: str
    merchant_id: str
    status: str
    utr: str
    payment_ids: list[str] = field(default_factory=list)
    gross_paise: int = 0
    fee_paise: int = 0
    tax_paise: int = 0
    net_paise: int = 0


def record_as_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)


class LedgerBackend(Protocol):
    """The methods the rest of `acp.assure` calls. Reads are not scoped here: access control is the policy's job."""

    source: str  # provenance label shown on the dashboard, e.g. "synthetic" or "razorpay-test"

    def payment(self, pid: str) -> Payment | None: ...

    def settlement(self, sid: str) -> Settlement | None: ...

    def refund(self, rid: str) -> Refund | None: ...

    def list_payments(self, merchant_id: str | None, status: str | None, limit: int) -> list[Payment]: ...

    def list_settlements(self, merchant_id: str | None) -> list[Settlement]: ...

    def refundable_paise(self, pid: str) -> int: ...

    def execute_refund(self, pid: str, amount_paise: int, reason: str, status: str = "processed") -> Refund: ...

    def new_refunds(self) -> list[Refund]:
        """Refunds created during this session (drafts or simulated executions), not pre-existing ones."""
        ...

    def merchants(self) -> dict[str, str]:
        """merchant id -> display name, for every tenant in this ledger."""
        ...

    def merchant_secrets(self, merchant_id: str) -> set[str]:
        """Strings that, if they appear in another merchant's answer, prove a cross-merchant leak."""
        ...

    def as_dict(self, obj: Any) -> dict[str, Any]: ...
