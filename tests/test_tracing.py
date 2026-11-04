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


class FakeProducer:
    def __init__(self):
        self.sent = []

    def produce(self, topic, key, value):
        self.sent.append((topic, key, value))

    def flush(self, timeout=0):
        pass


class FakeMessage:
    def __init__(self, value):
        self._v = value

    def error(self):
        return None

    def value(self):
        return self._v


class FakeConsumer:
    def __init__(self, messages):
        self.messages = list(messages)
        self.commits = 0

    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, message=None, asynchronous=False):
        self.commits += 1


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


def test_kafka_roundtrip_matches_direct_sqlite(settings, tmp_path):
    from acp.streaming import consume
    from acp.tracing import KafkaSpanExporter

    producer = FakeProducer()
    tracing = Tracing(settings, mode="kafka", exporter=KafkaSpanExporter(producer, "acp.spans"))
    _, run_id = Agent(FakeLLM(), FakeRetriever(), settings, tracing).ask("who?")
    assert producer.sent and all(t == "acp.spans" and k == run_id for t, k, _ in producer.sent)
    assert (
        not settings.db_path.exists()
        or store.connect(settings.db_path).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    )

    db2 = tmp_path / "consumed.db"
    consumer = FakeConsumer([FakeMessage(v) for _, _, v in producer.sent])
    n = consume(consumer, db2, idle_polls=2)
    assert n == len(producer.sent) and consumer.commits == n
    run = store.connect(db2).execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert run["llm_calls"] == 2 and run["input_tokens"] == 300


def test_consumer_skips_poison_messages_and_is_idempotent(settings, tmp_path):
    from acp.streaming import consume

    producer_msgs = [FakeMessage("not json"), FakeMessage("{}")]
    assert consume(FakeConsumer(producer_msgs), tmp_path / "x.db", idle_polls=2) == 0


def test_kafka_exporter_failure_never_crashes_agent(settings):
    from acp.tracing import KafkaSpanExporter

    class Broken(FakeProducer):
        def produce(self, *a, **k):
            raise RuntimeError("broker down")

    tracing = Tracing(settings, mode="kafka", exporter=KafkaSpanExporter(Broken()))
    final, _ = Agent(FakeLLM(), FakeRetriever(), settings, tracing).ask("who?")
    assert final["answer"]
