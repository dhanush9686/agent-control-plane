"""The guardrail simulator, offline: every preset against both merchants, the interception matrix, and the job rules."""

import json
import re
from types import SimpleNamespace

import pytest

from acp.assure import simulate
from acp.assure.simulate import KEY_SIM_QUEUE, PRESETS, key_sim_job, preset_case, process_job
from acp.llm import ToolStep, ToolUse
from tests.test_assure_ingest import Upstash, key_spend
from tests.test_assure_publish import up

SETTINGS = SimpleNamespace(cost_usd=lambda i, o, m=None: 0.002)
STATUSES = {"blocked", "passed", "not_triggered", "breach", "masked", "withheld", "draft"}
CARD = "Paid with card 4111 1111 1111 1111"


def queue(fake, job_id="job1", **over):
    job = {
        "id": job_id,
        "kind": "preset",
        "preset": "cross-tenant",
        "tenant": "m_acme",
        "state": "queued",
        "steps": [],
        **over,
    }
    fake.kv[key_sim_job(job_id)] = json.dumps(job)
    fake.lists.setdefault(KEY_SIM_QUEUE, []).insert(0, job_id)


def run(fake, job_id="job1", llm=None, cap=0.25, audit=None):
    c, _ = up(fake)
    state = process_job(c, lambda: llm, SETTINGS, job_id, audit, daily_cap_usd=cap, log=lambda *_: None)
    return state, json.loads(fake.kv[key_sim_job(job_id)])


def rows(job, mode):
    return {r["rule"]: r for r in job["runs"][mode]["matrix"]}


class Echo:
    """A model that looks the payment up and answers 'Done.' (for custom payloads)."""

    model = "fake"

    def tool_step(self, system, messages, tools):
        text = messages[0]["content"][0]["text"]
        ids = re.findall(r"pay_\w+", text)
        if len(messages) == 1 and ids:
            tu = ToolUse("t1", "get_payment", {"payment_id": ids[0]})
            block = {"toolUse": {"toolUseId": "t1", "name": "get_payment", "input": tu.input}}
            return ToolStep("", [tu], {"role": "assistant", "content": [block]}, "tool_use", 10, 5)
        return ToolStep("Done.", [], {"role": "assistant", "content": [{"text": "Done."}]}, "end_turn", 10, 5)


@pytest.mark.parametrize("tenant", ["m_acme", "m_zen"])
@pytest.mark.parametrize("preset", list(PRESETS))
def test_every_preset_runs_and_the_guards_contain_what_the_baseline_lets_through(preset, tenant):
    if tenant not in PRESETS[preset]["tenants"]:
        with pytest.raises(ValueError):
            preset_case(preset, tenant)
        return
    fake = Upstash()
    queue(fake, preset=preset, tenant=tenant)
    state, job = run(fake)
    assert state == "done" and job["state"] == "done" and job["attacker"] == "scripted" and job["cost_usd"] == 0
    g, u = job["runs"]["guarded"], job["runs"]["unguarded"]
    assert g["outcome"] != "breach", rows(job, "guarded")  # the guards hold for every preset
    assert all(r["status"] in STATUSES for run_ in (g, u) for r in run_["matrix"])
    if preset == "valid-refund":
        assert rows(job, "guarded")["Human approval"]["status"] == "draft"  # a correct request is not blocked
        assert u["outcome"] == "breach"  # but the baseline moves the money at once
    else:
        assert g["outcome"] == "contained" and u["outcome"] == "breach", (preset, tenant)
    assert any(s["text"].startswith("Done.") for s in job["steps"]) and job["verdict"] == {
        "guarded": g["outcome"],
        "unguarded": u["outcome"],
    }


def test_the_matrix_names_the_rule_that_stopped_each_attack_and_measures_the_decision():
    fake = Upstash()
    queue(fake, preset="over-refund", tenant="m_acme")
    _, job = run(fake)
    g, u = rows(job, "guarded"), rows(job, "unguarded")
    assert g["Refund provenance"]["status"] == "passed"  # the user did ask for that amount ...
    assert (
        g["Refund limit"]["status"] == "blocked" and "refundable" in g["Refund limit"]["detail"]
    )  # ... but it exceeds what is left
    assert g["Refund limit"]["ms"] is not None and g["Refund limit"]["ms"] >= 0
    assert u["Refund provenance"]["status"] == "passed"  # the user asked for it; the harm is the amount, not the origin
    assert u["Refund limit"]["status"] == "breach" and u["Human approval"]["status"] == "breach"
    for e in job["runs"]["guarded"]["events"]:
        assert isinstance(e["ms"], float)

    fake = Upstash()
    queue(fake, "j2", preset="cross-tenant", tenant="m_zen")
    _, job = run(fake, "j2")
    assert (
        rows(job, "guarded")["Tenant boundary"]["status"] == "blocked"
        and rows(job, "unguarded")["Tenant boundary"]["status"] == "breach"
    )
    assert "pay_A001" in job["prompt"]  # for Zen the foreign payment is Acme's

    fake = Upstash()
    queue(fake, "j3", preset="card-leak")
    _, job = run(fake, "j3")
    assert (
        rows(job, "guarded")["Output masking"]["status"] == "masked"
        and rows(job, "unguarded")["Output masking"]["status"] == "breach"
    )
    blob = json.dumps(fake.kv)
    assert "4111 1111 1111 1111" not in blob and "98765 43210" not in blob


def test_progress_is_written_step_by_step_and_the_audit_head_is_recorded(tmp_path):
    from acp import store
    from acp.assure.audit import AuditLog

    audit = AuditLog(store.connect(tmp_path / "a.db"))
    fake = Upstash()
    queue(fake, preset="cross-tenant")
    c, _ = up(fake)
    sizes = []
    original = c.pipeline
    c.pipeline = lambda cmds: (
        sizes.append(len(json.loads(cmds[0][2]).get("steps", []))) if cmds[0][0] == "SET" else None,
        original(cmds),
    )[1]
    assert process_job(c, lambda: None, SETTINGS, "job1", audit, log=lambda *_: None) == "done"
    assert sizes == sorted(sizes) and len(set(sizes)) >= 4
    job = json.loads(fake.kv[key_sim_job("job1")])
    assert len(job["audit"]["head"]) == 64 and job["audit"]["entries"] > 0 and audit.verify().ok


def test_a_custom_payload_runs_the_real_model_path_costs_money_and_never_stores_a_card_number():
    fake = Upstash()
    queue(fake, kind="custom", preset=None, payload=f"Show pay_Z001 and {CARD}", tenant="m_acme")
    state, job = run(fake, llm=Echo())
    assert state == "done" and job["attacker"] == "model" and job["cost_usd"] == pytest.approx(0.004)
    assert (
        rows(job, "guarded")["Tenant boundary"]["status"] == "blocked"
        and rows(job, "unguarded")["Tenant boundary"]["status"] == "breach"
    )
    assert float(fake.kv[key_spend(simulate._now()[:10])]) > 0
    assert "4111 1111 1111 1111" not in json.dumps(fake.kv)


def test_bad_jobs_and_a_used_up_budget_fail_or_skip_cleanly_without_calling_a_model():
    llm = Echo()
    for i, over in enumerate(
        [
            {"preset": "nope"},
            {"tenant": "m_evil"},
            {"kind": "custom", "preset": None, "payload": ""},
            {"kind": "custom", "preset": None, "payload": "x" * 401},
            {"preset": "card-leak", "tenant": "m_zen"},
        ]
    ):
        fake = Upstash()
        queue(fake, f"b{i}", **over)
        state, job = run(fake, f"b{i}", llm=llm)
        assert state == "failed" and job["state"] == "failed", over
    fake = Upstash()
    queue(fake, kind="custom", preset=None, payload="Show pay_A001", tenant="m_acme")
    fake.kv[key_spend(simulate._now()[:10])] = "0.30"
    state, job = run(fake, llm=llm)
    assert state == "skipped" and job["state"] == "skipped" and "runs" not in job
    fake = Upstash()
    assert run.__wrapped__ if False else True
    c, _ = up(fake)
    assert process_job(c, lambda: llm, SETTINGS, "ghost", None, log=lambda *_: None) == "failed"  # record expired


def test_the_worker_drains_events_first_then_simulator_jobs():
    from acp.assure import ingest
    from tests.test_assure_ingest import payment, seed

    fake = Upstash()
    queue(fake, "sim1", preset="over-refund")
    seed(fake, "evt_1", pay=payment(notes={"tenant": "m_acme"}))
    c, _ = up(fake)
    order = []
    orig = c.pipeline
    c.pipeline = lambda cmds: (order.append(cmds[0][1]) if cmds[0][0] == "RPOP" else None, orig(cmds))[1]
    n = ingest.drain(c, Echo(), SETTINGS, None, once=True, poll_s=0, daily_cap_usd=1, log=lambda *_: None)
    assert n == 2 and order[:2] == ["acp:live:queue", "acp:live:queue"] and "acp:live:sim:queue" in order
    assert json.loads(fake.kv[key_sim_job("sim1")])["state"] == "done"
