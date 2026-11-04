import json

import pytest

from acp import store
from acp.evals import checks
from acp.evals.golden import GoldenCase, load_golden
from acp.evals.grounding import ClaimVerdict, GroundingReport, evaluate_grounding
from acp.evals.report import aggregate
from acp.evals.runner import run_eval
from acp.export import build_export
from acp.runner import Agent
from acp.tracing import Tracing
from tests.conftest import CORPUS, FakeLLM, FakeRetriever


def test_golden_set_is_valid_and_covers_all_categories():
    cases = load_golden()
    assert 15 <= len(cases) <= 25
    assert {c.category for c in cases} == {
        "answerable",
        "multi_hop",
        "thin_data",
        "contradiction_trap",
        "unanswerable",
        "prompt_injection",
    }


def test_word_aware_matching():
    assert checks.contains("No, they differ.", "no")
    assert not checks.contains("I know that", "no")
    assert checks.contains("Jr NTR’s Luger", "luger")


def test_refusal_detection():
    assert checks.is_refusal("That is not in the knowledge base. Other text.")
    assert not checks.is_refusal("Rocky kills Garuda.")


def test_citation_check():
    ok, _ = checks.citation_check("Fact [KGF 1]", {"KGF 1"})
    assert ok
    ok, problems = checks.citation_check("Fact without citation", {"KGF 1"})
    assert not ok and "no citation" in problems
    ok, problems = checks.citation_check("Fact [Made Up]", {"KGF 1"})
    assert not ok
    ok, _ = checks.citation_check("That is not in the knowledge base.", set())
    assert ok  # refusals need no citation


def test_behavior_check():
    answer_case = GoldenCase("a", "answerable", "q", "answer", [["Telugu"]], ["banana"])
    assert checks.behavior_check(answer_case, "It is Telugu [S]") == []
    assert checks.behavior_check(answer_case, "That is not in the knowledge base.")
    assert checks.behavior_check(answer_case, "Telugu banana")
    refuse_case = GoldenCase("r", "unanswerable", "q", "refuse")
    assert checks.behavior_check(refuse_case, "It is Prabhas") != []
    assert checks.behavior_check(refuse_case, "That is not in the knowledge base.") == []


def _report(*claims):
    return GroundingReport(claims=[ClaimVerdict(claim=c, supported=s, evidence="e" if s else "") for c, s in claims])


def test_grounding_flags_unsupported_claims():
    llm = FakeLLM()
    llm.grounding_report = _report(("Rocky kills Garuda", True), ("Rocky is a pirate", False))
    r = evaluate_grounding(llm, "q", "a", CORPUS)
    assert r.hallucinated and r.score == 0.5 and r.unsupported[0].claim == "Rocky is a pirate"


def test_grounding_all_supported_and_empty():
    llm = FakeLLM()
    llm.grounding_report = _report(("x", True))
    assert evaluate_grounding(llm, "q", "a", CORPUS).score == 1.0
    llm.grounding_report = _report()
    r = evaluate_grounding(llm, "q", "a", CORPUS)
    assert r.score == 1.0 and not r.hallucinated


def test_grounding_ignores_refusal_sentences_the_judge_lists_as_claims():
    llm = FakeLLM()
    llm.grounding_report = _report(("That is not in the knowledge base.", False))
    assert not evaluate_grounding(llm, "q", "a", CORPUS).hallucinated


def _result(**kw):
    base = dict(
        category="answerable",
        expect="answer",
        refused=False,
        behavior_failures=[],
        hallucinated=False,
        citation_ok=True,
        passed=True,
        grounding_score=1.0,
        latency_ms=100.0,
        cost_usd=0.001,
        error=None,
    )
    return {**base, **kw}


def test_aggregate_rates():
    results = [
        _result(),
        _result(hallucinated=True, passed=False),
        _result(category="unanswerable", expect="refuse", refused=True),
        _result(category="unanswerable", expect="refuse", refused=False, passed=False),
    ]
    s = aggregate(results)
    assert s["answered_correctly_rate"] == 1.0
    assert s["hallucination_rate"] == round(1 / 3, 4)
    assert s["refusal_correctness"] == 0.75
    assert s["pass_rate"] == 0.5
    assert s["per_category"]["unanswerable"]["n"] == 2


def test_end_to_end_eval_then_export(settings, tmp_path):
    tracing = Tracing(settings)
    agent = Agent(FakeLLM(answer="Prashanth Neel directed it [KGF 1]"), FakeRetriever(), settings, tracing)
    judge = FakeLLM()
    judge.grounding_report = _report(("Neel directed it", True))
    cases = [GoldenCase("t1", "answerable", "Who directed it?", "answer", [["Prashanth Neel"]])]
    report = run_eval(agent, judge, cases, settings, report_path=tmp_path / "r.json", progress=lambda *_: None)
    assert report["results"][0]["passed"] and report["summary"]["pass_rate"] == 1.0
    assert json.loads((tmp_path / "r.json").read_text())["eval_id"] == report["eval_id"]
    conn = store.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0] == 1
    data = build_export(conn, min_cases=1)
    latest = data["evals"][0]
    assert latest["runs"][0]["spans"] and latest["runs"][0]["retrieved"]
    assert latest["summary"]["n_cases"] == 1
    assert "GROQ" not in json.dumps(data)


def test_export_without_eval_raises(settings):
    with pytest.raises(ValueError):
        build_export(store.connect(settings.db_path))
