"""A read-only ledger backed by Razorpay TEST-mode data: real API shapes, test payments, no real money.

Analogy: a window, not a door. We can look at the payments in a Razorpay test account, copy a sanitised snapshot,
and let the agent work on the copy. Nothing in this module can change anything in Razorpay.

Safety rules, each covered by a test:
  * Only test keys work. A key that does not start with `rzp_test_` is refused before any request is made.
  * Only GET requests exist. The transport has no method argument, so a write is not even expressible.
  * Only whitelisted fields are copied out of each API object. Email, phone, VPA, card ids and the raw JSON never
    reach the snapshot, the model, Redis or the audit log.
  * A refund the agent asks for is recorded in memory (a draft, or a simulated execution in the unguarded baseline).
    It is never sent to Razorpay.
Honest limits: a Razorpay account is one merchant, so tenancy is emulated with a `tenant` tag in each payment's
`notes`; settlement reports may be unavailable in test mode (the snapshot records whether they were); and this reads
test data only, so it says nothing about production traffic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from acp.assure.ledger import Payment, Refund, Settlement, record_as_dict
from acp.errors import LLMUnavailableError
from acp.llm import call_with_retries

BASE_URL = "https://api.razorpay.com/v1"
PAGE = 100  # the documented maximum for `count`
TEST_KEY_PREFIX = "rzp_test_"
TENANT_NAMES = {"m_acme": "Acme Stores", "m_zen": "Zen Books"}
_TENANT = re.compile(r"^m_[a-z0-9_]{1,24}$")
NOTE_MAX = 300

# (url, headers) -> (http status, body bytes). No method parameter: every request is a GET.
Transport = Callable[[str, dict[str, str]], tuple[int, bytes]]


class RazorpayError(Exception):
    """A Razorpay call failed; the message never contains a key or a secret."""


def require_test_key(key_id: str) -> str:
    if not isinstance(key_id, str) or not key_id.startswith(TEST_KEY_PREFIX):
        raise RazorpayError(
            "Refusing to use this key: only Razorpay TEST keys (starting with 'rzp_test_') are allowed. "
            "This project never touches live data."
        )
    return key_id


def _urllib_transport(timeout: float = 15.0) -> Transport:
    def go(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        req = urllib.request.Request(
            url, headers=headers
        )  # a plain Request: urllib sends a GET unless a body or a verb is supplied
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    return go


class _Retryable(Exception):
    pass


class RazorpayClient:
    def __init__(self, key_id: str, key_secret: str, transport: Transport | None = None) -> None:
        self._key_id = require_test_key(key_id)
        token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._transport = transport or _urllib_transport()

    @property
    def key_hint(self) -> str:
        return self._key_id[:12] + "…"  # enough to recognise the account's key in logs; never the secret

    def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        url = BASE_URL + path + (("?" + urllib.parse.urlencode(params)) if params else "")

        def call() -> tuple[int, bytes]:
            status, body = self._transport(url, self._headers)
            if status == 429 or status >= 500:
                raise _Retryable(f"HTTP {status}")
            return status, body

        try:
            (status, body), _, _ = call_with_retries(
                call, max_retries=3, retryable=lambda e: isinstance(e, _Retryable), deadline_s=30.0
            )
        except LLMUnavailableError as e:  # the shared retry helper's error type; say what actually failed
            raise RazorpayError("Razorpay is rate-limiting or unavailable (retried 3 times).") from e
        try:
            data = json.loads(body.decode() or "{}")
        except ValueError:
            data = {}
        if status == 401:
            raise RazorpayError(
                "Razorpay rejected the test keys (HTTP 401). Check RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET."
            )
        return status, data


# ------------------------------------------------------------------ whitelisted mapping
def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _tenant_and_note(notes: Any, default_tenant: str) -> tuple[str, str]:
    if not isinstance(notes, dict):  # the API returns [] when there are no notes
        return default_tenant, ""
    tenant = notes.get("tenant")
    tenant = tenant if isinstance(tenant, str) and _TENANT.match(tenant) else default_tenant
    memo = notes.get("memo")
    return tenant, (memo[:NOTE_MAX] if isinstance(memo, str) else "")


def _failure_code(raw: dict[str, Any]) -> str | None:
    """Razorpay's specific reason (e.g. payment_cancelled) when present, else its generic error code."""
    code = raw.get("error_reason") or raw.get("error_code")
    return str(code)[:60] if code else None


STATUS = {"captured": "captured", "refunded": "captured", "failed": "failed", "authorized": "authorized"}


def whitelist_payment(
    raw: dict[str, Any], default_tenant: str = "m_acme", order_notes: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Copy only the fields we use. Returns None for objects we cannot represent (for example status 'created').

    The tenant tag and memo are read from the payment's own notes first, then from its order's notes, because
    Razorpay keeps notes on both objects and we cannot assume which one the data was written to.
    """
    status = STATUS.get(str(raw.get("status")))
    pid = raw.get("id")
    if status is None or not isinstance(pid, str) or not isinstance(raw.get("amount"), int):
        return None
    notes = raw.get("notes")
    if not (isinstance(notes, dict) and notes) and order_notes:
        notes = order_notes
    tenant, note = _tenant_and_note(notes, default_tenant)
    return {
        "id": pid,
        "tenant": tenant,
        "amount_paise": _int(raw.get("amount")),
        "status": status,
        "method": str(raw.get("method") or "unknown")[:20],
        "created": _iso(raw.get("created_at")),
        "failure_code": _failure_code(raw),
        "refunded_paise": _int(raw.get("amount_refunded")),
        "note": note,
    }


def whitelist_refund(raw: dict[str, Any], tenants: dict[str, str]) -> dict[str, Any] | None:
    rid, pid = raw.get("id"), raw.get("payment_id")
    if not isinstance(rid, str) or not isinstance(pid, str) or not isinstance(raw.get("amount"), int):
        return None
    return {
        "id": rid,
        "payment_id": pid,
        "tenant": tenants.get(pid),
        "amount_paise": _int(raw.get("amount")),
        "status": str(raw.get("status") or "unknown")[:20],
    }


def whitelist_settlement(raw: dict[str, Any], default_tenant: str) -> dict[str, Any] | None:
    sid = raw.get("id")
    if not isinstance(sid, str):
        return None
    net, fees, tax = _int(raw.get("amount")), _int(raw.get("fees")), _int(raw.get("tax"))
    return {
        "id": sid,
        "tenant": default_tenant,
        "status": str(raw.get("status") or "unknown")[:20],
        "utr": str(raw.get("utr") or "")[:40],
        "net_paise": net,
        "fee_paise": fees,
        "tax_paise": tax,
    }


def _paged(client: RazorpayClient, path: str, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while len(items) < limit:
        status, data = client.get(path, {"count": min(PAGE, limit - len(items)), "skip": len(items)})
        if status != 200:
            raise RazorpayError(f"Razorpay {path} returned HTTP {status}.")
        page = data.get("items") or []
        items.extend(p for p in page if isinstance(p, dict))
        if len(page) < PAGE:
            break
    return items[:limit]


def fetch_snapshot(
    client: RazorpayClient, *, max_payments: int = 200, max_refunds: int = 200, default_tenant: str = "m_acme"
) -> dict[str, Any]:
    """Read the account once and return a sanitised, JSON-able snapshot (the evidence for a sync)."""
    raw_payments = _paged(client, "/payments", max_payments)
    order_notes: dict[str, dict[str, Any]] = {}
    if any(isinstance(p.get("order_id"), str) for p in raw_payments):
        status, _ = client.get("/orders", {"count": 1})
        if status == 200:
            for o in _paged(client, "/orders", max_payments):
                if isinstance(o.get("id"), str) and isinstance(o.get("notes"), dict):
                    order_notes[o["id"]] = o["notes"]
    payments = [
        w for p in raw_payments if (w := whitelist_payment(p, default_tenant, order_notes.get(p.get("order_id") or "")))
    ]
    owner = {p["id"]: p["tenant"] for p in payments}
    refunds = [w for r in _paged(client, "/refunds", max_refunds) if (w := whitelist_refund(r, owner))]
    status, data = client.get("/settlements", {"count": PAGE})
    available = status == 200
    settlements = (
        [w for s in (data.get("items") or []) if isinstance(s, dict) and (w := whitelist_settlement(s, default_tenant))]
        if available
        else []
    )
    snap = {
        "source": "razorpay-test",
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "key_hint": client.key_hint,
        "settlements_available": available,
        "payments": payments,
        "refunds": refunds,
        "settlements": settlements,
    }
    snap["sha256"] = snapshot_hash(snap)
    return snap


def snapshot_hash(snap: dict[str, Any]) -> str:
    body = {k: v for k, v in snap.items() if k not in ("fetched_at", "sha256")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot_summary(snap: dict[str, Any]) -> dict[str, Any]:
    """The small, non-sensitive description of a snapshot that is published as provenance."""
    by_tenant: dict[str, int] = {}
    for p in snap["payments"]:
        by_tenant[p["tenant"]] = by_tenant.get(p["tenant"], 0) + 1
    return {
        "source": snap["source"],
        "fetched_at": snap["fetched_at"],
        "sha256": snap["sha256"],
        "payments": len(snap["payments"]),
        "refunds": len(snap["refunds"]),
        "settlements": len(snap["settlements"]),
        "settlements_available": snap["settlements_available"],
        "payments_by_tenant": by_tenant,
        "statuses": {
            s: sum(1 for p in snap["payments"] if p["status"] == s) for s in ("captured", "failed", "authorized")
        },
    }


def snapshot_from_webhook(raw_payment: dict[str, Any], default_tenant: str = "m_acme") -> dict[str, Any] | None:
    """A one-payment snapshot built from a webhook's payment entity, or None if it cannot be represented.

    No Razorpay call is made: the payload that Razorpay delivered is the only source. It goes through the same
    field whitelist as a full snapshot, so anything the webhook receiver let through is re-checked here.
    """
    w = whitelist_payment(raw_payment, default_tenant)
    if w is None:
        return None
    snap: dict[str, Any] = {
        "source": "razorpay-test", "settlements_available": False, "payments": [w], "refunds": [], "settlements": [],
    }  # fmt: skip
    snap["sha256"] = snapshot_hash(snap)
    return snap


# ------------------------------------------------------------------ the ledger
class RazorpayTestLedger:
    source = "razorpay-test"

    def __init__(self, snapshot: dict[str, Any]) -> None:
        """Build a fresh in-memory ledger from a snapshot. Every instance has its own refund overlay."""
        self.snapshot_sha = snapshot.get("sha256", "")
        self.payments: dict[str, Payment] = {}
        for p in snapshot["payments"]:
            self.payments[p["id"]] = Payment(
                p["id"], p["tenant"], p["amount_paise"], p["status"], p["method"], p["created"],
                p["failure_code"], p["refunded_paise"], None, p["note"], None,
            )  # fmt: skip
        self.refunds: dict[str, Refund] = {}
        for r in snapshot["refunds"]:
            owner = r.get("tenant") or (
                self.payments[r["payment_id"]].merchant_id if r["payment_id"] in self.payments else ""
            )
            self.refunds[r["id"]] = Refund(r["id"], r["payment_id"], owner, r["amount_paise"], r["status"], "")
        self.settlements: dict[str, Settlement] = {}
        for s in snapshot.get("settlements", []):
            gross = s["net_paise"] + s["fee_paise"] + s["tax_paise"]
            self.settlements[s["id"]] = Settlement(
                s["id"], s["tenant"], s["status"], s["utr"], [], gross, s["fee_paise"], s["tax_paise"], s["net_paise"]
            )
        self._seq = 0
        self._tenants = sorted({p.merchant_id for p in self.payments.values()}) or ["m_acme"]

    # ----- reads
    def payment(self, pid: str) -> Payment | None:
        return self.payments.get(pid)

    def settlement(self, sid: str) -> Settlement | None:
        return self.settlements.get(sid)

    def refund(self, rid: str) -> Refund | None:
        return self.refunds.get(rid)

    def list_payments(self, merchant_id: str | None, status: str | None, limit: int) -> list[Payment]:
        rows = [
            p
            for p in self.payments.values()
            if (merchant_id is None or p.merchant_id == merchant_id) and (status is None or p.status == status)
        ]
        return sorted(rows, key=lambda p: (p.created, p.id), reverse=True)[:limit]

    def list_settlements(self, merchant_id: str | None) -> list[Settlement]:
        return [s for s in self.settlements.values() if merchant_id is None or s.merchant_id == merchant_id]

    def refundable_paise(self, pid: str) -> int:
        p = self.payments[pid]
        return max(0, p.amount_paise - p.refunded_paise) if p.status == "captured" else 0

    # ----- the only "write": an in-memory overlay. Nothing is ever sent to Razorpay.
    def execute_refund(self, pid: str, amount_paise: int, reason: str, status: str = "processed") -> Refund:
        self._seq += 1
        p = self.payments[pid]
        r = Refund(f"rfnd_N{self._seq:03d}", pid, p.merchant_id, amount_paise, status, reason)
        self.refunds[r.id] = r
        if status == "processed":
            p.refunded_paise += amount_paise
        return r

    def new_refunds(self) -> list[Refund]:
        return [r for r in self.refunds.values() if r.id.startswith("rfnd_N")]

    def merchants(self) -> dict[str, str]:
        return {t: TENANT_NAMES.get(t, t.removeprefix("m_").replace("_", " ").title()) for t in self._tenants}

    def merchant_secrets(self, merchant_id: str) -> set[str]:
        out = {self.merchants().get(merchant_id, merchant_id)}
        out |= {p.id for p in self.payments.values() if p.merchant_id == merchant_id}
        out |= {s.id for s in self.settlements.values() if s.merchant_id == merchant_id}
        out |= {s.utr for s in self.settlements.values() if s.merchant_id == merchant_id and s.utr}
        return out

    def as_dict(self, obj: Any) -> dict[str, Any]:
        return record_as_dict(obj)
