"""Tracing: record every step of every run, without ever being able to break the agent.

Analogy: a flight recorder. OpenTelemetry (OTel) is the industry-standard way
to describe "a span" = one timed step (name, start, end, attributes). We
create spans around every graph node and every LLM call, then a custom
exporter files them into SQLite instead of sending them to a vendor.

Safety rule: tracing is a passenger, not the pilot. Every tracing operation is
wrapped so that a failure (disk full, bad value) is swallowed and logged, and
the agent keeps answering.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from pydantic import BaseModel

from acp import store
from acp.config import Settings
from acp.llm import LLM, LLMResult
from acp.state import AgentState

log = logging.getLogger("acp.tracing")
T = TypeVar("T", bound=BaseModel)

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("acp_run_id", default=None)
_run_start_ns: contextvars.ContextVar[int | None] = contextvars.ContextVar("acp_run_start_ns", default=None)


# ---------------------------------------------------------------- records ---
def span_to_record(s: ReadableSpan) -> dict[str, Any] | None:
    """Flatten a finished OTel span into a plain JSON-able dict (the unit we store or stream).

    Analogy: turning a camera's raw footage into a one-line log entry. Everything downstream (SQLite writer,
    Kafka producer/consumer) speaks this dict, so they never touch OpenTelemetry objects.
    """
    a = dict(s.attributes or {})
    run_id = a.get("acp.run_id")
    if not run_id:
        return None  # not ours (e.g. some other library's span)
    return {
        "span_id": format(s.context.span_id, "016x"),
        "run_id": run_id,
        "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
        "name": s.name,
        "kind": a.get("acp.kind", "node"),
        "start_ns": s.start_time,
        "end_ns": s.end_time,
        "run_start_ns": a.get("acp.run_start_ns", s.start_time),
        "status": "ok" if s.status.status_code != trace.StatusCode.ERROR else "error",
        "input_tokens": a.get("acp.input_tokens"),
        "output_tokens": a.get("acp.output_tokens"),
        "cost_usd": a.get("acp.cost_usd"),
        "retries": a.get("acp.retries"),
        "question": a.get("acp.question", ""),
        "error": a.get("acp.error"),
        "attrs": {k.removeprefix("acp.attr."): v for k, v in a.items() if k.startswith("acp.attr.")},
    }


def write_record(conn: sqlite3.Connection, r: dict[str, Any]) -> None:
    """Insert one span record; when it is the run's root span, also upsert the `runs` summary row.

    Idempotent (INSERT OR REPLACE), so replaying a Kafka message twice is harmless.
    """
    latency_ms = (r["end_ns"] - r["start_ns"]) / 1e6
    extra = r["attrs"]
    conn.execute(
        "INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            r["span_id"],
            r["run_id"],
            r["parent_id"],
            r["name"],
            r["kind"],
            (r["start_ns"] - r["run_start_ns"]) / 1e6,
            latency_ms,
            r["status"],
            r["input_tokens"],
            r["output_tokens"],
            r["cost_usd"],
            r["retries"],
            json.dumps(extra, default=str),
        ),
    )
    if r["kind"] == "run":  # the root span closes last, so its children are already in the table
        agg = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cost_usd),0), COUNT(*), COALESCE(SUM(retries),0) "
            "FROM spans WHERE run_id=? AND kind='llm'",
            (r["run_id"],),
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["run_id"],
                r["question"],
                datetime.fromtimestamp(r["start_ns"] / 1e9, tz=UTC).isoformat(timespec="seconds"),
                latency_ms,
                r["status"],
                r["error"],
                extra.get("answer"),
                extra.get("attempts"),
                extra.get("sufficient"),
                agg[0],
                agg[1],
                agg[2],
                agg[3],
                agg[4],
                extra.get("retrieved_json"),
            ),
        )


# ---------------------------------------------------------------- exporters ---
class SQLiteSpanExporter(SpanExporter):
    """Receives finished spans from OpenTelemetry and writes them into SQLite."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = db_path
        store.connect(db_path).close()  # create schema eagerly

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            conn = store.connect(self._db_path)
            try:
                with conn:  # one transaction per batch
                    for s in spans:
                        if (rec := span_to_record(s)) is not None:
                            write_record(conn, rec)
            finally:
                conn.close()
            return SpanExportResult.SUCCESS
        except Exception:  # noqa: BLE001 - tracing must never raise into the agent
            log.exception("trace export failed (agent unaffected)")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:  # nothing to release: we open a connection per batch
        pass


class KafkaSpanExporter(SpanExporter):
    """Publishes span records to a Kafka topic instead of writing SQLite directly.

    Analogy: instead of filing each sheet yourself, you drop it in the outbox; a separate clerk
    (scripts/kafka_consumer.py) files it. The agent never waits on the database. Messages are keyed by run_id
    so all spans of one run stay in order on one partition (children before the root).
    `producer` is anything with produce(topic, key, value) and flush(): confluent_kafka.Producer or a fake.
    """

    def __init__(self, producer: Any, topic: str = "acp.spans") -> None:
        self._producer, self._topic = producer, topic

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            for s in spans:
                if (rec := span_to_record(s)) is not None:
                    self._producer.produce(self._topic, key=rec["run_id"], value=json.dumps(rec, default=str))
            self._producer.flush(5)
            return SpanExportResult.SUCCESS
        except Exception:  # noqa: BLE001
            log.exception("kafka export failed (agent unaffected)")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._producer.flush(5)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------- span helper ---
class SpanHandle:
    """What `with tracing.span(...) as sp` gives you. Every method is exception-proof."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, attrs: dict[str, Any]) -> None:
        """Set raw OTel attributes by their exact key, e.g. {"acp.input_tokens": 12}."""
        for k, v in attrs.items():
            try:
                if v is not None:
                    self._span.set_attribute(k, v)
            except Exception:  # noqa: BLE001
                log.debug("set_attribute failed", exc_info=True)

    def attr(self, **extra: Any) -> None:
        """Free-form details shown in the dashboard (stored under acp.attr.*)."""
        for k, v in extra.items():
            try:
                if v is None:
                    continue
                self._span.set_attribute(
                    f"acp.attr.{k}", v if isinstance(v, (str, int, float, bool)) else json.dumps(v, default=str)
                )
            except Exception:  # noqa: BLE001
                log.debug("attr failed", exc_info=True)

    def fail(self, exc: BaseException) -> None:
        try:
            self._span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)[:200]))
            self._span.set_attribute("acp.error", f"{type(exc).__name__}: {exc}"[:300])
        except Exception:  # noqa: BLE001
            pass


class Tracing:
    """Owns one TracerProvider wired to the SQLite exporter."""

    def __init__(
        self,
        settings: Settings,
        db_path: Path | str | None = None,
        mode: str | None = None,
        exporter: SpanExporter | None = None,
    ) -> None:
        """mode: "sync" (default; write before returning), "batch" (background thread, faster for the agent),
        or "kafka" (stream to a topic; needs `exporter` or ACP_KAFKA_BOOTSTRAP). Override with ACP_TRACE_MODE."""
        self.settings = settings
        self.db_path = Path(db_path or settings.db_path)
        self.mode = mode or settings.trace_mode
        self._provider = TracerProvider()  # private provider: no global state, tests stay isolated
        exp = exporter or self._make_exporter()
        processor = (
            BatchSpanProcessor(exp, schedule_delay_millis=500) if self.mode == "batch" else SimpleSpanProcessor(exp)
        )
        self._provider.add_span_processor(processor)
        self._tracer = self._provider.get_tracer("acp")

    def _make_exporter(self) -> SpanExporter:
        if self.mode == "kafka":
            from confluent_kafka import Producer  # optional dependency: requirements-streaming.txt

            return KafkaSpanExporter(
                Producer({"bootstrap.servers": self.settings.kafka_bootstrap}), self.settings.kafka_topic
            )
        if self.mode not in ("sync", "batch"):
            raise ValueError(f"unknown trace mode {self.mode!r}; use sync, batch or kafka")
        return SQLiteSpanExporter(self.db_path)

    def flush(self) -> None:
        """Wait until queued spans are written (a no-op in sync mode)."""
        try:
            self._provider.force_flush(10_000)
        except Exception:  # noqa: BLE001
            log.debug("flush failed", exc_info=True)

    @contextmanager
    def span(self, name: str, kind: str, attrs: dict[str, Any] | None = None) -> Iterator[SpanHandle]:
        """Open a span as a child of the current one. Tracing errors are swallowed; the body's are not."""
        span = token = None
        try:
            span = self._tracer.start_span(name)
            span.set_attribute("acp.kind", kind)
            if (rid := _run_id.get()) is not None:
                span.set_attribute("acp.run_id", rid)
            if (t0 := _run_start_ns.get()) is not None:
                span.set_attribute("acp.run_start_ns", t0)  # lets the waterfall place spans on one timeline
            token = otel_context.attach(trace.set_span_in_context(span))
        except Exception:  # noqa: BLE001
            log.exception("could not start span %s", name)
        handle = SpanHandle(span) if span is not None else SpanHandle(trace.INVALID_SPAN)
        handle.set(attrs or {})
        try:
            yield handle
        except BaseException as exc:
            handle.fail(exc)
            raise
        finally:
            try:
                if token is not None:
                    otel_context.detach(token)
                if span is not None:
                    span.end()
            except Exception:  # noqa: BLE001
                log.exception("could not end span %s", name)

    @contextmanager
    def run(self, question: str) -> Iterator[tuple[str, SpanHandle]]:
        """Root span for one question. Yields (run_id, handle); one run_id per question."""
        run_id = uuid.uuid4().hex[:12]
        token = _run_id.set(run_id)
        token_t0 = _run_start_ns.set(time.time_ns())
        try:
            with self.span("agent.run", "run", {"acp.question": question}) as sp:
                yield run_id, sp
        finally:
            _run_id.reset(token)
            _run_start_ns.reset(token_t0)
            if self.mode == "batch":
                self.flush()  # so callers can read the run right after it returns

    def shutdown(self) -> None:
        try:
            self._provider.shutdown()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------ LLM decorator ---
class TracedLLM:
    """Wraps any LLM (real or fake) so each call becomes an `llm.<purpose>` span.

    Records latency, input/output tokens, estimated cost, and retry count.
    """

    def __init__(self, inner: LLM, tracing: Tracing) -> None:
        self._inner = inner
        self._tracing = tracing
        self.model = inner.model

    def _record(self, sp: SpanHandle, res: LLMResult) -> None:
        cost = self._tracing.settings.cost_usd(res.input_tokens, res.output_tokens)
        sp.set(
            {
                "acp.input_tokens": res.input_tokens,
                "acp.output_tokens": res.output_tokens,
                "acp.cost_usd": cost,
                "acp.retries": res.retries,
            }
        )
        sp.attr(model=self.model, provider_latency_ms=round(res.latency_ms, 1))

    def complete(self, prompt: str, *, purpose: str) -> LLMResult:
        with self._tracing.span(f"llm.{purpose}", "llm") as sp:
            res = self._inner.complete(prompt, purpose=purpose)
            self._record(sp, res)
            return res

    def structured(self, schema: type[T], prompt: str, *, purpose: str) -> LLMResult:
        with self._tracing.span(f"llm.{purpose}", "llm") as sp:
            res = self._inner.structured(schema, prompt, purpose=purpose)
            self._record(sp, res)
            return res


# ------------------------------------------------------------ node wrapper ---
def _node_details(name: str, state: AgentState, out: dict[str, Any]) -> dict[str, Any]:
    """Small per-node summary shown in the waterfall (judge verdict, query used, ...)."""
    if name == "retrieve":
        docs = out.get("retrieved_docs", [])
        return {
            "query": state.get("search_query") or state.get("question"),
            "attempt": out.get("search_attempts"),
            "new_chunks": len(docs) - len(state.get("retrieved_docs", [])),
            "total_chunks": len(docs),
            "sources": sorted({d.metadata.get("source", "?") for d in docs}),
        }
    if name == "judge":
        return {"sufficient": out.get("sufficient_context"), "reasoning": out.get("judge_reasoning")}
    if name == "refine_query":
        return {"new_query": out.get("search_query")}
    if name == "synthesize":
        return {"answer_chars": len(out.get("answer", ""))}
    return {}


def trace_node(
    name: str, fn: Callable[[AgentState], dict[str, Any]], tracing: Tracing
) -> Callable[[AgentState], dict[str, Any]]:
    def wrapper(state: AgentState) -> dict[str, Any]:
        with tracing.span(f"node.{name}", "node") as sp:
            out = fn(state)
            try:
                sp.attr(**_node_details(name, state, out))
            except Exception:  # noqa: BLE001
                log.debug("node details failed", exc_info=True)
            return out

    return wrapper
