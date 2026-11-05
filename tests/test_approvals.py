"""The approvals queue and the alerts, offline: a fake Redis, a fake alert endpoint, real audit log and real policy."""

import json
from types import SimpleNamespace

from acp import store
from acp.assure import alerts, approvals
from acp.assure.approvals import (
    KEY_LIST,
    key_audit,
    key_decision,
    key_record,
    publish_drafts,
    queue_and_alert,
    read_all,
    sync_decisions,
)
from acp.assure.audit import AuditLog
from acp.assure.simulate import KEY_SIM_QUEUE, key_sim_job, process_job
from tests.test_assure_publish import TOKEN, UpstashClient

HOOK = "https://hooks.example.com/services/T000/B000/XXXX"
SETTINGS = SimpleNamespace(cost_usd=lambda i, o, m=None: 0.0)
DRAFT = {
    "id": "rfnd_N001", "payment_id": "pay_A001", "amount_paise": 50_000, "status": "pending_approval",
    "reason": "duplicate", "idempotency_key": "abc123", "approval_tier": "standard",
}  # fmt: skip


class Redis:
    """The Redis commands the worker and approvals use, with NX and expiry recorded."""

    def __init__(self):
        self.kv, self.lists, self.ttl = {}, {}, {}

    def __call__(self, url, headers, body):
        out = []
        for c in json.loads(body):
            op, key = c[0], c[1]
            if op == "SET":
                if "NX" in c and key in self.kv:
                    out.append({"result": None})
                    continue
                self.kv[key] = c[2]
                if "EX" in c:
                    self.ttl[key] = c[c.index("EX") + 1]
                out.append({"result": "OK"})
            elif op == "GET":
                out.append({"result": self.kv.get(key)})
            elif op == "MGET":
                out.append({"result": [self.kv.get(k) for k in c[1:]]})
            elif op == "LPUSH":
                for v in c[2:]:
                    self.lists.setdefault(key, []).insert(0, v)
                out.append({"result": len(self.lists[key])})
            elif op == "LTRIM":
                self.lists[key] = self.lists.get(key, [])[c[2] : c[3] + 1]
                out.append({"result": "OK"})
            elif op == "LRANGE":
                out.append({"result": self.lists.get(key, [])[c[2] : c[3] + 1 if c[3] != -1 else None]})
            elif op == "RPOP":
                q = self.lists.get(key, [])
                out.append({"result": q.pop() if q else None})
            elif op == "INCR":
                self.kv[key] = str(int(self.kv.get(key, 0)) + 1)
                out.append({"result": int(self.kv[key])})
            elif op == "INCRBYFLOAT":
                self.kv[key] = str(float(self.kv.get(key, 0)) + float(c[2]))
                out.append({"result": self.kv[key]})
            elif op == "EXPIRE":
                self.ttl[key] = c[2]
                out.append({"result": 1})
            else:
                out.append({"error": f"unsupported {op}"})
        return 200, json.dumps(out).encode()


def client():
    fake = Redis()
    return UpstashClient("https://eu1-x.upstash.io", TOKEN, transport=fake), fake


class Hook:
    """Stands in for the alert endpoint and remembers what it was sent."""

    def __init__(self, status=200):
        self.sent, self.status = [], status

    def __call__(self, url, body):
        self.sent.append((url, json.loads(body)["text"]))
        return self.status


SRC = {"kind": "sim", "id": "sim_1", "title": "Legitimate refund"}


def decide(fake, aid, decision="approved", by="Priya", note="checked"):
    fake.kv[key_decision(aid)] = json.dumps(
        {"decision": decision, "by": by, "note": note, "at": "2026-11-05T10:00:00+00:00"}
    )


# ------------------------------------------------------------------ the queue
def test_a_draft_becomes_one_approval_and_a_rerun_adds_nothing():
    c, fake = client()
    [rec] = publish_drafts(c, SRC, "m_acme", [DRAFT])
    assert rec["id"].startswith("apr_") and rec["tier"] == "standard" and rec["amount_paise"] == 50_000
    assert fake.lists[KEY_LIST] == [rec["id"]] and fake.ttl[key_record(rec["id"])] == approvals.TTL_S
    assert publish_drafts(c, SRC, "m_acme", [DRAFT]) == []  # same source, same draft: not queued twice
    assert fake.lists[KEY_LIST] == [rec["id"]]
    other = publish_drafts(c, {**SRC, "id": "sim_2"}, "m_acme", [DRAFT])  # a different run is a different request
    assert len(other) == 1 and len(fake.lists[KEY_LIST]) == 2


def test_the_request_holds_ids_and_amounts_and_masks_card_numbers_in_the_reason():
    c, fake = client()
    [rec] = publish_drafts(c, SRC, "m_acme", [{**DRAFT, "reason": "card 4111 1111 1111 1111 was charged twice"}])
    assert "4111 1111 1111 1111" not in fake.kv[key_record(rec["id"])]


def test_read_all_merges_the_decision_and_the_audit_state():
    c, fake = client()
    [a] = publish_drafts(c, SRC, "m_acme", [DRAFT])
    [b] = publish_drafts(
        c, SRC, "m_acme", [{**DRAFT, "idempotency_key": "zzz", "amount_paise": 1_200_000, "approval_tier": "senior"}]
    )
    decide(fake, a["id"], "rejected")
    by_id = {r["id"]: r for r in read_all(c)}
    assert by_id[a["id"]]["state"] == "rejected" and by_id[a["id"]]["decision"]["by"] == "Priya"
    assert by_id[b["id"]]["state"] == "pending" and by_id[b["id"]]["tier"] == "senior"


# ------------------------------------------------------------------ decisions reach the audit chain
def test_a_decision_is_written_into_the_audit_chain_exactly_once(tmp_path):
    c, fake = client()
    log = AuditLog(store.connect(tmp_path / "a.db"))
    [rec] = publish_drafts(c, SRC, "m_acme", [DRAFT])
    assert sync_decisions(c, log) == 0  # nothing decided yet
    decide(fake, rec["id"], "approved", by="Priya", note="duplicate order confirmed")
    assert sync_decisions(c, log) == 1 and sync_decisions(c, log) == 0
    [entry] = log.entries()
    assert entry["action"] == "approval_decided" and entry["actor"] == "Priya" and entry["run_id"] == rec["id"]
    assert '"decision":"approved"' in entry["detail"] and "pay_A001" in entry["detail"]
    assert json.loads(fake.kv[key_audit(rec["id"])])["hash"] == entry["hash"] and log.verify().ok


def test_without_an_audit_log_nothing_is_recorded_and_nothing_breaks():
    c, fake = client()
    [rec] = publish_drafts(c, SRC, "m_acme", [DRAFT])
    decide(fake, rec["id"])
    assert sync_decisions(c, None) == 0 and key_audit(rec["id"]) not in fake.kv


# ------------------------------------------------------------------ alerts
def test_nothing_is_sent_unless_an_https_url_is_configured():
    hook = Hook()
    assert not alerts.send_alert(None, "x", env={}, post=hook)
    assert not alerts.send_alert(None, "x", env={"ALERT_WEBHOOK_URL": "http://insecure.example.com/h"}, post=hook)
    assert hook.sent == []
    assert alerts.send_alert(None, "hello", env={"ALERT_WEBHOOK_URL": HOOK}, post=hook) and hook.sent[0] == (
        HOOK,
        "hello",
    )


def test_a_failing_endpoint_or_a_crash_never_raises_and_reports_false():
    logs = []
    env = {"ALERT_WEBHOOK_URL": HOOK}
    assert not alerts.send_alert(None, "x", env=env, post=Hook(status=500), log=logs.append)

    def boom(url, body):
        raise OSError("network down")

    assert not alerts.send_alert(None, "x", env=env, post=boom, log=logs.append)
    assert any("500" in m for m in logs) and any("OSError" in m for m in logs)


def test_alerts_are_rate_limited_per_hour_and_masked():
    c, _ = client()
    hook, env = Hook(), {"ALERT_WEBHOOK_URL": HOOK}
    results = [
        alerts.send_alert(c, f"alert {i} card 4111 1111 1111 1111", env=env, post=hook, log=lambda *_: None)
        for i in range(25)
    ]
    assert results.count(True) == alerts.MAX_PER_HOUR and len(hook.sent) == alerts.MAX_PER_HOUR
    assert "4111 1111 1111 1111" not in hook.sent[0][1]


def test_alert_texts_carry_ids_and_amounts_but_not_customer_text():
    rec = {"tier": "senior", "amount_paise": 1_250_000, "payment_id": "pay_A002", "tenant": "m_acme", "source": SRC}
    t = alerts.approval_text(rec, "https://example.vercel.app/")
    assert "₹12,500.00" in t and "pay_A002" in t and t.endswith("https://example.vercel.app/app/approvals")
    assert "Guardrails blocked 2 attempt(s)" in alerts.block_text("event", "evt_1", ["a", "b"])


def test_queue_and_alert_sends_one_alert_per_new_approval_and_one_for_blocks(monkeypatch):
    c, _ = client()
    hook = Hook()
    monkeypatch.setenv("ALERT_WEBHOOK_URL", HOOK)
    args = (c, SRC, "m_acme", [DRAFT])
    assert queue_and_alert(*args, ["Tenant boundary"], post=hook, log=lambda *_: None) == 1
    assert len(hook.sent) == 2 and "Approval needed" in hook.sent[0][1] and "Tenant boundary" in hook.sent[1][1]
    assert queue_and_alert(*args, [], post=hook, log=lambda *_: None) == 0  # nothing new, nothing to say
    assert len(hook.sent) == 2


# ------------------------------------------------------------------ end to end through the real policy
def run_job(preset, tenant="m_acme", audit=None):
    c, fake = client()
    fake.kv[key_sim_job("job1")] = json.dumps(
        {"id": "job1", "kind": "preset", "preset": preset, "tenant": tenant, "state": "queued", "steps": []}
    )
    fake.lists[KEY_SIM_QUEUE] = ["job1"]
    state = process_job(c, lambda: None, SETTINGS, "job1", audit, daily_cap_usd=1, log=lambda *_: None)
    return state, fake, c


def test_a_legitimate_refund_in_the_simulator_lands_in_the_approvals_queue():
    state, fake, c = run_job("valid-refund")
    assert state == "done"
    [rec] = read_all(c)
    assert rec["state"] == "pending" and rec["source"]["kind"] == "sim" and rec["source"]["id"] == "job1"
    assert rec["tenant"] == "m_acme" and rec["amount_paise"] > 0 and rec["payment_id"].startswith("pay_")
    job = json.loads(fake.kv[key_sim_job("job1")])
    assert "drafts" not in job["runs"]["guarded"] and "drafts" not in job["runs"]["unguarded"]


def test_attacks_that_are_blocked_create_no_approval():
    for preset in ("cross-tenant", "over-refund", "prompt-override"):
        state, _, c = run_job(preset)
        assert state == "done" and read_all(c) == [], preset


# ------------------------------------------------------------------ decisions are recorded, the report published
def test_the_report_is_stored_in_three_formats_and_only_rebuilt_when_something_changed(tmp_path):
    from acp.assure import report

    c, fake = client()
    log = AuditLog(store.connect(tmp_path / "a.db"))
    log.append("m_acme", "run_start", {"prompt": "hi"}, "r1")
    assert report.publish_report(c, log) is True
    assert set(fake.kv) >= {report.KEY_REPORT + k for k in ("html", "json", "csv", "meta")}
    meta = json.loads(fake.kv[report.KEY_REPORT + "meta"])
    assert meta["ok"] and meta["entries_total"] == 1 and meta["head"] == log.head()
    assert fake.ttl[report.KEY_REPORT + "html"] == report.REPORT_TTL_S
    assert report.publish_report(c, log) is False  # nothing changed
    log.append("m_acme", "run_end", {"answer": "ok"}, "r1")
    assert (
        report.publish_report(c, log) is True and json.loads(fake.kv[report.KEY_REPORT + "meta"])["entries_total"] == 2
    )


def test_an_idle_worker_records_a_new_decision_in_the_audit_chain_and_refreshes_the_report(tmp_path):
    from acp.assure import ingest, report

    c, fake = client()
    log = AuditLog(store.connect(tmp_path / "a.db"))
    [rec] = publish_drafts(c, SRC, "m_acme", [DRAFT])
    ingest.drain(c, None, SETTINGS, log, once=True, poll_s=0, daily_cap_usd=1, log=lambda *_: None)
    before = json.loads(fake.kv[report.KEY_REPORT + "meta"])
    assert before["approvals"] == {"pending": 1} and before["entries_total"] == 0
    decide(fake, rec["id"], "approved", by="Priya", note="verified")
    ingest.drain(c, None, SETTINGS, log, once=True, poll_s=0, daily_cap_usd=1, log=lambda *_: None)
    after = json.loads(fake.kv[report.KEY_REPORT + "meta"])
    assert after["approvals"] == {"approved": 1} and after["entries_total"] == 1 and log.verify().ok
    assert "Priya" in fake.kv[report.KEY_REPORT + "html"]
    assert "approval_decided" in fake.kv[report.KEY_REPORT + "csv"]
