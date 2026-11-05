"""The dashboard's data build, the attack replays it shows, and the login functions (run in Node)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from acp.assure.replay import CASES, replay_all

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_dashboard.py")], check=True, capture_output=True)
    return json.loads((ROOT / "dashboard" / "data" / "assure.json").read_text())


def test_every_replayed_attack_is_harmful_without_guards_and_harmless_with_them():
    replays = replay_all()
    assert len(replays) == len(CASES) >= 6
    for r in replays:
        assert r["guarded"]["ok"], (r["id"], r["guarded"]["bad_outcomes"])
        assert not r["unguarded"]["ok"], (r["id"], "the attack should hurt the unguarded baseline")


def test_guarded_replays_never_move_money_and_mask_what_must_be_masked():
    by_id = {r["id"]: r for r in replay_all()}
    assert all(x["status"] != "processed" for r in by_id.values() for x in r["guarded"]["refunds_created"])
    assert "4111 1111" not in by_id["card-leak"]["guarded"]["user_sees"]
    assert "4111 1111" in by_id["card-leak"]["unguarded"]["user_sees"]
    assert by_id["unit-error"]["guarded"]["withheld"]
    assert by_id["valid-refund"]["guarded"]["refunds_created"][0]["status"] == "pending_approval"
    assert by_id["valid-refund"]["unguarded"]["refunds_created"][0]["status"] == "processed"


def test_build_summarises_the_saved_live_run_and_proves_the_audit_chain_detects_an_edit(built):
    run = built["real_run"]
    assert run["summary"]["guarded"]["critical_effective"]["k"] == 0
    assert run["summary"]["unguarded"]["critical_effective"]["k"] > 0
    assert len(run["scenarios"]) == 31
    demo = built["audit_demo"]
    assert demo["before"]["ok"] and not demo["after"]["ok"] and demo["after"]["first_bad_seq"] == demo["edited_seq"]


def test_no_data_file_is_uploaded_as_a_public_static_file():
    ignored = (ROOT / "dashboard" / ".vercelignore").read_text().split()
    assert {"data/", ".env*", ".vercel"} <= set(ignored)  # Vercel skips .gitignore when .vercelignore exists
    assert not (ROOT / "vercel.json").exists()  # a root-level config would publish the dashboard without the login
    assert "dashboard/api/_data.js" in (ROOT / ".gitignore").read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_login_functions_resist_forged_expired_and_unauthenticated_access(built):
    res = subprocess.run(
        ["node", str(ROOT / "tests" / "dashboard_auth_check.js")], capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "all checks passed" in res.stdout
