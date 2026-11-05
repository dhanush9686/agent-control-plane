"""The compliance report: contents, the three formats, and the stand-alone verifier that proves it was not edited."""

import copy
import csv
import importlib.util
import io
import json

import pytest

from acp import store
from acp.assure import report as R
from acp.assure.audit import AuditLog
from acp.assure.sandbox import Ledger
from acp.config import ROOT
from examples.guard_quickstart import build


def verifier():
    spec = importlib.util.spec_from_file_location("verify_audit_export", ROOT / "scripts" / "verify_audit_export.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def audited(tmp_path):
    """A real log written by the guard: a blocked read, a held refund, a decision, an answer."""
    log = AuditLog(store.connect(tmp_path / "audit.db"))
    guard = build(Ledger())
    guard.audit = log
    s = guard.session("m_acme", "Refund pay_A001 for Rs 500.00")
    s.call("get_payment", payment_id="pay_Z001")
    aid = s.call("propose_refund", payment_id="pay_A001", amount_paise=50_000).approval.id
    guard.decide(aid, True, "Priya", "duplicate order")
    s.check_answer("Queued ₹500.00.")
    return log


def test_the_report_summarises_runs_blocks_and_approvals(audited):
    rep = R.build_report(audited, approvals=[{"id": "apr_1", "state": "approved"}, {"id": "apr_2", "state": "pending"}])
    s = rep["summary"]
    assert s["runs"] == 1 and s["tool_calls"] == 2 and s["blocked"] == 1 and s["allowed"] == 1
    assert s["blocked_by_reason"] == {"not_found": 1} and s["approvals"] == {"approved": 1, "pending": 1}
    assert rep["integrity"]["ok"] and rep["scope"]["entries_total"] == rep["scope"]["entries_listed"] == 7
    [b] = rep["blocked"]
    assert b["tool"] == "get_payment" and b["code"] == "not_found" and b["args"] == {"payment_id": "pay_Z001"}


def test_the_exported_json_verifies_with_the_stand_alone_script(audited):
    rep = R.build_report(audited)
    ok, msg = verifier().verify(json.loads(R.to_json(rep)))
    assert ok, msg


def test_the_verifier_names_an_edited_a_removed_and_a_reordered_entry(audited):
    v, rep = verifier(), json.loads(R.to_json(R.build_report(audited)))
    edited = copy.deepcopy(rep)
    edited["entries"][1]["detail"] = edited["entries"][1]["detail"].replace("pay_Z001", "pay_A001")
    ok, msg = v.verify(edited)
    assert not ok and "entry 2" in msg and "edited" in msg
    removed = copy.deepcopy(rep)
    del removed["entries"][2]
    ok, msg = v.verify(removed)
    assert not ok and "entry 4" in msg and "removed or reordered" in msg
    swapped = copy.deepcopy(rep)
    swapped["entries"][3], swapped["entries"][4] = swapped["entries"][4], swapped["entries"][3]
    assert not v.verify(swapped)[0]
    truncated = copy.deepcopy(rep)
    truncated["entries"] = truncated["entries"][:-1]
    truncated["scope"]["entries_listed"] = 6
    truncated["scope"]["entries_total"] = 6  # claims the tail is complete but the head does not match
    assert not v.verify(truncated)[0]


def test_a_report_listing_only_the_newest_entries_still_verifies_from_its_anchor(audited):
    rep = json.loads(R.to_json(R.build_report(audited, limit=3)))
    assert rep["scope"]["entries_listed"] == 3 and rep["scope"]["entries_total"] == 7
    assert rep["integrity"]["anchor_prev_hash"] == audited.entries()[-4]["hash"]
    assert verifier().verify(rep)[0]


def test_a_broken_chain_is_reported_as_failed_not_hidden(audited):
    audited._conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 3")
    audited._conn.commit()
    rep = R.build_report(audited)
    assert not rep["integrity"]["ok"] and rep["integrity"]["first_bad_seq"] == 3
    assert "FAILED at entry 3" in R.to_html(rep)
    assert not verifier().verify(json.loads(R.to_json(rep)))[0]


def test_csv_neutralises_spreadsheet_formulas_and_keeps_one_row_per_entry(audited):
    audited.append('=HYPERLINK("http://evil")', "tool_call", {"tool": "t", "allowed": True, "code": "ok"}, "r1")
    rep = R.build_report(audited)
    rows = list(csv.reader(io.StringIO(R.to_csv(rep))))
    assert rows[0][:4] == ["seq", "time_utc", "actor", "action"] and len(rows) == 1 + len(rep["entries"])
    actors = [r[2] for r in rows[1:]]
    assert '\'=HYPERLINK("http://evil")' in actors and not any(a.startswith("=") for a in actors)


def test_html_escapes_everything_it_prints_and_states_the_limits_and_how_to_verify(audited):
    audited.append(
        "<script>alert(1)</script>", "tool_call", {"tool": "<b>x</b>", "allowed": False, "code": "<i>"}, "r2"
    )
    page = R.to_html(R.build_report(audited, title="Q4 <review>"))
    assert "<script>" not in page and "&lt;script&gt;" in page and "Q4 &lt;review&gt;" in page
    assert "Integrity check passed" in page and "verify_audit_export.py" in page
    assert "tamper-evident, not tamper-proof" in page and audited.head() in page


def test_masked_data_never_appears_in_any_format(tmp_path):
    log = AuditLog(store.connect(tmp_path / "a.db"))
    log.append("x", "run_start", {"prompt": "card 4111 1111 1111 1111 and 98765 43210"}, "r")
    rep = R.build_report(log)
    for text in (R.to_json(rep), R.to_csv(rep), R.to_html(rep)):
        assert "4111 1111 1111 1111" not in text and "98765 43210" not in text


def test_an_empty_log_gives_a_valid_empty_report(tmp_path):
    log = AuditLog(store.connect(tmp_path / "e.db"))
    rep = R.build_report(log)
    assert rep["integrity"]["ok"] and rep["entries"] == [] and "The log is empty." in R.to_html(rep)
    assert verifier().verify(json.loads(R.to_json(rep)))[0]


def test_the_renderings_shrink_to_fit_the_storage_limit(tmp_path, monkeypatch):
    log = AuditLog(store.connect(tmp_path / "big.db"))
    for i in range(200):
        log.append(
            "a", "tool_call", {"tool": "get_payment", "allowed": True, "code": "ok", "n": "x" * 200 + str(i)}, f"r{i}"
        )
    monkeypatch.setattr(R, "MAX_BYTES", 60_000)
    rep, files = R.render_all(log, limit=200)
    assert rep["scope"]["entries_listed"] < 200 and all(len(v.encode()) <= 60_000 for v in files.values())
    assert verifier().verify(json.loads(files["json"]))[0]


def test_the_command_line_writes_a_file_and_fails_on_a_broken_chain(audited, tmp_path, capsys):
    db = str(tmp_path / "audit.db")
    out = tmp_path / "r.html"
    assert R.main(["--db", db, "--format", "html", "--out", str(out)]) == 0
    assert "Integrity check passed" in out.read_text() and "chain OK" in capsys.readouterr().out
    audited._conn.execute("UPDATE audit_log SET action = 'x' WHERE seq = 2")
    audited._conn.commit()
    assert R.main(["--db", db, "--format", "json", "--out", str(out)]) == 1
