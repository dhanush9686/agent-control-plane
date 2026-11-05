"""Friendly, specific errors.

Analogy: instead of the car just stalling, the dashboard shows "low fuel".
Each error carries a human-readable hint so the CLI can print it without a
scary stack trace.
"""


class ACPError(Exception):
    """Base class: anything we expect can go wrong and can explain itself."""


class MissingKeyError(ACPError):
    pass


class LLMUnavailableError(ACPError):
    """The LLM provider kept failing after all retries (rate limit, timeout, outage).

    `retry_after` is the provider's own estimate (seconds) when it gave one, so callers can pass it on.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ModelAccessError(LLMUnavailableError):
    """The AWS account/region cannot use this model. A subclass of LLMUnavailableError so a fallback chain moves on."""


class RateLimited(ACPError):
    """A limit was hit (per-user rate, daily quota, or the shared model budget). The API turns this into HTTP 429."""

    def __init__(self, message: str, retry_after: float, scope: str = "rate") -> None:
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))
        self.scope = scope  # rate | daily_asks | daily_uploads | model_budget
