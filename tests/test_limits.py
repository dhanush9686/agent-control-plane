import threading
from datetime import UTC, datetime

import pytest
from langchain_core.documents import Document

from acp import store
from acp.budget import OVERHEAD_TOKENS, estimate_tokens, fit_documents
from acp.errors import LLMUnavailableError, RateLimited
from acp.llm import LLMResult
from acp.ratelimit import BudgetedLLM, DailyQuota, ModelBudget, SlidingWindow
from tests.conftest import FakeLLM


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


# ------------------------------------------------------------------ context budget
def _doc(text, cite="f.pdf p.1"):
    return Document(page_content=text, metadata={"citation": cite})


def test_estimate_is_pessimistic_and_monotonic():
    assert estimate_tokens("") == 0 and estimate_tokens("a" * 350) == 100
    assert estimate_tokens("a" * 7000) > estimate_tokens("a" * 3500)


def test_budget_keeps_best_chunks_first_and_drops_the_tail():
    docs = [_doc(f"chunk {i} " + "word " * 150, f"f.pdf p.{i}") for i in range(20)]
    kept, dropped = fit_documents(docs, budget_tokens=OVERHEAD_TOKENS + 1000)
    assert 1 <= len(kept) < 20 and dropped == 20 - len(kept)
    assert [d.metadata["citation"] for d in kept] == [f"f.pdf p.{i}" for i in range(len(kept))]  # a prefix, in order
    total = sum(estimate_tokens(d.page_content) for d in kept)
    assert total <= 1000


def test_long_chunks_are_truncated_not_dropped():
    kept, dropped = fit_documents([_doc("x " * 5000)], budget_tokens=3500)
    assert dropped == 0 and len(kept[0].page_content) <= 1004 and kept[0].page_content.endswith("...")


def test_one_chunk_is_always_kept_even_if_it_alone_exceeds_the_budget():
    kept, dropped = fit_documents([_doc("word " * 100)], budget_tokens=10)
    assert len(kept) == 1 and dropped == 0


def test_budget_does_not_mutate_the_original_documents():
    original = _doc("y " * 3000)
    fit_documents([original], 3500)
    assert len(original.page_content) == 6000


# ------------------------------------------------------------------ sliding window
def test_sliding_window_allows_limit_then_blocks_with_retry_after():
    clock = Clock()
    w = SlidingWindow(3, 60.0, clock)
    assert [w.hit("u") for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = w.hit("u")
    assert 59.0 <= wait <= 60.0
    clock.advance(30)
    assert 29.0 <= w.hit("u") <= 30.0
    clock.advance(31)  # the first events have aged out
    assert w.hit("u") == 0.0


def test_sliding_window_isolates_users():
    w = SlidingWindow(1, 60.0, Clock())
    assert w.hit("alice") == 0.0 and w.hit("bob") == 0.0 and w.hit("alice") > 0


# ------------------------------------------------------------------ daily quota
@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    store.connect(path).close()
    return lambda: store.connect(path)


def test_daily_quota_counts_then_refuses_until_midnight(db):
    now = [datetime(2026, 5, 1, 22, 0, tzinfo=UTC)]
    q = DailyQuota(db, lambda: now[0])
    assert [q.consume_ask("u", 3) for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = q.consume_ask("u", 3)
    assert wait == pytest.approx(2 * 3600, abs=1)  # 22:00 -> midnight
    assert q.remaining("u", 3, 2) == {"asks": 0, "uploads": 2}
    now[0] = datetime(2026, 5, 2, 0, 0, 1, tzinfo=UTC)  # a new UTC day starts fresh
    assert q.consume_ask("u", 3) == 0.0


def test_daily_quota_is_atomic_under_concurrency(db):
    q = DailyQuota(db, lambda: datetime(2026, 5, 1, 12, tzinfo=UTC))
    allowed, lock = [], threading.Lock()

    def worker():
        r = q.consume_ask("u", 5)
        with lock:
            allowed.append(r == 0.0)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(allowed) == 5  # exactly the limit, never more, even with 30 simultaneous requests


def test_zero_limit_blocks_everything_and_uploads_are_separate(db):
    q = DailyQuota(db, lambda: datetime(2026, 5, 1, 12, tzinfo=UTC))
    assert q.consume_ask("u", 0) > 0
    assert q.consume_upload("u", 1) == 0.0 and q.consume_upload("u", 1) > 0 and q.consume_ask("u", 1) == 0.0


# ------------------------------------------------------------------ model budget
def _budget(db, clock, tpm=6000, daily=100_000, now=None):
    return ModelBudget(db, tpm, daily, clock, now or (lambda: datetime(2026, 5, 1, 12, tzinfo=UTC)))


def test_bucket_drains_then_refills_over_time(db):
    clock = Clock()
    b = _budget(db, clock)
    assert b.reserve("m", 4000) == 0.0
    wait = b.reserve("m", 4000)  # only 2000 left; need 2000 more at 100 tokens/s
    assert wait == pytest.approx(20.0, abs=0.1)
    clock.advance(20)
    assert b.reserve("m", 4000) == 0.0


def test_models_have_separate_buckets(db):
    b = _budget(db, Clock())
    assert b.reserve("a", 6000) == 0.0 and b.reserve("b", 6000) == 0.0 and b.reserve("a", 100) > 0


def test_settle_refunds_the_over_reservation(db):
    b = _budget(db, Clock())
    assert b.reserve("m", 5000) == 0.0
    b.settle("m", reserved=5000, actual=1000)  # we only used 1000 of the 5000 reserved
    assert b.reserve("m", 5000) == 0.0  # so 5000 is available again
    assert b.used_today("m") == 1000


def test_daily_cap_blocks_until_midnight_and_persists(db):
    now = [datetime(2026, 5, 1, 23, 0, tzinfo=UTC)]
    b = _budget(db, Clock(), daily=3000, now=lambda: now[0])
    b.settle("m", 0, 2900)
    assert b.reserve("m", 500) == pytest.approx(3600, abs=1)
    fresh = _budget(db, Clock(), daily=3000, now=lambda: now[0])  # a "restarted" process sees the same day total
    assert fresh.reserve("m", 500) > 0
    now[0] = datetime(2026, 5, 2, 0, 1, tzinfo=UTC)
    assert fresh.reserve("m", 500) == 0.0


def test_a_request_larger_than_the_whole_bucket_is_still_admitted_when_full(db):
    assert _budget(db, Clock(), tpm=2000).reserve("m", 50_000) == 0.0


def test_refund_returns_tokens(db):
    b = _budget(db, Clock())
    b.reserve("m", 6000)
    b.refund("m", 6000)
    assert b.reserve("m", 6000) == 0.0


# ------------------------------------------------------------------ fallback chain
class NamedLLM(FakeLLM):
    def __init__(self, name, fail=False, **kw):
        super().__init__(**kw)
        self.model, self.fail = name, fail

    def complete(self, prompt, *, purpose):
        if self.fail:
            raise LLMUnavailableError("down")
        res = super().complete(prompt, purpose=purpose)
        res.model = self.model
        return res

    def structured(self, schema, prompt, *, purpose):
        res = super().structured(schema, prompt, purpose=purpose)
        res.model = self.model
        return res


def _chain(db, llms, tpm=6000, daily=100_000, clock=None):
    b = _budget(db, clock or Clock(), tpm=tpm, daily=daily)
    return BudgetedLLM({"synthesize": ("big", "small")}, ("small",), lambda m: llms[m], b), b


def test_uses_first_model_and_records_real_usage(db):
    llm, budget = _chain(db, {"big": NamedLLM("big"), "small": NamedLLM("small")})
    res = llm.complete("hello", purpose="synthesize")
    assert res.model == "big" and budget.used_today("big") == 120 and budget.used_today("small") == 0


def test_falls_back_when_the_first_model_is_out_of_budget(db):
    llm, budget = _chain(db, {"big": NamedLLM("big"), "small": NamedLLM("small")})
    budget.reserve("big", 6000)  # drain the big model's minute bucket
    assert llm.complete("hello", purpose="synthesize").model == "small"


def test_falls_back_when_the_provider_fails_and_refunds_the_failed_model(db):
    llm, budget = _chain(db, {"big": NamedLLM("big", fail=True), "small": NamedLLM("small")})
    assert llm.complete("hi", purpose="synthesize").model == "small"
    assert budget.reserve("big", 6000) == 0.0  # the failed model's reservation was given back


def test_unlisted_purpose_uses_the_default_chain(db):
    llm, _ = _chain(db, {"big": NamedLLM("big"), "small": NamedLLM("small")})
    assert llm.complete("hi", purpose="refine_query").model == "small"


def test_everything_exhausted_raises_rate_limited_with_the_shortest_wait(db):
    llm, budget = _chain(db, {"big": NamedLLM("big"), "small": NamedLLM("small")})
    budget.reserve("big", 6000)
    budget.reserve("small", 6000)
    with pytest.raises(RateLimited) as e:
        llm.complete("hi", purpose="synthesize")
    assert e.value.scope == "model_budget" and e.value.retry_after >= 1


def test_all_providers_failing_re_raises_the_provider_error(db):
    llm, _ = _chain(db, {"big": NamedLLM("big", fail=True), "small": NamedLLM("small", fail=True)})
    with pytest.raises(LLMUnavailableError):
        llm.complete("hi", purpose="synthesize")


def test_structured_calls_go_through_the_budget_too(db):
    from acp.nodes import JudgeVerdict

    llm, budget = _chain(db, {"big": NamedLLM("big"), "small": NamedLLM("small")})
    res = llm.structured(JudgeVerdict, "judge this", purpose="judge")
    assert res.parsed.sufficient is True and res.model == "small" and budget.used_today("small") == 240


def test_other_exceptions_refund_and_propagate(db):
    class Boom(NamedLLM):
        def complete(self, prompt, *, purpose):
            raise ValueError("bug")

    llm, budget = _chain(db, {"big": Boom("big"), "small": NamedLLM("small")})
    with pytest.raises(ValueError):
        llm.complete("hi", purpose="synthesize")
    assert budget.reserve("big", 6000) == 0.0


def test_result_carries_cost_model(db):
    from acp.config import Settings

    s = Settings(prices={"big": (1.0, 1.0), "small": (0.0, 0.0)})
    assert s.cost_usd(1_000_000, 0, "big") == 1.0 and s.cost_usd(1_000_000, 0, "small") == 0.0
    assert LLMResult().model == ""
