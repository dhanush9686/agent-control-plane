"""The Razorpay test-mode adapter, offline: recorded-style payloads, a fake transport, and the safety rules."""

import json
import re
from pathlib import Path

import pytest

from acp.assure import razorpay
from acp.assure.razorpay import (
    RazorpayClient,
    RazorpayError,
    RazorpayTestLedger,
    fetch_snapshot,
    require_test_key,
    snapshot_hash,
    snapshot_summary,
)

SECRET = "s3cr3t-value-never-printed"
SENSITIVE = ["asha@example.com", "+919876543210", "asha@okhdfc", "card_Hh2kL9", "HDFC", "Asha Rao"]


def pay(i, status="captured", amount=150_000, notes=None, refunded=0, **extra):
    base = {
        "id": f"pay_T{i:03d}", "entity": "payment", "amount": amount, "currency": "INR", "status": status,
        "method": "upi", "created_at": 1_772_000_000 + i * 3600, "amount_refunded": refunded,
        "email": "asha@example.com", "contact": "+919876543210", "vpa": "asha@okhdfc", "card_id": "card_Hh2kL9",
        "bank": "HDFC", "description": "Asha Rao order", "notes": notes if notes is not None else [],
        "error_code": "BAD_REQUEST_ERROR" if status == "failed" else None,
    }  # fmt: skip
    base.update(extra)
    return base


class FakeRazorpay:
    """A scripted API: records every request, answers by path."""

    def __init__(self, payments=(), refunds=(), settlements=None):
        self.payments, self.refunds, self.settlements = list(payments), list(refunds), settlements
        self.requests = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        path, _, query = url.removeprefix(razorpay.BASE_URL).partition("?")
        q = dict(x.split("=") for x in query.split("&")) if query else {}
        skip, count = int(q.get("skip", 0)), int(q.get("count", 10))
        if path == "/payments":
            items = self.payments[skip : skip + count]
        elif path == "/refunds":
            items = self.refunds[skip : skip + count]
        elif path == "/orders":
            items = getattr(self, "orders", [])[skip : skip + count]
        elif path == "/settlements":
            if self.settlements is None:
                return 400, json.dumps({"error": {"description": "not available"}}).encode()
            items = self.settlements
        else:
            return 404, b"{}"
        return 200, json.dumps({"entity": "collection", "count": len(items), "items": items}).encode()


def client(api):
    return RazorpayClient("rzp_test_AbCdEf123456", SECRET, transport=api)


# ------------------------------------------------------------------ safety rules
@pytest.mark.parametrize("key", ["rzp_live_abc", "", "abc", None, "RZP_TEST_x"])
def test_only_test_keys_are_accepted_and_no_request_is_made(key):
    api = FakeRazorpay()
    with pytest.raises(RazorpayError, match="TEST keys"):
        require_test_key(key)
    with pytest.raises(RazorpayError):
        RazorpayClient(key, SECRET, transport=api)
    assert api.requests == []


def test_the_module_cannot_issue_anything_but_a_get():
    src = Path(razorpay.__file__).read_text()
    assert not re.search(r"\b(POST|PUT|PATCH|DELETE)\b", src)
    assert "method=" not in src and "data=" not in src  # urllib defaults to GET only when neither is set


def test_requests_use_basic_auth_and_the_secret_never_leaks_into_hints_or_errors():
    api = FakeRazorpay([pay(1)])
    c = client(api)
    c.get("/payments", {"count": 1})
    assert api.requests[0][1]["Authorization"].startswith("Basic ")
    assert SECRET not in c.key_hint and "rzp_test_AbC" in c.key_hint

    def unauthorised(url, headers):
        return 401, b'{"error":{"description":"Authentication failed"}}'

    with pytest.raises(RazorpayError) as e:
        client(unauthorised).get("/payments")
    assert SECRET not in str(e.value) and "401" in str(e.value)


def test_rate_limits_are_retried_and_a_persistent_outage_becomes_a_clear_error(monkeypatch):
    monkeypatch.setattr("acp.llm.time.sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(url, headers):
        calls["n"] += 1
        return (429, b"{}") if calls["n"] < 3 else (200, b'{"items": []}')

    assert client(flaky).get("/payments")[0] == 200 and calls["n"] == 3
    with pytest.raises(RazorpayError, match="unavailable"):
        client(lambda u, h: (503, b"{}")).get("/payments")


# ------------------------------------------------------------------ whitelist and mapping
def test_sensitive_customer_fields_never_enter_the_snapshot():
    api = FakeRazorpay(
        [pay(i) for i in range(5)],
        [{"id": "rfnd_1", "payment_id": "pay_T000", "amount": 100, "status": "processed", "notes": {"x": "Asha Rao"}}],
    )
    snap = fetch_snapshot(client(api))
    dumped = json.dumps(snap)
    for s in SENSITIVE:
        assert s not in dumped, s
    assert SECRET not in dumped and "Basic" not in dumped
    assert set(snap["payments"][0]) == {
        "id",
        "tenant",
        "amount_paise",
        "status",
        "method",
        "created",
        "failure_code",
        "refunded_paise",
        "note",
    }


def test_status_mapping_tenant_tags_and_notes():
    api = FakeRazorpay(
        [
            pay(1, "captured", notes={"tenant": "m_zen", "memo": "x" * 500}),
            pay(2, "refunded", amount=100_000, refunded=100_000),
            pay(3, "failed"),
            pay(4, "created"),  # not a payment attempt we represent: skipped
            pay(5, notes={"tenant": "not valid!"}),
            pay(6, "authorized", notes=[]),
        ]
    )
    snap = fetch_snapshot(client(api))
    by_id = {p["id"]: p for p in snap["payments"]}
    assert "pay_T004" not in by_id and len(by_id) == 5
    assert by_id["pay_T001"]["tenant"] == "m_zen" and len(by_id["pay_T001"]["note"]) == 300
    assert by_id["pay_T002"]["status"] == "captured" and by_id["pay_T002"]["refunded_paise"] == 100_000
    assert by_id["pay_T003"]["status"] == "failed" and by_id["pay_T003"]["failure_code"] == "BAD_REQUEST_ERROR"
    assert by_id["pay_T005"]["tenant"] == "m_acme" and by_id["pay_T006"]["tenant"] == "m_acme"
    assert by_id["pay_T001"]["created"] == "2026-02-25"


def test_pagination_follows_count_and_skip():
    api = FakeRazorpay([pay(i) for i in range(130)])
    snap = fetch_snapshot(client(api), max_payments=130)
    assert len(snap["payments"]) == 130
    paths = [u for u, _ in api.requests if "/payments" in u]
    assert "count=100&skip=0" in paths[0] and "count=30&skip=100" in paths[1]


def test_settlements_may_be_unavailable_in_test_mode_and_are_recorded_as_such():
    snap = fetch_snapshot(client(FakeRazorpay([pay(1)], settlements=None)))
    assert snap["settlements_available"] is False and snap["settlements"] == []
    ok = fetch_snapshot(
        client(
            FakeRazorpay(
                [pay(1)],
                settlements=[
                    {"id": "setl_1", "amount": 9_000, "fees": 800, "tax": 200, "status": "processed", "utr": "UTR1"}
                ],
            )
        )
    )
    assert ok["settlements_available"] is True
    led = RazorpayTestLedger(ok)
    s = led.settlement("setl_1")
    assert (s.gross_paise, s.fee_paise, s.tax_paise, s.net_paise) == (10_000, 800, 200, 9_000)


def test_snapshot_hash_ignores_fetch_time_and_summary_contains_no_payment_ids():
    api = FakeRazorpay([pay(1), pay(2, notes={"tenant": "m_zen"})])
    a, b = fetch_snapshot(client(api)), fetch_snapshot(client(api))
    a["fetched_at"] = "2000-01-01T00:00:00+00:00"
    assert snapshot_hash(a) == a["sha256"] == b["sha256"]
    summary = json.dumps(snapshot_summary(a))
    assert "pay_T" not in summary and '"m_zen": 1' in summary


# ------------------------------------------------------------------ the ledger
def ledger(**kw):
    api = FakeRazorpay(
        [
            pay(1, notes={"tenant": "m_acme"}),
            pay(2, notes={"tenant": "m_zen"}),
            pay(3, "failed"),
            pay(4, refunded=50_000),
        ],
        **kw,
    )
    return RazorpayTestLedger(fetch_snapshot(client(api))), api


def test_ledger_reads_tenants_refundable_amounts_and_secrets():
    led, _ = ledger()
    assert led.source == "razorpay-test" and set(led.merchants()) == {"m_acme", "m_zen"}
    assert led.payment("pay_T001").merchant_id == "m_acme" and led.payment("pay_T999") is None
    assert led.refundable_paise("pay_T004") == 100_000 and led.refundable_paise("pay_T003") == 0
    assert "pay_T002" in led.merchant_secrets("m_zen") and "pay_T001" not in led.merchant_secrets("m_zen")
    assert [p.id for p in led.list_payments("m_acme", "captured", 10)] == ["pay_T004", "pay_T001"]  # newest first


def test_a_refund_is_recorded_in_memory_only_and_never_reaches_razorpay():
    led, api = ledger()
    before = len(api.requests)
    draft = led.execute_refund("pay_T001", 10_000, "returned", status="pending_approval")
    done = led.execute_refund("pay_T001", 20_000, "returned", status="processed")
    assert draft.status == "pending_approval" and done.status == "processed"
    assert led.payment("pay_T001").refunded_paise == 20_000  # only the simulated execution counts
    assert [r.id for r in led.new_refunds()] == [draft.id, done.id]
    assert len(api.requests) == before  # not a single request


def test_each_ledger_instance_has_its_own_overlay():
    api = FakeRazorpay([pay(1)])
    snap = fetch_snapshot(client(api))
    a, b = RazorpayTestLedger(snap), RazorpayTestLedger(snap)
    a.execute_refund("pay_T001", 10_000, "x")
    assert a.payment("pay_T001").refunded_paise == 10_000 and b.payment("pay_T001").refunded_paise == 0


def test_tenant_and_memo_fall_back_to_the_order_notes_when_the_payment_has_none():
    api = FakeRazorpay(
        [
            pay(1, order_id="order_A", notes=[]),
            pay(2, order_id="order_B", notes={"tenant": "m_acme"}),  # the payment's own notes win
            pay(3, order_id="order_missing", notes=[]),
        ]
    )
    api.orders = [
        {"id": "order_A", "notes": {"tenant": "m_zen", "memo": "from the order"}},
        {"id": "order_B", "notes": {"tenant": "m_zen"}},
    ]
    by_id = {p["id"]: p for p in fetch_snapshot(client(api))["payments"]}
    assert by_id["pay_T001"]["tenant"] == "m_zen" and by_id["pay_T001"]["note"] == "from the order"
    assert by_id["pay_T002"]["tenant"] == "m_acme"
    assert by_id["pay_T003"]["tenant"] == "m_acme"  # no notes anywhere: the default tenant


def test_orders_are_not_requested_when_no_payment_belongs_to_an_order():
    api = FakeRazorpay([pay(1)])
    fetch_snapshot(client(api))
    assert not [u for u, _ in api.requests if "/orders" in u]


def test_failed_payments_record_razorpays_specific_reason_over_the_generic_code():
    api = FakeRazorpay(
        [
            pay(1, "failed", error_reason="payment_cancelled"),
            pay(2, "failed", error_reason=None),
            pay(3, "failed", error_reason=None, error_code=None),
            pay(4),
        ]
    )
    by_id = {p["id"]: p for p in fetch_snapshot(client(api))["payments"]}
    assert by_id["pay_T001"]["failure_code"] == "payment_cancelled"
    assert by_id["pay_T002"]["failure_code"] == "BAD_REQUEST_ERROR"  # falls back to the generic code
    assert by_id["pay_T003"]["failure_code"] is None and by_id["pay_T004"]["failure_code"] is None
