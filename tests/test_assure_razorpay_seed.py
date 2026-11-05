"""The seeder: dry run by default, test keys only, one write endpoint, idempotent, and a safe checkout page."""

import json
import re
from pathlib import Path

import pytest

from acp.assure import razorpay_seed
from acp.assure.razorpay import RazorpayError
from acp.assure.razorpay_seed import (
    ALLOWED_WRITE_PATH,
    PLAN,
    SEED_TAG,
    RazorpaySeeder,
    SeedOrder,
    order_payload,
    render_checkout_page,
    validate_plan,
)

SECRET = "seed-secret-never-in-the-page"


class FakeAccount:
    """A fake Razorpay account that records every call and supports orders."""

    def __init__(self, existing=()):
        self.orders = [dict(o) for o in existing]
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.removeprefix("https://api.razorpay.com/v1")
        self.calls.append((method, path, json.loads(body) if body else None))
        if method == "GET" and path.startswith("/orders"):
            return 200, json.dumps({"items": self.orders}).encode()
        if method == "POST" and path == "/orders":
            payload = json.loads(body)
            order = {"id": f"order_{len(self.orders):03d}", **payload}
            self.orders.append(order)
            return 200, json.dumps(order).encode()
        return 404, b"{}"


def seeder(account=None):
    account = account or FakeAccount()
    return RazorpaySeeder("rzp_test_AbCdEf123456", SECRET, transport=account), account


def test_the_plan_is_valid_and_covers_every_scenario_the_agent_needs():
    validate_plan()
    assert {o.tenant for o in PLAN} == {"m_acme", "m_zen"} and len({o.key for o in PLAN}) == len(PLAN)
    assert any("SYSTEM NOTICE" in o.memo for o in PLAN) and any("4111" in o.memo for o in PLAN)
    assert {o.outcome for o in PLAN} == {"success", "failure"}
    assert all(isinstance(o.amount_paise, int) and o.amount_paise >= 100 for o in PLAN)


@pytest.mark.parametrize(
    "bad",
    [
        SeedOrder("a", "m_acme", 99, "success"),
        SeedOrder("a", "m_acme", 150.5, "success"),
        SeedOrder("a", "m_acme", 500, "maybe"),
    ],
)
def test_an_invalid_plan_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_plan((bad,))
    with pytest.raises(ValueError, match="unique"):
        validate_plan((SeedOrder("a", "m_acme", 500, "success"), SeedOrder("a", "m_zen", 500, "success")))


def test_a_dry_run_reads_but_never_writes():
    s, account = seeder()
    out = s.seed()
    assert out["created"] == 0 and all(r["order_id"] is None for r in out["rows"])
    assert {m for m, _, _ in account.calls} == {"GET"}


def test_create_makes_each_missing_order_once_with_tenant_notes_and_a_receipt():
    s, account = seeder()
    out = s.seed(create=True)
    assert out["created"] == len(PLAN) and all(r["order_id"] for r in out["rows"])
    posts = [(p, b) for m, p, b in account.calls if m == "POST"]
    assert {p for p, _ in posts} == {ALLOWED_WRITE_PATH}
    body = next(b for _, b in posts if b["receipt"] == "acme-04")
    assert body["amount"] == 220_000 and body["currency"] == "INR"
    assert (
        body["notes"]["tenant"] == "m_acme"
        and body["notes"]["seed"] == SEED_TAG
        and "SYSTEM NOTICE" in body["notes"]["memo"]
    )


def test_a_rerun_creates_nothing_because_receipts_already_exist():
    s, account = seeder()
    s.seed(create=True)
    again, _ = seeder(account)
    account.calls.clear()
    out = again.seed(create=True)
    assert out["created"] == 0 and out["existing"] == len(PLAN)
    assert not [c for c in account.calls if c[0] == "POST"]


def test_orders_not_made_by_this_seeder_are_left_alone_and_not_counted():
    other = {"id": "order_x", "receipt": "acme-01", "notes": {"tenant": "m_acme"}}  # no seed tag
    s, account = seeder(FakeAccount([other]))
    out = s.seed()
    assert out["existing"] == 0 and next(r for r in out["rows"] if r["order"].key == "acme-01")["order_id"] is None


# ------------------------------------------------------------------ the guards
@pytest.mark.parametrize("key", ["rzp_live_abc", "", "abc", None])
def test_live_or_missing_keys_are_refused_before_any_request(key):
    account = FakeAccount()
    with pytest.raises(RazorpayError, match="TEST keys"):
        RazorpaySeeder(key, SECRET, transport=account)
    assert account.calls == []


@pytest.mark.parametrize("path", ["/refunds", "/payments/pay_1/capture", "/payments", "/orders/order_1"])
def test_writes_to_anything_but_orders_are_refused(path):
    s, account = seeder()
    with pytest.raises(RazorpayError, match="only create orders"):
        s._call("POST", path, {"amount": 100})
    assert account.calls == []
    for verb in ("DELETE", "PUT", "PATCH"):
        with pytest.raises(RazorpayError, match="only reads and creates orders"):
            s._call(verb, "/orders")


def test_the_module_never_mentions_endpoints_that_move_money():
    src = Path(razorpay_seed.__file__).read_text()
    assert not re.search(r"/refunds|/capture|/payouts|/transfers|/settlements", src)


def test_http_errors_are_clear_and_never_leak_the_secret():
    def refuse(method, url, headers, body):
        return 400, json.dumps({"error": {"description": "The amount must be at least 100."}}).encode()

    s = RazorpaySeeder("rzp_test_AbCdEf123456", SECRET, transport=refuse)
    with pytest.raises(RazorpayError) as e:
        s._call("POST", "/orders", {})
    assert "at least 100" in str(e.value) and SECRET not in str(e.value)
    unauth = RazorpaySeeder("rzp_test_AbCdEf123456", SECRET, transport=lambda *a: (401, b"{}"))
    with pytest.raises(RazorpayError, match="401"):
        unauth.existing_orders()


# ------------------------------------------------------------------ the checkout page
def test_the_checkout_page_has_the_public_key_and_every_order_but_never_the_secret():
    s, _ = seeder()
    out = s.seed(create=True)
    page = render_checkout_page("rzp_test_AbCdEf123456", out["rows"])
    assert "rzp_test_AbCdEf123456" in page and SECRET not in page
    assert "success@razorpay" in page and "failure@razorpay" in page
    assert page.count("data-order=") == len(PLAN)
    for token in ("__ROWS__", "__KEY__", "__SEED__", "__ORDERS__"):
        assert token not in page


def test_checkout_data_cannot_break_out_of_the_script_tag_or_inject_html():
    evil = SeedOrder("e1", "m_acme", 500, "success", memo="</script><img src=x onerror=alert(1)>", why="<b>x</b>")
    page = render_checkout_page("rzp_test_AbCdEf123456", [{"order": evil, "order_id": "order_1"}])
    body = page.split("<script")[0]  # the HTML the browser parses as markup
    data = page.split("const KEY")[1].split("</script>")[0]  # the script data: inert JSON strings
    assert "<img" not in body and "<b>x</b>" not in body and "&lt;b&gt;x&lt;/b&gt;" in body
    assert "</script" not in data  # the "</" in the memo is escaped, so the data cannot close the tag
    assert "<\\/script>" in data


def test_the_checkout_page_refuses_a_live_key():
    with pytest.raises(RazorpayError):
        render_checkout_page("rzp_live_abc", [])


def test_order_payload_only_includes_a_memo_when_there_is_one():
    assert "memo" not in order_payload(SeedOrder("k", "m_zen", 500, "success"))["notes"]
    assert order_payload(SeedOrder("k", "m_zen", 500, "success", memo="hi"))["notes"]["memo"] == "hi"
