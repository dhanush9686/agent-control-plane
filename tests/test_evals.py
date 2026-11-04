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
    assert 50 <= len(cases) <= 70
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
    from acp.export import lineage_cypher, strip_private

    assert "MERGE (n:Question" in lineage_cypher(data)
    assert "_graph" not in strip_private(data)["evals"][0]
    assert latest["doc_stats"]
    assert "GROQ" not in json.dumps(strip_private(data))


def test_export_without_eval_raises(settings):
    with pytest.raises(ValueError):
        build_export(store.connect(settings.db_path))


def test_wilson_interval_behaves():
    from acp.evals.report import wilson

    lo, hi = wilson(19, 19)
    assert hi == 1.0 and 0.8 < lo < 0.9  # perfect score on 19 questions still allows ~83%
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi < 0.35
    assert wilson(0, 0) is None
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi


def test_aggregate_includes_confidence_intervals():
    s = aggregate([_result(), _result(passed=False, hallucinated=True)])
    assert s["counts"]["pass_rate"] == {"k": 1, "n": 2}
    lo, hi = s["ci95"]["pass_rate"]
    assert lo < 0.5 < hi


def test_judge_labels_are_valid_and_match_corpus():
    from acp.config import get_settings
    from acp.evals.judge_check import context_for, load_labels
    from acp.index import load_and_chunk

    items = load_labels()
    chunks = load_and_chunk(get_settings().corpus_path)
    assert len(items) >= 30 and len({i["id"] for i in items}) == len(items)
    assert any(i["unsupported"] for i in items) and any(not i["unsupported"] for i in items)
    for i in items:
        assert context_for(i["sources"], chunks)


def test_judge_check_scoring_and_run_with_fake_judge():
    from acp.evals.judge_check import run_judge_check, score

    rows = [
        {"label": True, "predicted": True, "kind": "a"},
        {"label": False, "predicted": True, "kind": "a"},
        {"label": True, "predicted": False, "kind": "b"},
        {"label": False, "predicted": False, "kind": "b"},
    ]
    s = score(rows)
    assert s["accuracy"] == 0.5 and s["precision"] == 0.5 and s["recall"] == 0.5
    judge = FakeLLM()
    judge.grounding_report = _report(("claim", False))  # always flags a hallucination
    items = [
        {"id": "x", "kind": "k", "sources": ["Inception"], "unsupported": True, "answer": "a", "labelled_by": "author"}
    ]
    from acp.config import get_settings
    from acp.index import load_and_chunk

    out = run_judge_check(judge, items, load_and_chunk(get_settings().corpus_path), progress=lambda *_: None)
    assert out["accuracy"] == 1.0 and out["confusion"]["tp"] == 1


def test_sign_test_and_compare():
    from acp.evals.compare import compare, sign_test_p

    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(10, 0) < 0.01
    assert sign_test_p(5, 5) == 1.0

    def ev(eid, agent, passes):
        res = {
            f"c{i}": {
                "passed": p,
                "category": "x",
                "expect": "answer",
                "refused": False,
                "behavior_failures": [],
                "hallucinated": False,
                "citation_ok": True,
                "grounding_score": 1.0,
                "latency_ms": 10.0,
                "cost_usd": 0.0,
                "error": None,
                "llm_calls": 2,
            }
            for i, p in enumerate(passes)
        }
        return {"eval_id": eid, "summary": {"agent": agent, "agent_model": "m"}, "results": res}

    out = compare(ev("a", "graph", [True, True, True, False]), ev("b", "naive", [True, False, False, False]))
    assert out["only_a_passes"] == ["c1", "c2"] and out["only_b_passes"] == []
    assert out["n_shared"] == 4 and out["a"]["avg_llm_calls"] == 2


def test_history_append_is_idempotent_and_skips_smoke_runs(tmp_path):
    from acp import history

    path = tmp_path / "h.jsonl"
    full = history.entry_from_eval(
        "e1", "2026-01-01T00:00:00", "m", {"n_cases": 58, "pass_rate": 0.9, "agent": "graph"}
    )
    smoke = history.entry_from_eval("e2", "2026-01-02T00:00:00", "m", {"n_cases": 3, "pass_rate": 1.0})
    assert history.append_missing([full, smoke], path) == 1
    assert history.append_missing([full], path) == 0
    loaded = history.load(path)
    assert [e["eval_id"] for e in loaded] == ["e1"] and loaded[0]["pass_rate"] == 0.9


def test_run_eval_appends_history(settings, tmp_path):
    from acp import history

    agent = Agent(FakeLLM(answer="Prashanth Neel directed it [KGF 1]"), FakeRetriever(), settings, Tracing(settings))
    judge = FakeLLM()
    cases = [GoldenCase(f"t{i}", "answerable", "q", "answer", [["Prashanth Neel"]]) for i in range(10)]
    hp = tmp_path / "hist.jsonl"
    run_eval(agent, judge, cases, settings, report_path=tmp_path / "r.json", progress=lambda *_: None, history_path=hp)
    assert len(history.load(hp)) == 1
