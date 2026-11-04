"""Friendly, specific errors.

Analogy: instead of the car just stalling, the dashboard shows "low fuel".
Each error carries a human-readable hint so the CLI can print it without a
scary stack trace.
"""


class ACPError(Exception):
    """Base class: anything we expect can go wrong and can explain itself."""


class MissingKeyError(ACPError):
    pass


class EmptyIndexError(ACPError):
    pass


class LLMUnavailableError(ACPError):
    """The LLM provider kept failing after all retries (rate limit, timeout, outage)."""


class RateLimited(ACPError):
    """A limit was hit (per-user rate, daily quota, or the shared model budget). The API turns this into HTTP 429."""

    def __init__(self, message: str, retry_after: float, scope: str = "rate") -> None:
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))
        self.scope = scope  # rate | daily_asks | daily_uploads | model_budget
