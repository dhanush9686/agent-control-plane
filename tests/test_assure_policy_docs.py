"""The cited rules library and the optional search_policy tool, offline."""

import yaml

from acp.assure import evaluate, policy_docs
from acp.assure.agent import AssuranceAgent
from acp.assure.replay import ScriptedModel
from acp.assure.sandbox import Ledger
from acp.assure.tools import OPTIONAL_TOOLS, TOOLS, ToolRuntime, tool_specs

NOTES = policy_docs.load_notes()


def test_every_note_is_complete_sourced_and_states_what_it_did_not_confirm():
    ids = [n["id"] for n in NOTES]
    assert len(ids) == len(set(ids)) >= 6
    for n in NOTES:
        assert all(n.get(f) not in (None, "", []) or f == "not_confirmed" for f in policy_docs.REQUIRED), n["id"]
        assert n["url"].startswith("https://www.rbi.org.in/") and n["verified_on"] == "2026-11-05"
        assert isinstance(n["not_confirmed"], list) and n["not_confirmed"], f"{n['id']} must say what was not confirmed"
        assert len(n["summary"].split()) <= 80, "notes are short summaries, not copies of the source"
        assert all(len(r.split()) <= 70 for r in n["rules"])
        assert all(k.endswith("_paise") and isinstance(v, int) and v > 0 for k, v in (n["amounts_paise"] or {}).items())


def top(q):
    r = policy_docs.search(q)
    return r[0]["id"] if r else None


def test_search_finds_the_right_note_and_returns_nothing_for_an_uncovered_question():
    assert (
        top("UPI payment debited but the beneficiary was not credited, when must it be reversed")
        == "rbi-tat-failed-transactions-2019"
    )
    assert (
        top("unauthorised transaction reported within three working days, customer liability")
        == "rbi-limited-liability-unauthorised-2017"
    )
    assert top("can a merchant store the full card number") == "rbi-card-data-storage"
    assert (
        top("refund to a different account than the original payment method")
        == "rbi-pa-directions-2025-refunds-disputes"
    )
    assert (
        top("how many days until settlement reaches the merchant, escrow") == "rbi-pa-directions-2025-escrow-settlement"
    )
    assert top("what is the weather in Mumbai") is None and top("the of and") is None
    assert len(policy_docs.search("payment aggregator", k=2)) <= 2


def test_the_settlement_note_says_it_has_no_fixed_number_of_days():
    note = next(n for n in NOTES if n["id"] == "rbi-pa-directions-2025-escrow-settlement")
    assert any("No fixed number of days" in x for x in note["not_confirmed"])


def test_the_tool_is_off_by_default_so_the_measured_benchmark_is_unchanged():
    assert "search_policy" not in {s["toolSpec"]["name"] for s in tool_specs()}
    assert "search_policy" in {s["toolSpec"]["name"] for s in tool_specs(policy_docs=True)}
    assert set(OPTIONAL_TOOLS).isdisjoint(TOOLS)
    off = ToolRuntime(Ledger(), "m_acme", "x", guarded=True)
    ev, _ = off.call("search_policy", {"query": "UPI reversal"})
    assert not ev.allowed and ev.code == "unknown_tool"
    on = ToolRuntime(Ledger(), "m_acme", "x", guarded=True, policy_docs=True)
    ev, payload = on.call("search_policy", {"query": "UPI reversal time limit"})
    assert ev.allowed and payload["count"] >= 1 and payload["results"][0]["id"] == "rbi-tat-failed-transactions-2019"
    bad, _ = on.call("search_policy", {"query": "x"})
    assert not bad.allowed and bad.code == "bad_arguments"


def run(script, guarded=True):
    agent = AssuranceAgent(ScriptedModel(script), Ledger(), guarded=guarded, policy_docs=True)
    return agent.run("A UPI payment was debited but not credited. What compensation applies?", "m_acme")


def test_a_rupee_figure_from_a_note_passes_the_numeric_guard_and_an_invented_one_is_withheld():
    ask = ("use", "search_policy", {"query": "UPI debited not credited reversal compensation"})
    right = run([ask, ("say", "Under RBI/2019-20/67 it is reversed by T+1, then ₹100 per day of delay.")])
    assert not right.withheld and "₹100" in right.answer
    wrong = run([ask, ("say", "Under RBI/2019-20/67 the compensation is ₹250 per day.")])
    assert (
        wrong.withheld and "250.00" in wrong.unsupported_raw
    )  # the guard checks regulation figures like ledger figures
    assert not run(
        [ask, ("say", "Reversed by T+1, then ₹100 per day.")], guarded=False
    ).withheld  # baseline has no guard


def test_the_system_prompt_only_mentions_the_tool_when_it_is_enabled():
    seen = {}

    class Spy(ScriptedModel):
        def tool_step(self, system, messages, tools):
            seen["system"], seen["tools"] = system, [t["toolSpec"]["name"] for t in tools]
            return super().tool_step(system, messages, tools)

    AssuranceAgent(Spy([("say", "ok")]), Ledger(), policy_docs=True).run("hi", "m_acme")
    assert "search_policy" in seen["system"] and "search_policy" in seen["tools"]
    AssuranceAgent(Spy([("say", "ok")]), Ledger()).run("hi", "m_acme")
    assert "search_policy" not in seen["system"] and "search_policy" not in seen["tools"]


def test_the_regulation_scenarios_are_well_formed_and_only_use_known_tools_and_checks():
    scs = evaluate.load_scenarios(evaluate.REGULATION)
    assert len(scs) >= 8 and len({s["id"] for s in scs}) == len(scs)
    allowed_checks = {
        "facts",
        "answer_any",
        "must_call",
        "no_refund",
        "no_pii",
        "numbers_supported",
        "no_foreign_data",
        "refund_requested",
    }
    for s in scs:
        assert s["category"] == "regulation" and set(s["checks"]) <= allowed_checks, s["id"]
        assert all(t in OPTIONAL_TOOLS or t in TOOLS for t in s["checks"].get("must_call", []))
    # every expected rupee figure really is in a note (or absent), so a scenario cannot demand something the notes lack
    figures = {f"{v / 100:.2f}" for n in NOTES for v in (n["amounts_paise"] or {}).values()}
    for s in scs:
        assert set(s["checks"].get("facts", [])) <= figures, s["id"]
    assert yaml.safe_load(policy_docs.NOTES_PATH.read_text())  # parses
