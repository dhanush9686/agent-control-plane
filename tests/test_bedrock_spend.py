import io
import json
import threading
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

from acp import store
from acp.config import Settings
from acp.embeddings import BedrockEmbedder, make_embedder
from acp.errors import ModelAccessError, RateLimited
from acp.llm import LLMResult
from acp.ratelimit import BudgetedLLM, ModelBudget
from tests.conftest import FakeLLM


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    store.connect(path).close()
    return lambda: store.connect(path)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


NOW = lambda: datetime(2026, 5, 1, 12, tzinfo=UTC)  # noqa: E731


# ------------------------------------------------------------------ USD cap
def test_spend_is_tracked_per_model_and_summed(db):
    b = ModelBudget(db, 100_000, 10**9, Clock(), NOW, usd_cap=1.0)
    b.settle("a", 0, 1000, 0.25)
    b.settle("b", 0, 500, 0.10)
    b.record("embed", 300, 0.05)
    assert b.spent_today_usd() == pytest.approx(0.40) and b.used_today("a") == 1000


def test_reaching_the_daily_dollar_cap_blocks_every_model_until_midnight(db):
    b = ModelBudget(db, 100_000, 10**9, Clock(), NOW, usd_cap=0.50)
    assert b.reserve("a", 100) == 0.0
    b.settle("a", 100, 100, 0.50)
    assert b.reserve("a", 100) == pytest.approx(12 * 3600, abs=1)  # noon -> midnight
    assert b.reserve("some-other-model", 100) > 0  # the cap is global, not per model


def test_no_cap_means_money_never_blocks(db):
    b = ModelBudget(db, 100_000, 10**9, Clock(), NOW, usd_cap=None)
    b.settle("a", 0, 10, 9999.0)
    assert b.reserve("a", 100) == 0.0


def test_a_new_day_resets_the_spend_total(db):
    now = [datetime(2026, 5, 1, 23, tzinfo=UTC)]
    b = ModelBudget(db, 100_000, 10**9, Clock(), lambda: now[0], usd_cap=0.1)
    b.settle("a", 0, 1, 0.1)
    assert b.reserve("a", 1) > 0
    now[0] = datetime(2026, 5, 2, 0, 5, tzinfo=UTC)
    assert b.reserve("a", 1) == 0.0


def test_negative_or_missing_costs_cannot_reduce_the_total(db):
    b = ModelBudget(db, 100_000, 10**9, Clock(), NOW, usd_cap=1.0)
    b.settle("a", 0, 10, 0.2)
    b.settle("a", 0, 10, -5.0)  # a bug upstream must not "refund" money
    assert b.spent_today_usd() == pytest.approx(0.2)


class Priced(FakeLLM):
    model = "apac.amazon.nova-micro-v1:0"

    def complete(self, prompt, *, purpose):
        return LLMResult("x", input_tokens=1_000_000, output_tokens=1_000_000, model=self.model)


def test_budgeted_llm_records_real_dollars_using_the_price_table(db):
    s = Settings()
    b = ModelBudget(db, 10**9, 10**12, Clock(), NOW, usd_cap=10.0)
    llm = BudgetedLLM({}, ("apac.amazon.nova-micro-v1:0",), lambda m: Priced(), b, s.cost_usd)
    llm.complete("hi", purpose="p")
    assert b.spent_today_usd() == pytest.approx(0.035 + 0.14)  # 1M in + 1M out at Nova Micro prices


def test_exhausted_dollar_budget_surfaces_as_rate_limited(db):
    s = Settings()
    b = ModelBudget(db, 10**9, 10**12, Clock(), NOW, usd_cap=0.05)
    llm = BudgetedLLM({}, ("apac.amazon.nova-micro-v1:0",), lambda m: Priced(), b, s.cost_usd)
    llm.complete("hi", purpose="p")  # spends $0.175, over the $0.05 cap
    with pytest.raises(RateLimited) as e:
        llm.complete("again", purpose="p")
    assert e.value.scope == "model_budget" and "spend" in str(e.value)


# ------------------------------------------------------------------ embedder
def titan_client(dim=1024, fail=None, tokens=5):
    class C:
        def __init__(self):
            self.bodies, self.lock = [], threading.Lock()
            self.failures = list(fail or [])

        def invoke_model(self, **kw):
            if self.failures:
                raise self.failures.pop(0)
            req = json.loads(kw["body"])
            with self.lock:
                self.bodies.append(req)
            # encode the text's length into the vector so tests can check ORDER is preserved
            vec = [float(len(req["inputText"]))] + [0.0] * (req["dimensions"] - 1)
            return {"body": io.BytesIO(json.dumps({"embedding": vec, "inputTextTokenCount": tokens}).encode())}

    return C()


def emb(client, **kw):
    return BedrockEmbedder(Settings(aws_region="ap-south-1", llm_max_retries=3, **kw), client=client)


def test_embeddings_keep_input_order_even_when_requests_run_concurrently():
    texts = ["x" * n for n in range(1, 60)]
    vecs = emb(titan_client()).embed(texts)
    assert [v[0] for v in vecs] == [float(len(t)) for t in texts] and len(vecs[0]) == 1024


def test_request_shape_asks_for_normalized_vectors_of_the_configured_size():
    client = titan_client()
    emb(client, embed_dimensions=512).embed(["hello"])
    assert client.bodies == [{"inputText": "hello", "dimensions": 512, "normalize": True}]
    assert emb(titan_client(), embed_dimensions=512).dim == 512


def test_unsupported_dimensions_are_rejected_up_front():
    with pytest.raises(ValueError, match="256, 512, 1024"):
        emb(titan_client(), embed_dimensions=384)


def test_usage_callback_receives_total_tokens_once_per_call():
    seen = []
    e = BedrockEmbedder(
        Settings(aws_region="ap-south-1"), client=titan_client(tokens=7), on_usage=lambda m, t: seen.append((m, t))
    )
    e.embed(["a", "b", "c"])
    assert seen == [("amazon.titan-embed-text-v2:0", 21)]


def test_empty_input_makes_no_calls_and_no_usage_event():
    client, seen = titan_client(), []
    assert BedrockEmbedder(Settings(), client=client, on_usage=lambda *a: seen.append(a)).embed([]) == [] and seen == []


def test_throttled_requests_are_retried(monkeypatch):
    monkeypatch.setattr("acp.llm.time.sleep", lambda s: None)
    err = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeModel")
    assert len(emb(titan_client(fail=[err, err])).embed(["a"])[0]) == 1024


def test_missing_embedding_model_access_is_a_clear_error():
    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "InvokeModel")
    with pytest.raises(ModelAccessError, match="Model access"):
        emb(titan_client(fail=[err] * 5)).embed(["a"])


def test_embedder_factory_and_pinecone_index_name(monkeypatch):
    with pytest.raises(ValueError, match="Unknown ACP_EMBEDDING_PROVIDER"):
        make_embedder(Settings(embedding_provider="nope"))
    from acp.vectorstore import INDEX_NAME_DEFAULT

    monkeypatch.delenv("PINECONE_INDEX", raising=False)
    created = {}

    class FakePC:
        def __init__(self, api_key):
            pass

        def has_index(self, name):
            created["name"] = name
            return True

        def describe_index(self, name):
            return type("D", (), {"status": {"ready": True}, "dimension": 1024, "host": "h"})()

        def Index(self, host):  # noqa: N802
            return object()

    import pinecone

    monkeypatch.setattr(pinecone, "Pinecone", FakePC)
    from acp.vectorstore import PineconeStore

    PineconeStore.connect(1024, api_key="k")
    assert created["name"] == f"{INDEX_NAME_DEFAULT}-1024"
