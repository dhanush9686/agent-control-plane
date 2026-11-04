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
from acp.errors import LLMUnavailableError, MissingKeyError

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
    if status == 400 and "tool_use_failed" in str(exc):
        return True  # seen in practice: the model occasionally answers in prose instead of calling the schema tool
    if isinstance(status, int):
        return status == 429 or status >= 500
    name = type(exc).__name__.lower()
    return any(w in name for w in ("timeout", "connection", "ratelimit", "unavailable"))


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        return float(headers.get("retry-after")) if headers else None
    except (TypeError, ValueError):
        return None


class GroqLLM:
    """ChatGroq with exponential backoff + token/latency accounting."""

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
        start = time.perf_counter()
        last: Exception | None = None
        for attempt in range(self._settings.llm_max_retries + 1):
            try:
                out = fn()
                return out, (time.perf_counter() - start) * 1000, attempt
            except Exception as exc:  # noqa: BLE001 - we classify below
                last = exc
                if not _is_retryable(exc) or attempt == self._settings.llm_max_retries:
                    break
                delay = _retry_after(exc) or min(2**attempt + random.random(), 30.0)
                time.sleep(delay)
        raise LLMUnavailableError(
            f"The LLM call failed after {self._settings.llm_max_retries} retries "
            f"({type(last).__name__}). Likely a rate limit or outage: wait a minute and retry."
        ) from last

    @staticmethod
    def _tokens(msg: Any) -> tuple[int, int]:
        usage = getattr(msg, "usage_metadata", None) or {}
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    def complete(self, prompt: str, *, purpose: str) -> LLMResult:
        msg, ms, retries = self._call(lambda: self._chat.invoke(prompt))
        tin, tout = self._tokens(msg)
        return LLMResult(str(msg.content).strip(), None, tin, tout, ms, retries)

    def structured(self, schema: type[T], prompt: str, *, purpose: str) -> LLMResult:
        runnable = self._chat.with_structured_output(schema, include_raw=True)

        def go() -> dict:
            out = runnable.invoke(prompt)
            if out.get("parsed") is None:
                raise _BadOutput(str(out.get("parsing_error")))
            return out

        out, ms, retries = self._call(go)
        tin, tout = self._tokens(out["raw"])
        return LLMResult("", out["parsed"], tin, tout, ms, retries)


def get_llm(settings: Settings) -> LLM:
    return GroqLLM(settings)
