"""Offline tests for the public-dataset converters, using tiny rows shaped like the real ones."""

import json

from acp.benchmarks import build_squad_kb, fever_items, ragtruth_items
from acp.evals.golden import GoldenCase
from acp.evals.judge_check import context_for, load_labels, run_judge_check
from tests.conftest import FakeLLM


def _sq(title, ctx, q, answers):
    return {
        "title": title,
        "context": ctx,
        "question": q,
        "answers": {"text": answers, "answer_start": [0] * len(answers)},
    }


SQUAD_ROWS = [
    _sq("Normans", "The Normans lived in\nNormandy, France.", "Where is Normandy?", ["France", "France"]),
    _sq("Normans", "The Normans lived in\nNormandy, France.", "Who lived on Mars?", []),
    _sq("Rome", "Rome is the capital of Italy.", "Capital of Italy?", ["Rome"]),
    _sq("Rome", "Rome is the capital of Italy.", "Capital of Spain?", []),
    _sq("Lonely", "Only answerable question here.", "Q?", ["x"]),  # no unanswerable sibling -> not paired
]


def test_squad_kb_pairs_answerable_and_unanswerable():
    corpus, cases = build_squad_kb(SQUAD_ROWS, n_questions=4, seed=1)
    assert len(cases) == 4
    assert sorted(c["expect"] for c in cases) == ["answer", "answer", "refuse", "refuse"]
    answerable = [c for c in cases if c["expect"] == "answer"]
    assert all(c["must_include"] and c["must_include"][0] for c in answerable)
    # every paragraph (even the unpaired one) lands in the KB, newlines flattened, one header per article
    assert "The Normans lived in Normandy, France." in corpus and "Only answerable question here." in corpus
    assert corpus.count("=== ") == 3
    # the generated golden cases are valid GoldenCase objects
    for c in cases:
        GoldenCase(**c)


def test_squad_kb_is_deterministic():
    assert build_squad_kb(SQUAD_ROWS, 4, seed=3) == build_squad_kb(SQUAD_ROWS, 4, seed=3)


def _rt(i, labels, task="QA", ctx="passage", out="answer"):
    return {
        "id": str(i),
        "task_type": task,
        "context": ctx,
        "output": out,
        "query": "q",
        "hallucination_labels": json.dumps(labels),
    }


def test_ragtruth_items_are_balanced_and_filtered():
    rows = [
        _rt(1, [{"text": "x"}]),
        _rt(2, []),
        _rt(3, [{"text": "y"}]),
        _rt(4, []),
        _rt(5, [], task="Summary"),
        _rt(6, [], ctx="c" * 5000),
    ]
    items = ragtruth_items(rows, per_class=1)
    assert len(items) == 2 and {i["unsupported"] for i in items} == {True, False}
    assert all(i["dataset"] == "RAGTruth" and "human" in i["labelled_by"] for i in items)
    ids = {i["id"] for i in ragtruth_items(rows, per_class=5)}
    assert "rt-5" not in ids and "rt-6" not in ids  # non-QA and over-long rows dropped


def _fv(cid, label, claim="claim", evidence="evidence"):
    return {"cid": cid, "premise": claim, "hypothesis": evidence, "fever_gold_label": label}


def test_fever_items_claim_is_premise_and_labels_map_correctly():
    rows = [
        _fv(1, "SUPPORTS", "C1", "E1"),
        _fv(2, "REFUTES", "C2", "E2"),
        _fv(3, "NOT ENOUGH INFO", "C3", "E3"),
        _fv(4, "SUPPORTS"),
    ]
    items = {i["id"]: i for i in fever_items(rows, per_class=5)}
    sup = items["fv-1-SUP"]
    assert sup["answer"] == "C1" and sup["context"] == "E1" and sup["unsupported"] is False
    assert items["fv-2-REF"]["unsupported"] is True and items["fv-3-NOT"]["unsupported"] is True


def test_external_labels_load_and_run_through_the_grader(tmp_path):
    path = tmp_path / "ext.jsonl"
    item = {
        "id": "x1",
        "dataset": "FEVER",
        "kind": "supported",
        "context": "The sky is blue.",
        "answer": "The sky is blue.",
        "unsupported": False,
        "labelled_by": "human annotators (FEVER)",
    }
    path.write_text(json.dumps(item) + "\n")
    items = load_labels([path])
    assert items[0]["dataset"] == "FEVER"
    assert context_for(items[0], [])[0].page_content == "The sky is blue."
    judge = FakeLLM()  # extracts no claims -> never flags a hallucination -> agrees with label "supported"
    out = run_judge_check(judge, items, [], progress=lambda *_: None)
    assert out["accuracy"] == 1.0 and out["by_dataset"]["FEVER"]["n"] == 1
