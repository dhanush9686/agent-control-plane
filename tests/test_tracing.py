import json

from acp import store
from acp.runner import Agent
from acp.tracing import Tracing
from tests.conftest import FakeLLM, FakeRetriever


def _ask(settings, sufficient=(True,)):
    tracing = Tracing(settings)
    agent = Agent(FakeLLM(list(sufficient)), FakeRetriever(), settings, tracing)
    final, run_id = agent.ask("who?")
    return final, run_id, store.connect(settings.db_path)


def test_exporter_writes_run_and_spans(settings):
    final, run_id, conn = _ask(settings)
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run["status"] == "ok" and run["question"] == "who?"
    assert run["llm_calls"] == 2
    assert run["input_tokens"] == 300 and run["output_tokens"] == 60  # judge 200/40 + synthesize 100/20
    names = {r["name"] for r in conn.execute("SELECT name FROM spans WHERE run_id=?", (run_id,))}
    assert {"agent.run", "node.retrieve", "node.judge", "node.synthesize", "llm.judge", "llm.synthesize"} <= names
    assert json.loads(run["retrieved_json"])


def test_llm_span_is_child_of_its_node_and_has_tokens_and_cost(settings):
    _, run_id, conn = _ask(settings)
    spans = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM spans WHERE run_id=?", (run_id,))}
    assert spans["llm.judge"]["parent_id"] == spans["node.judge"]["span_id"]
    assert spans["llm.judge"]["input_tokens"] == 200 and spans["llm.judge"]["output_tokens"] == 40
    assert spans["llm.synthesize"]["input_tokens"] == 100
    assert spans["agent.run"]["parent_id"] is None


def test_judge_verdict_is_recorded(settings):
    _, run_id, conn = _ask(settings)
    row = conn.execute("SELECT attributes FROM spans WHERE run_id=? AND name='node.judge'", (run_id,)).fetchone()
    assert json.loads(row["attributes"])["sufficient"] is True


def test_one_run_id_per_question(settings):
    tracing = Tracing(settings)
    agent = Agent(FakeLLM(), FakeRetriever(), settings, tracing)
    _, a = agent.ask("one")
    _, b = agent.ask("two")
    assert a != b
    conn = store.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_retry_attempts_show_up_as_extra_spans(settings):
    _, run_id, conn = _ask(settings, sufficient=(False,))
    n = conn.execute("SELECT COUNT(*) FROM spans WHERE run_id=? AND name='node.retrieve'", (run_id,)).fetchone()[0]
    assert n == settings.max_attempts


def test_cost_uses_price_table(settings):
    s = type(settings)(db_path=settings.db_path, prices={"fake": (1.0, 2.0)}, model="fake")
    assert s.cost_usd(1_000_000, 1_000_000) == 3.0
    assert s.cost_usd(10, 10) > 0


def test_tracing_failure_never_crashes_the_agent(settings, monkeypatch):
    tracing = Tracing(settings)
    agent = Agent(FakeLLM(), FakeRetriever(), settings, tracing)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store, "connect", boom)
    final, run_id = agent.ask("who?")  # exporter fails internally; answer still comes back
    assert final["answer"] and run_id


def test_batch_mode_writes_after_flush(settings):
    tracing = Tracing(settings, mode="batch")
    _, run_id = Agent(FakeLLM(), FakeRetriever(), settings, tracing).ask("who?")
    conn = store.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE run_id=?", (run_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM spans WHERE run_id=?", (run_id,)).fetchone()[0] >= 6


def test_unknown_trace_mode_is_rejected(settings):
    import pytest

    with pytest.raises(ValueError, match="unknown trace mode"):
        Tracing(settings, mode="carrier-pigeon")


def test_runs_are_tagged_with_user_and_document(settings):
    tracing = Tracing(settings)
    agent = Agent(FakeLLM(), FakeRetriever(), settings, tracing)
    _, run_id = agent.ask("who?", user_id="u" * 16, doc_id="d" * 16)
    row = (
        store.connect(settings.db_path).execute("SELECT user_id, doc_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    )
    assert tuple(row) == ("u" * 16, "d" * 16)
    _, plain = agent.ask("again")  # untagged runs (the CLI) still work
    assert (
        store.connect(settings.db_path).execute("SELECT user_id FROM runs WHERE run_id=?", (plain,)).fetchone()[0]
        is None
    )


def test_uploaded_document_text_is_not_kept_in_traces_by_default(settings):
    import json

    from langchain_core.documents import Document

    class DocRetr:
        def search(self, q, k):
            return [
                Document(
                    page_content="SECRET PRIVATE TEXT",
                    metadata={"source": "a.pdf", "citation": "a.pdf p.3", "page": 3, "chunk_id": 9},
                )
            ]

    agent = Agent(FakeLLM(), DocRetr(), settings, Tracing(settings))
    _, run_id = agent.ask("q")
    raw = (
        store.connect(settings.db_path)
        .execute("SELECT retrieved_json FROM runs WHERE run_id=?", (run_id,))
        .fetchone()[0]
    )
    assert "SECRET" not in raw and json.loads(raw)[0] == {
        "source": "a.pdf",
        "citation": "a.pdf p.3",
        "page": 3,
        "chunk_id": 9,
    }


def test_chunk_text_is_kept_when_explicitly_enabled(tmp_path):
    import json

    from langchain_core.documents import Document

    from acp.config import Settings

    s = Settings(db_path=tmp_path / "t.db", store_chunk_text=True)

    class DocRetr:
        def search(self, q, k):
            return [Document(page_content="VISIBLE TEXT", metadata={"source": "a.pdf", "citation": "a.pdf p.3"})]

    _, run_id = Agent(FakeLLM(), DocRetr(), s, Tracing(s)).ask("q")
    raw = store.connect(s.db_path).execute("SELECT retrieved_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    assert json.loads(raw)[0]["text"] == "VISIBLE TEXT"
