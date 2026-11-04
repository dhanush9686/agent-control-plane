import os

import pytest

from acp.config import Settings
from acp.errors import LLMUnavailableError, MissingKeyError
from acp.llm import GroqLLM, _is_retryable


class Err(Exception):
    def __init__(self, status=None, msg=""):
        super().__init__(msg)
        self.status_code = status


def _bare(max_retries=2):
    llm = object.__new__(GroqLLM)  # skip __init__: no key, no network
    llm._settings = Settings(llm_max_retries=max_retries)
    return llm


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("acp.llm.time.sleep", lambda s: None)


def test_retryable_classification():
    assert _is_retryable(Err(429)) and _is_retryable(Err(503))
    assert _is_retryable(Err(400, "tool_use_failed"))
    assert not _is_retryable(Err(401)) and not _is_retryable(Err(400, "bad request"))
    assert _is_retryable(type("APITimeoutError", (Exception,), {})())


def test_retries_then_succeeds_and_counts_retries():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Err(429)
        return "ok"

    out, _, retries = _bare()._call(flaky)
    assert out == "ok" and retries == 2


def test_gives_up_with_friendly_error():
    def always():
        raise Err(429)

    with pytest.raises(LLMUnavailableError, match="rate limit"):
        _bare(max_retries=2)._call(always)


def test_non_retryable_fails_immediately():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise Err(401)

    with pytest.raises(LLMUnavailableError):
        _bare()._call(bad)
    assert calls["n"] == 1


def test_missing_key_gives_friendly_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(MissingKeyError, match=".env"):
        GroqLLM(Settings())


@pytest.mark.live
@pytest.mark.skipif(
    not (os.environ.get("GROQ_API_KEY") and os.environ.get("RUN_LIVE") == "1"),
    reason="live test: set RUN_LIVE=1 and GROQ_API_KEY",
)
def test_live_groq_roundtrip():
    res = GroqLLM(Settings()).complete("Reply with the single word: ok", purpose="live")
    assert res.text and res.output_tokens > 0
