"""The CI and hosted-worker plumbing: no long-lived AWS keys, narrow permissions, a role that can invoke one model, and
a gate that fails on a regression. These are static checks of files that GitHub and AWS run elsewhere."""

import json
import re
from pathlib import Path

import pytest
import yaml

from acp.assure.evaluate import gate
from scripts import check_live_gate

ROOT = Path(__file__).resolve().parents[1]
WF = {p.stem: yaml.safe_load(p.read_text()) for p in (ROOT / ".github" / "workflows").glob("*.yml")}
TEXT = {p.stem: p.read_text() for p in (ROOT / ".github" / "workflows").glob("*.yml")}


def on(wf):  # PyYAML reads the bare key `on` as True
    return wf.get("on") or wf.get(True)


def test_no_workflow_uses_long_lived_aws_keys_or_echoes_a_secret():
    for name, text in TEXT.items():
        assert not re.search(r"AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)", text), name
        assert "echo ${{ secrets" not in text and 'echo "${{ secrets' not in text, name


def test_the_workers_assume_a_role_by_oidc_and_only_they_get_id_token_permission():
    for name in ("live-worker", "live-sync"):
        wf = WF[name]
        assert wf["permissions"] == {"contents": "read", "id-token": "write"}, name
        steps = wf["jobs"][next(iter(wf["jobs"]))]["steps"]
        aws = [s for s in steps if str(s.get("uses", "")).startswith("aws-actions/configure-aws-credentials")]
        assert len(aws) == 1 and "role-to-assume" in aws[0]["with"], name
        assert "AWS_ROLE_ARN" in aws[0]["with"]["role-to-assume"]
    assert "permissions" not in WF["ci"] or "id-token" not in str(
        WF["ci"].get("permissions")
    )  # CI never gets cloud access


def test_triggers_are_what_the_docs_say():
    w = on(WF["live-worker"])
    assert (
        set(w["repository_dispatch"]["types"]) == {"acp-event", "acp-sim"}
        and "schedule" in w
        and "workflow_dispatch" in w
    )
    assert WF["live-worker"]["concurrency"]["cancel-in-progress"] is False
    s = on(WF["live-sync"])
    assert s["schedule"][0]["cron"] == "0 3 * * 1" and "pull_request" not in s and "push" not in s  # never from a PR


def test_the_sync_gates_after_publishing_and_keeps_its_results_even_on_failure():
    steps = WF["live-sync"]["jobs"]["sync"]["steps"]
    runs = [s.get("run", "") for s in steps]
    sync_i = next(i for i, r in enumerate(runs) if "acp.assure.live" in r)
    gate_i = next(i for i, r in enumerate(runs) if "check_live_gate.py" in r)
    assert sync_i < gate_i and "--max-cost" in runs[sync_i]
    assert any(s.get("if") == "always()" and "upload-artifact" in str(s.get("uses")) for s in steps)


def test_the_worker_skips_everything_expensive_when_the_queue_is_empty():
    steps = WF["live-worker"]["jobs"]["work"]["steps"]
    after = steps[[s.get("id") for s in steps].index("queue") + 1 :]
    assert after and all("pending != '0'" in str(s.get("if")) for s in after)


@pytest.fixture(scope="module")
def role():
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor(
        "!",
        lambda loader, suffix, node: {
            suffix: loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode)
            else loader.construct_sequence(node)
        },
    )
    return yaml.load((ROOT / "infra" / "github-oidc-bedrock.yaml").read_text(), Loader=Loader)["Resources"][
        "WorkerRole"
    ]["Properties"]


def test_the_aws_role_trusts_only_this_repo_on_main_and_can_invoke_one_model(role):
    trust = role["AssumeRolePolicyDocument"]["Statement"][0]
    cond = trust["Condition"]["StringEquals"]
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert cond["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert "ref:refs/heads/" in str(cond["token.actions.githubusercontent.com:sub"]) and "*" not in str(cond)
    subs = cond["token.actions.githubusercontent.com:sub"]  # a plain value, or both forms behind an if
    forms = [v for v in str(subs).split("repo:")[1:]]
    assert forms and all(":ref:refs/heads/${Branch}" in f for f in forms)  # every accepted subject is the one branch
    # the id-pinned form GitHub now issues
    assert any("@${GitHubOwnerId}/${GitHubRepo}@${GitHubRepoId}" in f for f in forms)
    st = role["Policies"][0]["PolicyDocument"]["Statement"]
    assert len(st) == 1 and st[0]["Action"] == "bedrock:InvokeModel" and st[0]["Effect"] == "Allow"
    assert len(st[0]["Resource"]) == 2 and not any(r == "*" for r in st[0]["Resource"])


def good_payload(k=0, rate=0.9, aborted=False, errors=0):
    summary = {"guarded": {"critical_effective": {"k": k}, "pass": {"rate": rate}, "errors": errors}}
    return {
        "latest": {
            "aborted": aborted,
            "sources": {"razorpay-test": {"summary": summary}, "synthetic": {"summary": summary}},
        }
    }


def test_the_live_gate_fails_on_a_critical_outcome_a_low_pass_rate_errors_or_a_partial_sync():
    assert check_live_gate.check(good_payload()) == []
    assert any("critical" in p for p in check_live_gate.check(good_payload(k=1)))
    assert any("pass rate" in p for p in check_live_gate.check(good_payload(rate=0.5)))
    assert any("errored" in p for p in check_live_gate.check(good_payload(errors=2)))
    assert any("spend cap" in p for p in check_live_gate.check(good_payload(aborted=True)))
    assert (
        check_live_gate.main([str(ROOT / "results" / "live-latest.json")]) == 0
        if (ROOT / "results" / "live-latest.json").exists()
        else True
    )


def test_the_committed_benchmark_still_passes_the_gate_so_a_regression_cannot_be_committed_silently():
    saved = json.loads((ROOT / "dashboard" / "data" / "assure.json").read_text())["real_run"]
    assert gate({"summary": saved["summary"]}) == []
