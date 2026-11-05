"""BedrockLLM against a fake boto3 client (offline), plus the shared retry rules both providers now use."""

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from acp.config import Settings
from acp.errors import LLMUnavailableError, ModelAccessError
from acp.evals.grounding import GroundingReport
from acp.llm import (
    GIVE_UP_IF_ASKED_TO_WAIT_S,
    MAX_SLEEP_S,
    BedrockLLM,
    GroqLLM,
    _retry_after,
    call_with_retries,
    inline_json_schema,
)
from acp.nodes import JudgeVerdict


def cerr(code, headers=None):
    meta = {"HTTPHeaders": headers or {}}
    return ClientError({"Error": {"Code": code, "Message": f"{code} happened"}, "ResponseMetadata": meta}, "Converse")


def text_reply(text="Hello there", tin=11, tout=3):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": tin, "outputTokens": tout},
    }


def tool_reply(payload, tin=40, tout=9):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "t1", "name": "respond", "input": payload}}],
            }
        },
        "usage": {"inputTokens": tin, "outputTokens": tout},
    }


class FakeBedrock:
    """Scripted `converse`: each entry is a response dict or an exception to raise."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def converse(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("acp.llm.time.sleep", sleeps.append)
    return sleeps


def llm(client, **kw):
    s = Settings(model="apac.amazon.nova-micro-v1:0", aws_region="ap-south-1", llm_max_retries=3, **kw)
    return BedrockLLM(s, client=client)


# ------------------------------------------------------------------ complete
def test_complete_sends_a_converse_request_and_maps_tokens():
    client = FakeBedrock(text_reply("Rocky kills Garuda.", 120, 7))
    res = llm(client).complete("who?", purpose="synthesize")
    assert (res.text, res.input_tokens, res.output_tokens, res.model) == (
        "Rocky kills Garuda.",
        120,
        7,
        "apac.amazon.nova-micro-v1:0",
    )
    sent = client.calls[0]
    assert sent["modelId"] == "apac.amazon.nova-micro-v1:0"
    assert sent["messages"] == [{"role": "user", "content": [{"text": "who?"}]}]
    assert sent["inferenceConfig"] == {"maxTokens": 1024, "temperature": 0.0}
    assert "toolConfig" not in sent


def test_reasoning_blocks_are_not_mixed_into_the_answer():
    reply = {
        "output": {
            "message": {"content": [{"reasoningContent": {"reasoningText": {"text": "thinking"}}}, {"text": "Final."}]}
        },
        "usage": {},
    }
    assert llm(FakeBedrock(reply)).complete("x", purpose="p").text == "Final."


# ------------------------------------------------------------------ structured
def test_structured_forces_a_tool_call_with_the_pydantic_schema():
    client = FakeBedrock(tool_reply({"sufficient": True, "reasoning": "it says so"}))
    res = llm(client).structured(JudgeVerdict, "judge", purpose="judge")
    assert res.parsed == JudgeVerdict(sufficient=True, reasoning="it says so") and res.input_tokens == 40
    cfg = client.calls[0]["toolConfig"]
    assert cfg["toolChoice"] == {"tool": {"name": "respond"}}
    schema = cfg["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert schema["type"] == "object" and set(schema["properties"]) == {"sufficient", "reasoning"}
    assert "title" not in schema and "$defs" not in schema


def test_nested_schemas_are_inlined_for_the_tool_spec():
    schema = inline_json_schema(GroundingReport.model_json_schema())
    assert "$ref" not in str(schema) and "$defs" not in str(schema)
    item = schema["properties"]["claims"]["items"]
    assert item["type"] == "object" and set(item["properties"]) == {"claim", "supported", "evidence"}


def test_structured_retries_when_the_model_returns_the_wrong_shape():
    client = FakeBedrock(
        tool_reply({"sufficient": "maybe-not-a-bool-at-all", "reasoning": 5}),
        tool_reply({"sufficient": False, "reasoning": "no"}),
    )
    res = llm(client).structured(JudgeVerdict, "judge", purpose="judge")
    assert res.parsed.sufficient is False and res.retries == 1 and len(client.calls) == 2


def test_structured_retries_when_the_model_skips_the_tool():
    client = FakeBedrock(text_reply("I refuse to use tools"), tool_reply({"sufficient": True, "reasoning": "ok"}))
    assert llm(client).structured(JudgeVerdict, "j", purpose="judge").retries == 1


# ------------------------------------------------------------------ retries and errors
def test_throttling_is_retried_with_capped_backoff_and_counted(no_sleep):
    client = FakeBedrock(cerr("ThrottlingException"), cerr("ServiceUnavailableException"), text_reply("ok"))
    res = llm(client).complete("hi", purpose="p")
    assert res.text == "ok" and res.retries == 2 and len(no_sleep) == 2 and all(s <= MAX_SLEEP_S for s in no_sleep)


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "ModelTimeoutException", "InternalServerException", "ModelNotReadyException"]
)
def test_retryable_codes_give_up_with_a_friendly_error_after_the_limit(code):
    with pytest.raises(LLMUnavailableError, match="failed after 3 retries"):
        llm(FakeBedrock(cerr(code))).complete("hi", purpose="p")


def test_transport_timeouts_are_retried():
    client = FakeBedrock(ReadTimeoutError(endpoint_url="https://bedrock-runtime"), text_reply("fine"))
    assert llm(client).complete("hi", purpose="p").retries == 1


def test_access_denied_is_a_model_access_error_with_actionable_advice_and_no_retry(no_sleep):
    client = FakeBedrock(cerr("AccessDeniedException"))
    with pytest.raises(ModelAccessError, match="Model access") as e:
        llm(client).complete("hi", purpose="p")
    assert "apac.amazon.nova-micro-v1:0" in str(e.value) and "ap-south-1" in str(e.value)
    assert len(client.calls) == 1 and no_sleep == []  # permanent: never retried
    assert isinstance(e.value, LLMUnavailableError)  # so a fallback chain moves on to the next model


def test_validation_errors_are_not_retried():
    client = FakeBedrock(cerr("ValidationException"))
    with pytest.raises(LLMUnavailableError):
        llm(client).complete("hi", purpose="p")
    assert len(client.calls) == 1


# ------------------------------------------------------------------ the known-issue fix (shared by Groq too)
def test_a_huge_retry_after_makes_us_give_up_immediately_instead_of_sleeping(no_sleep):
    """Regression: a Groq daily cap said retry-after: 847 and the request slept ~14 minutes."""
    calls = []

    def fn():
        calls.append(1)
        raise cerr("ThrottlingException", {"retry-after": "847"})

    with pytest.raises(LLMUnavailableError, match="asked us to wait 847 s") as e:
        call_with_retries(fn, max_retries=4, retryable=lambda x: True)
    assert len(calls) == 1 and no_sleep == [] and e.value.retry_after == 847.0


def test_a_small_retry_after_is_honoured_but_capped(no_sleep):
    seq = [
        cerr("ThrottlingException", {"retry-after": "3"}),
        cerr("ThrottlingException", {"retry-after": "14"}),
        "done",
    ]

    def fn():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    out, _, retries = call_with_retries(fn, max_retries=4, retryable=lambda x: True)
    assert out == "done" and retries == 2 and no_sleep == [3.0, MAX_SLEEP_S] and 14 < GIVE_UP_IF_ASKED_TO_WAIT_S


def test_total_time_per_call_is_bounded_by_the_deadline(no_sleep):
    t = [0.0]
    import acp.llm as m

    real = m.time.perf_counter
    m.time.perf_counter = lambda: t[0]
    try:

        def fn():
            t[0] += 30  # each attempt "takes" 30 s
            raise cerr("ThrottlingException")

        with pytest.raises(LLMUnavailableError):
            call_with_retries(fn, max_retries=10, retryable=lambda x: True, deadline_s=60)
    finally:
        m.time.perf_counter = real
    assert t[0] <= 90  # gave up near the deadline instead of burning all 10 retries


def test_retry_after_is_read_from_botocore_and_http_style_errors():
    assert _retry_after(cerr("ThrottlingException", {"retry-after": "7"})) == 7.0
    http = type("E", (Exception,), {})()
    http.response = type("R", (), {"headers": {"retry-after": "5"}})()
    assert _retry_after(http) == 5.0 and _retry_after(ValueError("x")) is None and _retry_after(cerr("X")) is None


def test_groq_client_uses_the_same_capped_retry_logic(no_sleep):
    class Err(Exception):
        status_code = 429
        response = type("R", (), {"headers": {"retry-after": "847"}})()

    g = object.__new__(GroqLLM)
    g._settings = Settings(llm_max_retries=4)
    with pytest.raises(LLMUnavailableError, match="asked us to wait 847 s"):
        g._call(lambda: (_ for _ in ()).throw(Err("daily cap")))
    assert no_sleep == []


# ------------------------------------------------------------------ provider switch
def test_get_llm_selects_the_provider(monkeypatch):
    from acp.llm import get_llm

    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert type(get_llm(Settings(llm_provider="groq", model="m"))).__name__ == "GroqLLM"
    with pytest.raises(Exception, match="Unknown ACP_LLM_PROVIDER"):
        get_llm(Settings(llm_provider="nope"))


def test_prefix_helper_and_cost_lookup():
    from acp.config import bedrock_profile_prefix, price_key

    assert [bedrock_profile_prefix(r) for r in ("ap-south-1", "us-east-1", "eu-west-1", None)] == [
        "apac",
        "us",
        "eu",
        "us",
    ]
    assert (
        price_key("apac.amazon.nova-lite-v1:0") == "amazon.nova-lite-v1:0"
        and price_key("global.anthropic.x") == "anthropic.x"
    )
    s = Settings()
    assert s.cost_usd(1_000_000, 1_000_000, "us.amazon.nova-micro-v1:0") == pytest.approx(0.175)
    assert s.cost_usd(1_000_000, 0, "totally.unknown-model") == 0.0


def test_default_chains_use_nova_lite_first_with_micro_as_fallback_and_pro_as_grader(monkeypatch):
    for k in ("ACP_MODEL", "ACP_JUDGE_MODEL", "ACP_JUDGE_MODELS", "ACP_SYNTH_MODELS", "ACP_LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    s = Settings()
    assert s.model == "apac.amazon.nova-lite-v1:0"
    assert s.judge_models == s.synth_models == ("apac.amazon.nova-lite-v1:0", "apac.amazon.nova-micro-v1:0")
    assert s.judge_model == "apac.amazon.nova-pro-v1:0" and s.judge_model != s.model
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert Settings().synth_models[0] == "us.amazon.nova-lite-v1:0"  # region prefix follows AWS_REGION
    monkeypatch.setenv("ACP_SYNTH_MODELS", "a, b ,c")
    assert Settings().synth_models == ("a", "b", "c")  # env override still wins
