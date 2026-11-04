# Design decisions

Each entry: the choice, why, and what it costs us. Everything here describes code that exists.

## 1. LangGraph StateGraph with a conditional edge (not a while-loop)
- **Choice:** `retrieve -> judge -> (refine_query -> retrieve | synthesize) -> END`, with the routing in `acp/graph.py::make_router`.
- **Why:** the loop decision ("sufficient? attempts left?") is a pure function of state, so unit tests cover all three branches without any LLM. The retry cap (`max_attempts=2`) is a visible setting, not a hidden `break`.
- **Cost:** one more dependency and a little ceremony for a graph this small.

## 2. Dependency injection for the LLM and retriever
- **Choice:** `make_nodes(llm, retriever, settings)`; the LLM is a small protocol (`complete`, `structured`).
- **Why:** unit tests use a scripted fake LLM and fake retriever and run offline in under a second; the real pieces (Groq + FAISS) are covered by `make test-live` and by the eval itself.
- **Cost:** a fake can drift from reality; that is why the live tests and real eval exist.

## 3. One LLM door with our own retry/backoff
- **Choice:** `ChatGroq(max_retries=0)` and `GroqLLM._call` retries on 429, 5xx, timeouts, connection errors and malformed structured output, honouring `retry-after`.
- **Why:** the free tier rate-limits constantly (44 retries in one 19-question eval). Owning the loop lets us count retries per call and put them in the trace. A real run also showed Groq returning HTTP 400 `tool_use_failed` when the model answered in prose instead of calling the schema tool; we treat that as retryable.
- **Cost:** retries inflate latency numbers; the README says so.

## 4. OpenTelemetry spans, custom SQLite exporter
- **Choice:** standard OTel SDK for spans; our own `SpanExporter` writes `runs` and `spans` tables.
- **Why:** OTel is the industry vocabulary (spans, parents, attributes) and keeps the door open to swap the exporter for a vendor later. A private `TracerProvider` (no global state) keeps tests isolated.
- **Never crash the agent:** span creation, attribute setting and export are all wrapped; failures are logged and swallowed. A test forces the exporter to fail.
- **Cost:** `SimpleSpanProcessor` writes synchronously, fine at this scale, not for high throughput.

## 5. SQLite as the trace store
- **Why:** a single file, no server, transactional, queryable with plain SQL, trivially exported to JSON for a static dashboard. Indexes on `spans(run_id)`, `spans(name)`, `runs(started_at)`.
- **Cost:** one writer at a time, no retention policy, not a production observability backend.

## 6. LLM-as-judge for grounding, plus deterministic checks
- **Why LLM judge:** deciding whether a sentence is supported by a passage needs language understanding; string matching cannot do it.
- **Known biases and how we handle them:**
  - *Self-preference / shared blind spots:* same model family may grade its own output leniently. The grader can be a different model (`--judge-model`); the default is the same model for free-tier simplicity. Not eliminated.
  - *Leniency to fluent text:* the prompt says plausible-but-absent facts are NOT supported and requires a verbatim evidence quote.
  - *Variance:* temperature 0, but results still differ run to run (our two full runs scored 89.5% then 100%).
  - *Format slips:* in a real run the judge listed "That is not in the knowledge base." as a claim despite instructions. We now filter refusal sentences deterministically rather than trust the prompt.
- **Deterministic checks** (refusal present when expected, citation present, cited source was actually retrieved, banned phrases for injection tests) are cheap, free and stable, and anchor the LLM judgement.
- **Cost:** one extra LLM call per question; claim splitting and verifying share a call.

## 7. Citations accepted in two bracket styles
- A real run showed the model writing `【Source: X】` despite being told to use `[X]`. The prompt was tightened and the extractor accepts both, instead of failing correct answers on formatting.

## 8. Golden set design
- 19 questions over six categories, each with expected behaviour (`answer` or `refuse`), required any-of phrases and banned phrases. It includes a thin-data entry, a contradiction trap and three prompt-injection attempts. Small by design so it fits free-tier limits.

## 9. Regression gate on a committed report
- `scripts/check_regression.py` compares `results/eval_report.json` with `evals/thresholds.json`. Thresholds are set below the observed numbers to tolerate run-to-run variance. CI does not re-run the LLM evals.

## 10. Static dashboard, no build step
- Vanilla JS + one pinned CDN chart library (Chart.js 4.4.1 with an integrity hash). All data is rendered via `textContent`, so exported text cannot inject HTML. If the chart library fails to load, the table still shows the same numbers.

## 11. Index is rebuilt, not committed
- `data/faiss_index/` is gitignored; `make setup` builds it. FAISS indexes are loaded with pickle (`allow_dangerous_deserialization=True`), which is only safe for files you built yourself, so we never ship one.
