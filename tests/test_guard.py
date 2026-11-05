"""The guard package: rules from a file, enforced in front of plain functions, with approvals and the audit log."""

from pathlib import Path

import pytest

from acp import store
from acp.assure.audit import AuditLog
from acp.assure.sandbox import Ledger
from acp.guard import ConfigError, parse_config
from examples.guard_quickstart import RULES, build


def make(audit=None):
    ledger = Ledger()
    guard = build(ledger)
    if audit:
        guard.audit = audit
    return ledger, guard


def audit_log(tmp_path):
    return AuditLog(store.connect(tmp_path / "a.db"))


# ----------------------------------------------------------------- the rules file
def test_the_example_rules_file_loads():
    cfg = parse_config(__import__("yaml").safe_load(Path(RULES).read_text()))
    assert set(cfg.tools) == {"get_payment", "list_payments", "propose_refund"}
    assert cfg.max_calls == 8


@pytest.mark.parametrize(
    "bad",
    [
        {"tools": {"t": {"effect": "read", "scopee": {"owner_field": "x"}}}},  # a typo is an error, not a missing rule
        {"tools": {"t": {"effect": "read", "approval": {"required": True}}}},  # approval on a read tool
        {"tools": {"t": {"effect": "write", "scope": {"owner_field": "x"}}}},  # scope on a write tool
        {"tools": {"t": {"effect": "write", "limits": {"a": {"min": 5, "max": 1}}}}},
        {"tools": {"t": {"effect": "write", "limits": {"a": {}}}}},
        {"version": 2, "tools": {}},
        {},
    ],
)
def test_a_bad_rules_file_is_refused_with_a_readable_message(bad):
    with pytest.raises(ConfigError) as e:
        parse_config(bad)
    assert "Invalid rules file" in str(e.value)


def test_registering_a_tool_that_is_not_in_the_rules_is_refused():
    _, guard = make()
    with pytest.raises(KeyError):
        guard.register("delete_everything", lambda: None)


# ----------------------------------------------------------------- reads
def test_another_tenants_object_looks_missing_and_no_data_comes_back():
    _, guard = make()
    s = guard.session("m_acme", "look up pay_Z001")
    out = s.call("get_payment", payment_id="pay_Z001")
    assert not out.allowed and out.code == "not_found" and out.result is None
    assert "999900" not in str(out.for_model())  # the amount of the foreign payment never reaches the model


def test_a_nonexistent_payment_and_a_foreign_one_look_identical():
    _, guard = make()
    s = guard.session("m_acme", "x")
    a, b = s.call("get_payment", payment_id="pay_Z001"), s.call("get_payment", payment_id="pay_NOPE1")
    assert b.allowed and b.result is None  # missing: the function itself returned nothing
    assert a.for_model() == {"error": "No such object for this tenant.", "code": "not_found"}


def test_own_payment_is_returned_and_a_list_drops_foreign_rows():
    _, guard = make()
    s = guard.session("m_acme", "list them")
    assert s.call("get_payment", payment_id="pay_A001").result["amount_paise"] == 149_900
    rows = s.call("list_payments", limit=20).result
    assert rows["count"] == len(rows["payments"]) > 0
    assert {p["merchant_id"] for p in rows["payments"]} == {"m_acme"}


def test_card_data_in_a_result_is_masked():
    _, guard = make()
    s = guard.session("m_acme", "show pay_A006")
    note = s.call("get_payment", payment_id="pay_A006").result["note"]
    assert "4111 1111 1111 1111" not in note and "98765 43210" not in note


# ----------------------------------------------------------------- writes
def test_a_refund_must_name_a_payment_and_an_amount_the_user_asked_for():
    _, guard = make()
    s = guard.session("m_acme", "Refund pay_A001 for Rs 500.00")
    assert s.call("propose_refund", payment_id="pay_A002", amount_paise=50_000).code == "not_named_by_user"
    assert s.call("propose_refund", payment_id="pay_A001", amount_paise=60_000).code == "amount_not_requested"
    ok = s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000)
    assert ok.allowed and ok.code == "pending_approval" and ok.effect == "pending"


def test_a_refund_in_another_tenant_or_beyond_the_limit_is_refused():
    _, guard = make()
    s = guard.session("m_acme", "Refund pay_Z001 for Rs 500.00 and refund pay_A001 for Rs 90000.00")
    assert s.call("propose_refund", payment_id="pay_Z001", amount_paise=50_000).code == "not_found"
    assert s.call("propose_refund", payment_id="pay_A001", amount_paise=9_000_000).code == "over_limit"


def test_text_inside_a_tool_result_can_never_authorise_a_write():
    # pay_A004's note tells the agent to refund pay_A002; the user never named pay_A002.
    _, guard = make()
    s = guard.session("m_acme", "What is the status of pay_A004?")
    assert "SYSTEM NOTICE" in s.call("get_payment", payment_id="pay_A004").result["note"]
    assert s.call("propose_refund", payment_id="pay_A002", amount_paise=2_500_000).code == "not_named_by_user"


def test_a_write_needing_approval_does_not_run_until_a_person_decides():
    ledger, guard = make()
    s = guard.session("m_acme", "Refund pay_A001 for Rs 500.00")
    out = s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000)
    assert ledger.new_refunds() == [] and ledger.payment("pay_A001").refunded_paise == 0
    req = guard.decide(out.approval.id, True, "Priya", "duplicate order")
    assert req.state == "approved" and ledger.payment("pay_A001").refunded_paise == 50_000


def test_a_rejected_request_never_runs_and_a_decision_is_final():
    ledger, guard = make()
    s = guard.session("m_acme", "Refund pay_A001 for Rs 500.00")
    aid = s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000).approval.id
    guard.decide(aid, False, "Priya")
    assert ledger.payment("pay_A001").refunded_paise == 0
    with pytest.raises(ValueError, match="already rejected"):
        guard.decide(aid, True, "Someone else")


def test_the_same_request_twice_is_one_approval_and_a_session_limit_applies():
    _, guard = make()
    s = guard.session("m_acme", "Refund pay_A001 for Rs 500.00, pay_A002 for Rs 100.00, pay_A004 for Rs 100.00")
    a = s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000).approval
    b = s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000).approval
    assert a.id == b.id and len(guard.pending()) == 1
    s.call("propose_refund", payment_id="pay_A002", amount_paise=10_000)
    assert s.call("propose_refund", payment_id="pay_A004", amount_paise=10_000).code == "session_limit"


def test_a_senior_tier_approval_needs_a_written_reason():
    _, guard = make()
    s = guard.session("m_acme", "Refund pay_A002 for Rs 20000.00")
    out = s.call("propose_refund", payment_id="pay_A002", amount_paise=2_000_000)
    assert out.approval.tier == "senior"
    with pytest.raises(ValueError, match="written reason"):
        guard.decide(out.approval.id, True, "Priya")
    assert guard.decide(out.approval.id, True, "Priya", "customer verified by phone").state == "approved"


def test_a_decision_needs_a_name():
    _, guard = make()
    aid = (
        guard.session("m_acme", "Refund pay_A001 for Rs 500.00")
        .call("propose_refund", payment_id="pay_A001", amount_paise=50_000)
        .approval.id
    )
    with pytest.raises(ValueError):
        guard.decide(aid, True, "  ")


# ----------------------------------------------------------------- budget, errors, answers
def test_the_call_budget_stops_a_runaway_agent():
    _, guard = make()
    s = guard.session("m_acme", "x")
    codes = [s.call("get_payment", payment_id="pay_A001").code for _ in range(10)]
    assert codes[:8] == ["ok"] * 8 and codes[8:] == ["call_budget"] * 2


def test_an_unknown_tool_and_a_failing_tool_reveal_nothing_internal():
    _, guard = make()
    guard.config.tools["boom"] = guard.config.tools["get_payment"]
    guard.register("boom", lambda **kw: 1 / 0)
    s = guard.session("m_acme", "x")
    assert s.call("rm_rf").code == "unknown_tool"
    out = s.call("boom")
    assert out.code == "tool_error" and "division" not in out.reason.lower()


def test_an_answer_with_an_invented_amount_is_withheld_and_card_numbers_are_masked(tmp_path):
    _, guard = make()
    s = guard.session("m_acme", "What did pay_A001 cost?")
    s.call("get_payment", payment_id="pay_A001")
    good = s.check_answer("pay_A001 was ₹1,499.00.")
    assert not good.withheld and good.text == "pay_A001 was ₹1,499.00."
    bad = s.check_answer("pay_A001 was ₹1,799.00.")
    assert bad.withheld and bad.unsupported == ["1799.00"] and "1,799" not in bad.text
    masked = s.check_answer("The card was 4111 1111 1111 1111.")
    assert "4111 1111 1111 1111" not in masked.text and masked.masked


# ----------------------------------------------------------------- audit and callbacks
def test_every_decision_lands_in_a_verifiable_audit_chain(tmp_path):
    log = audit_log(tmp_path)
    _, guard = make(log)
    s = guard.session("m_acme", "Refund pay_A001 for Rs 500.00")
    s.call("get_payment", payment_id="pay_Z001")
    aid = s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000).approval.id
    guard.decide(aid, True, "Priya", "ok")
    s.check_answer("done")
    actions = [e["action"] for e in log.entries()]
    assert actions == [
        "run_start",
        "tool_call",
        "approval_requested",
        "tool_call",
        "approval_decided",
        "approval_executed",
        "run_end",
    ]
    assert log.verify().ok
    conn = log._conn
    conn.execute("UPDATE audit_log SET detail = replace(detail, 'pay_Z001', 'pay_A001') WHERE seq = 2")
    conn.commit()
    assert not log.verify().ok  # editing an entry is detected


def test_callbacks_fire_for_blocks_and_approvals_and_the_audit_never_holds_card_numbers(tmp_path):
    log = audit_log(tmp_path)
    blocked, held = [], []
    ledger = Ledger()
    guard = build(ledger)
    guard.audit, guard.on_block, guard.on_approval = log, blocked.append, held.append
    s = guard.session("m_acme", "Refund pay_A001 for Rs 500.00. My card is 4111 1111 1111 1111")
    s.call("get_payment", payment_id="pay_Z001")
    s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000)
    assert [b.code for b in blocked] == ["not_found"] and len(held) == 1
    assert "4111 1111 1111 1111" not in " ".join(e["detail"] for e in log.entries())


def test_the_quickstart_runs(capsys):
    from examples.guard_quickstart import main

    main()
    out = capsys.readouterr().out
    assert out.count("BLOCKED") == 3 and "withheld" in out and "apr_" in out
