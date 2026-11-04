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
