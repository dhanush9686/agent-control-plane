"""Rate limiting, daily quotas, and the shared model-token budget.

Analogy: three turnstiles in a row. (1) Per-user speed limit: no more than N questions a minute. (2) Per-user daily
allowance: M questions and K uploads a day. (3) The shared fuel gauge: all users together may only burn so many
model tokens per minute and per day, because the free tier is one shared tank. Pass all three or wait.

Everything takes an injectable clock so tests can fast-forward time. Counters that must survive a restart (daily
usage) live in SQLite; the per-minute ones are in memory and reset on restart, which is harmless.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from acp.budget import estimate_tokens
from acp.errors import LLMUnavailableError, RateLimited
from acp.llm import LLM, LLMResult

Clock = Callable[[], float]
DEFAULT_OUTPUT_RESERVE = 900  # tokens we assume a completion may use, reserved before the call and refunded after


def _utc_day(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _seconds_to_midnight(now: datetime) -> float:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


class SlidingWindow:
    """At most `limit` events per `window_s` seconds per key. Thread-safe, in memory (one server process)."""

    def __init__(self, limit: int, window_s: float = 60.0, clock: Clock = time.monotonic) -> None:
        self.limit, self.window, self._clock = limit, window_s, clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> float:
        """Record an event. Returns 0.0 if allowed, else the seconds to wait before the next one would be."""
        now = self._clock()
        with self._lock:
            q = self._events[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return q[0] + self.window - now
            q.append(now)
            return 0.0


class DailyQuota:
    """Per-user daily counters in SQLite (asks, uploads). The increment is atomic, so two simultaneous requests can
    never both squeeze under the limit."""

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], utc_now: Callable[[], datetime] | None = None):
        self._conn_factory = conn_factory
        self._utc_now = utc_now or (lambda: datetime.now(UTC))

    def _consume(self, user_id: str, column: str, limit: int) -> float:
        if limit <= 0:
            return _seconds_to_midnight(self._utc_now())
        now = self._utc_now()
        conn = self._conn_factory()
        try:
            with conn:
                cur = conn.execute(
                    f"INSERT INTO usage (user_id, day, {column}) VALUES (?, ?, 1) "
                    f"ON CONFLICT(user_id, day) DO UPDATE SET {column} = {column} + 1 WHERE {column} < ?",
                    (user_id, _utc_day(now), limit),
                )
                return 0.0 if cur.rowcount == 1 else _seconds_to_midnight(now)
        finally:
            conn.close()

    def consume_ask(self, user_id: str, limit: int) -> float:
        return self._consume(user_id, "asks", limit)

    def consume_upload(self, user_id: str, limit: int) -> float:
        return self._consume(user_id, "uploads", limit)

    def remaining(self, user_id: str, ask_limit: int, upload_limit: int) -> dict[str, int]:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                "SELECT asks, uploads FROM usage WHERE user_id=? AND day=?", (user_id, _utc_day(self._utc_now()))
            ).fetchone()
        finally:
            conn.close()
        asks, uploads = (row["asks"], row["uploads"]) if row else (0, 0)
        return {"asks": max(0, ask_limit - asks), "uploads": max(0, upload_limit - uploads)}


class ModelBudget:
    """The shared fuel gauge for each model: a per-minute bucket (in memory) and a per-day total (SQLite).

    reserve() before a call, settle() after it with the real token count (refunding what we over-reserved).
    """

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        tpm: int,
        daily_tokens: int,
        clock: Clock = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn_factory, self.tpm, self.daily = conn_factory, tpm, daily_tokens
        self._clock = clock
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._bucket: dict[str, tuple[float, float]] = {}  # model -> (tokens available, last refill time)
        self._lock = threading.Lock()

    def used_today(self, model: str) -> int:
        conn = self._conn_factory()
        try:
            row = conn.execute(
                "SELECT tokens FROM model_usage WHERE model=? AND day=?", (model, _utc_day(self._utc_now()))
            ).fetchone()
        finally:
            conn.close()
        return int(row["tokens"]) if row else 0

    def _refill(self, model: str) -> float:
        now = self._clock()
        tokens, last = self._bucket.get(model, (float(self.tpm), now))
        tokens = min(float(self.tpm), tokens + (now - last) * self.tpm / 60.0)
        self._bucket[model] = (tokens, now)
        return tokens

    def reserve(self, model: str, est_tokens: int) -> float:
        """Try to take `est_tokens` from the model. Returns 0.0 on success, else seconds to wait."""
        est = min(est_tokens, self.tpm)  # a request bigger than the whole bucket would otherwise never be admitted
        if self.used_today(model) + est > self.daily:
            return _seconds_to_midnight(self._utc_now())
        with self._lock:
            available = self._refill(model)
            if available < est:
                return (est - available) * 60.0 / self.tpm
            self._bucket[model] = (available - est, self._clock())
            return 0.0

    def settle(self, model: str, reserved: int, actual: int) -> None:
        """Record real usage: refund (or charge) the difference in the minute bucket, add to the daily total."""
        with self._lock:
            available = self._refill(model)
            self._bucket[model] = (
                min(float(self.tpm), max(0.0, available + min(reserved, self.tpm) - actual)),
                self._clock(),
            )
        conn = self._conn_factory()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO model_usage (model, day, tokens) VALUES (?, ?, ?) "
                    "ON CONFLICT(model, day) DO UPDATE SET tokens = tokens + excluded.tokens",
                    (model, _utc_day(self._utc_now()), max(0, actual)),
                )
        finally:
            conn.close()

    def refund(self, model: str, reserved: int) -> None:
        """The call never happened (it failed before reaching the provider): give the reservation back."""
        with self._lock:
            available = self._refill(model)
            self._bucket[model] = (min(float(self.tpm), available + min(reserved, self.tpm)), self._clock())


class BudgetedLLM:
    """An LLM that respects the shared budget and falls back along a chain of models.

    `chains` maps a purpose ("judge", "synthesize", ...) to models in preference order. For each call we take the
    first model that has budget; if its provider fails (LLMUnavailableError) we try the next. If none can serve the
    call we raise RateLimited with the shortest wait, which the API turns into HTTP 429 + Retry-After.
    """

    def __init__(
        self,
        chains: dict[str, tuple[str, ...]],
        default_chain: tuple[str, ...],
        factory: Callable[[str], LLM],
        budget: ModelBudget,
    ) -> None:
        self._chains, self._default, self._factory, self._budget = chains, default_chain, factory, budget
        self.model = default_chain[0]

    def _run(self, purpose: str, prompt: str, call: Callable[[LLM], LLMResult]) -> LLMResult:
        est = estimate_tokens(prompt) + DEFAULT_OUTPUT_RESERVE
        waits: list[float] = []
        last_error: Exception | None = None
        for model in self._chains.get(purpose, self._default):
            wait = self._budget.reserve(model, est)
            if wait > 0:
                waits.append(wait)
                continue
            try:
                res = call(self._factory(model))
            except LLMUnavailableError as e:  # provider kept failing: refund and try the next model
                self._budget.refund(model, est)
                last_error = e
                continue
            except BaseException:
                self._budget.refund(model, est)
                raise
            self._budget.settle(model, est, res.input_tokens + res.output_tokens)
            res.model = res.model or model
            return res
        if waits:
            raise RateLimited(
                "The shared AI budget is used up for the moment. Please try again shortly.",
                min(waits),
                "model_budget",
            )
        assert last_error is not None
        raise last_error

    def complete(self, prompt: str, *, purpose: str) -> LLMResult:
        return self._run(purpose, prompt, lambda llm: llm.complete(prompt, purpose=purpose))

    def structured(self, schema, prompt: str, *, purpose: str) -> LLMResult:  # noqa: ANN001
        return self._run(purpose, prompt, lambda llm: llm.structured(schema, prompt, purpose=purpose))
