"""The LLM client: one door through which every model call passes.

Analogy: the company switchboard. Every call goes through one place, so we can
(a) retry politely when the line is busy (rate limits), (b) time it and count
the words used (tokens), and (c) swap in a fake phone for offline tests.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from acp.config import Settings
from acp.errors import LLMUnavailableError, MissingKeyError, ModelAccessError

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult:
    """What came back from one model call, plus the numbers we trace."""

    text: str = ""
    parsed: Any = None  # a Pydantic object for structured calls
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    retries: int = 0
    model: str = ""  # which model actually served the call (differs from the default when falling back)


class LLM(Protocol):
    """The two things the agent ever asks of a model."""

    model: str

    def complete(self, prompt: str, *, purpose: str) -> LLMResult: ...

    def structured(self, schema: type[T], prompt: str, *, purpose: str) -> LLMResult: ...


class _BadOutput(Exception):
    """Model returned something we could not parse into the schema."""


def _is_retryable(exc: Exception) -> bool:
    """Retry on rate limits (429), server errors (5xx), timeouts, dropped connections, bad JSON."""
    if isinstance(exc, _BadOutput):
        return True
    status = getattr(exc, "status_code", None)
    if status == 400 and any(m in str(exc) for m in ("tool_use_failed", "json_validate_failed", "failed_generation")):
        return True  # seen in practice: the model occasionally answers in prose / invalid JSON instead of the schema
    if isinstance(status, int):
        return status == 429 or status >= 500
    name = type(exc).__name__.lower()
    return any(w in name for w in ("timeout", "connection", "ratelimit", "unavailable"))


def _retry_after(exc: Exception) -> float | None:
    """The provider's requested wait in seconds, from a Retry-After header (HTTP SDKs or botocore), if any."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None and isinstance(resp, dict):  # botocore ClientError.response
        headers = resp.get("ResponseMetadata", {}).get("HTTPHeaders")
    try:
        return float(headers.get("retry-after")) if headers and headers.get("retry-after") else None
    except (TypeError, ValueError, AttributeError):
        return None


MAX_SLEEP_S = 10.0  # never sleep longer than this between attempts
GIVE_UP_IF_ASKED_TO_WAIT_S = 15.0  # a provider that says "come back in 14 minutes" gets abandoned, not waited on


def call_with_retries(
    fn: Any,
    *,
    max_retries: int,
    retryable: Any = _is_retryable,
    deadline_s: float = 60.0,
    describe: Any = None,
) -> tuple[Any, float, int]:
    """Run `fn()`, retrying transient failures with capped backoff. Returns (result, elapsed_ms, retries_used).

    Analogy: redialling a busy line, but with manners. We wait a little between tries (never more than MAX_SLEEP_S),
    stop after `deadline_s` in total, and if the other end says "I'm busy for the next 14 minutes" we hang up at once
    so a fallback model can take the call instead of this request hanging.
    The shared tank of lessons here: a real run once sat in `sleep(847)` because the provider's daily cap was spent.
    """
    start = time.perf_counter()
    last: Exception | None = None
    asked_for: float | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn(), (time.perf_counter() - start) * 1000, attempt
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            if not retryable(exc) or attempt == max_retries:
                break
            asked_for = _retry_after(exc)
            if asked_for is not None and asked_for > GIVE_UP_IF_ASKED_TO_WAIT_S:
                break  # not worth waiting for: let the caller fail over
            delay = min(asked_for if asked_for else 2**attempt + random.random(), MAX_SLEEP_S)
            if (time.perf_counter() - start) + delay > deadline_s:
                break
            time.sleep(delay)
    detail = describe(last) if describe else f"{type(last).__name__}: {str(last)[:160]}"
    if asked_for is not None and asked_for > GIVE_UP_IF_ASKED_TO_WAIT_S:
        msg = f"The model provider asked us to wait {int(asked_for)} s (a daily or account limit). {detail}"
    else:
        msg = f"The LLM call failed after {max_retries} retries ({detail}). Likely a rate limit or outage."
    raise LLMUnavailableError(msg, retry_after=asked_for) from last


class GroqLLM:
    """ChatGroq with capped exponential backoff + token/latency accounting."""

    def __init__(self, settings: Settings) -> None:
        if not os.environ.get("GROQ_API_KEY"):
            raise MissingKeyError(
                "GROQ_API_KEY is not set. Copy .env.example to .env and paste your free key "
                "from https://console.groq.com/keys"
            )
        from langchain_groq import ChatGroq

        self.model = settings.model
        self._settings = settings
        # max_retries=0: WE own retrying so we can count and trace each retry.
        self._chat = ChatGroq(
            model=settings.model,
            temperature=settings.temperature,
            timeout=settings.llm_timeout_s,
            max_retries=0,
        )

    def _call(self, fn: Any) -> tuple[Any, float, int]:
        return call_with_retries(
            fn, max_retries=self._settings.llm_max_retries, deadline_s=self._settings.llm_deadline_s
        )

    @staticmethod
    def _tokens(msg: Any) -> tuple[int, int]:
        usage = getattr(msg, "usage_metadata", None) or {}
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    def complete(self, prompt: str, *, purpose: str) -> LLMResult:
        msg, ms, retries = self._call(lambda: self._chat.invoke(prompt))
        tin, tout = self._tokens(msg)
        return LLMResult(str(msg.content).strip(), None, tin, tout, ms, retries, self.model)

    def structured(self, schema: type[T], prompt: str, *, purpose: str) -> LLMResult:
        runnable = self._chat.with_structured_output(schema, include_raw=True)

        def go() -> dict:
            out = runnable.invoke(prompt)
            if out.get("parsed") is None:
                raise _BadOutput(str(out.get("parsing_error")))
            return out

        out, ms, retries = self._call(go)
        tin, tout = self._tokens(out["raw"])
        return LLMResult("", out["parsed"], tin, tout, ms, retries, self.model)


# ------------------------------------------------------------------------------------------- Amazon Bedrock
_BEDROCK_RETRYABLE = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)
_BEDROCK_NO_ACCESS = frozenset({"AccessDeniedException", "ResourceNotFoundException"})


def _bedrock_code(exc: Exception) -> str | None:
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None


def _bedrock_retryable(exc: Exception) -> bool:
    if isinstance(exc, _BadOutput):
        return True
    code = _bedrock_code(exc)
    if code is not None:
        return code in _BEDROCK_RETRYABLE
    name = type(exc).__name__.lower()  # botocore transport errors: ReadTimeoutError, EndpointConnectionError, ...
    return any(w in name for w in ("timeout", "connection"))


def inline_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a Pydantic JSON schema simple enough for a model's tool spec: resolve $ref/$defs, drop titles/defaults.

    Several Bedrock models accept only a plain subset of JSON Schema, and Pydantic emits `$defs` + `$ref` for nested
    models; inlining them keeps the tool definition self-contained.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title", "default")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk({k: v for k, v in schema.items() if k != "$defs"})


class BedrockLLM:
    """Amazon Bedrock through the Converse API (one request shape for every model family).

    Structured output uses *tool use*: we hand the model one tool whose input schema is our Pydantic model and force
    it to call that tool; the tool input is the structured answer. Credentials come from the normal AWS chain
    (profile, env vars, role); nothing here reads or prints a key.
    """

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.model = settings.model
        self._settings = settings
        self._client = client or self._make_client(settings)

    @staticmethod
    def _make_client(settings: Settings) -> Any:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import NoCredentialsError, NoRegionError

        try:
            session = boto3.Session(region_name=settings.aws_region)
            if session.get_credentials() is None:
                raise NoCredentialsError()
            return session.client(
                "bedrock-runtime",
                config=Config(
                    read_timeout=settings.llm_timeout_s,
                    connect_timeout=10,
                    retries={"max_attempts": 1, "mode": "standard"},  # WE own retrying (counted + traced)
                ),
            )
        except (NoCredentialsError, NoRegionError) as e:
            raise MissingKeyError(
                "No AWS credentials/region found for Amazon Bedrock. Run `aws configure` (or set AWS_PROFILE and "
                "AWS_REGION) and make sure the IAM user may call bedrock:InvokeModel."
            ) from e

    # ---- shared plumbing
    def _call(self, fn: Any) -> tuple[Any, float, int]:
        return call_with_retries(
            fn,
            max_retries=self._settings.llm_max_retries,
            retryable=_bedrock_retryable,
            deadline_s=self._settings.llm_deadline_s,
            describe=self._describe,
        )

    def _describe(self, exc: Exception | None) -> str:
        code = _bedrock_code(exc) if exc is not None else None
        if code in _BEDROCK_NO_ACCESS:
            raise ModelAccessError(
                f"Amazon Bedrock model '{self.model}' is not available to this AWS account in region "
                f"{self._settings.aws_region or 'default'}. Enable it under Bedrock > Model access, "
                "or choose another model (ACP_SYNTH_MODELS / ACP_JUDGE_MODELS)."
            ) from exc
        return f"{type(exc).__name__}: {str(exc)[:160]}"

    @staticmethod
    def _text_of(output: dict[str, Any]) -> str:
        blocks = output.get("output", {}).get("message", {}).get("content", [])
        return "".join(b["text"] for b in blocks if "text" in b).strip()  # skips reasoning/tool blocks

    def _converse(self, prompt: str, **extra: Any) -> dict[str, Any]:
        return self._client.converse(
            modelId=self.model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": self._settings.max_output_tokens, "temperature": self._settings.temperature},
            **extra,
        )

    # ---- the LLM protocol
    def complete(self, prompt: str, *, purpose: str) -> LLMResult:
        out, ms, retries = self._call(lambda: self._converse(prompt))
        usage = out.get("usage", {})
        return LLMResult(
            self._text_of(out),
            None,
            int(usage.get("inputTokens", 0)),
            int(usage.get("outputTokens", 0)),
            ms,
            retries,
            self.model,
        )

    def structured(self, schema: type[T], prompt: str, *, purpose: str) -> LLMResult:
        tool = {
            "toolSpec": {
                "name": "respond",
                "description": "Return your answer by calling this tool with the requested fields.",
                "inputSchema": {"json": inline_json_schema(schema.model_json_schema())},
            }
        }

        def go() -> tuple[dict[str, Any], Any]:
            out = self._converse(prompt, toolConfig={"tools": [tool], "toolChoice": {"tool": {"name": "respond"}}})
            for block in out.get("output", {}).get("message", {}).get("content", []):
                if "toolUse" in block:
                    try:
                        return out, schema.model_validate(block["toolUse"]["input"])
                    except ValueError as e:  # pydantic.ValidationError is a ValueError
                        raise _BadOutput(str(e)[:200]) from e
            raise _BadOutput("the model did not call the tool")

        (out, parsed), ms, retries = self._call(go)
        usage = out.get("usage", {})
        return LLMResult(
            "", parsed, int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0)), ms, retries, self.model
        )


def get_llm(settings: Settings) -> LLM:
    """The configured provider (ACP_LLM_PROVIDER): Amazon Bedrock by default, or Groq."""
    if settings.llm_provider == "groq":
        return GroqLLM(settings)
    if settings.llm_provider == "bedrock":
        return BedrockLLM(settings)
    raise MissingKeyError(f"Unknown ACP_LLM_PROVIDER {settings.llm_provider!r}; use 'bedrock' or 'groq'.")
