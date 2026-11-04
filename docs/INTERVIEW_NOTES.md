# Interview notes (plain language, strictly what exists)

## One-sentence pitch
"I wrapped a small RAG agent with a flight recorder (OpenTelemetry traces in SQLite) and an automatic fact-checker (claim-level grounding evals), then gated releases on the scores and published the results on a static dashboard."

## Component walkthrough

| Component | Plain explanation | Analogy |
|---|---|---|
| `acp/index.py`, `build_index.py` | Splits the corpus into paragraph chunks (13) and embeds them with MiniLM into a FAISS index | Cutting a book into index cards filed by meaning |
| `acp/retrieval.py` | Returns the 3 closest chunks for a query; friendly error if the index is missing | The librarian |
| `acp/nodes.py` | `retrieve`, `judge`, `refine_query`, `synthesize` | Research assistant: fetch, check, re-search, write up with footnotes |
| `acp/graph.py` | LangGraph StateGraph; one conditional edge after `judge`; cap of 2 searches | Subway map with a junction sign |
| `acp/llm.py` | The only place that talks to Groq; retries with backoff; counts tokens | Company switchboard |
| `acp/tracing.py`, `store.py` | OTel spans around nodes and LLM calls, exported to SQLite by a custom exporter | Flight recorder |
| `acp/evals/` | Golden set, deterministic checks, grounding judge, report | Exam day: answer key + fact-checker |
| `scripts/check_regression.py` | Exit 1 if scores fall under thresholds | Bouncer at the release door |
| `dashboard/` | Static page reading `data/traces.json` | A binder of photocopied case files |

## Key tradeoffs I can defend
- Graph vs loop: testable routing, explicit cap.
- DI + fakes for unit tests, real Groq/FAISS in live tests and the eval.
- SQLite: simple and sufficient here; not a production observability backend.
- LLM-judge: needed for semantic support checks, but biased; mitigated, not solved.
- Cost is an estimate from a price table; latency includes free-tier rate-limit waits.

## 10 likely questions

1. **Why a graph instead of a loop?** The routing decision is a pure function (`make_router`), so I unit test the three outcomes (sufficient, retry, cap reached) without an LLM, and the cap is a visible setting. It also makes the path inspectable.
2. **How does the agent avoid hallucinating?** The `synthesize` prompt allows only retrieved context, requires citations and a fixed refusal sentence, and treats the question as untrusted. The `judge` node first decides whether the context is sufficient; if not, it refines the query once and otherwise refuses.
3. **How do you measure hallucination?** A grounding judge splits each answer into atomic claims and checks each against the retrieved chunks only, quoting evidence. Any unsupported claim marks the answer as hallucinated. Hallucination rate = such answers / non-refused answers.
4. **What are the weaknesses of LLM-as-judge?** Shared blind spots with the generator (I use the same model by default, with `--judge-model` to change it), leniency to fluent text, and variance. I saw this directly: the judge once flagged the refusal sentence as an unsupported claim, which I fixed with a deterministic filter, and two full runs scored 89.5% and 100%.
5. **What did the evals actually find?** In the earlier full run the agent embellished the thin Dragon entry ("Luger's role... drives the narrative", unsupported), and the judge caught it. The final run passed 19/19. I say plainly that it is one small, non-deterministic sample.
6. **How does tracing work and why can't it break the agent?** OTel spans are created around each node and LLM call (via a decorator, `TracedLLM`, and a node wrapper). A custom exporter writes to SQLite on span end. Every tracing operation is wrapped in try/except, and a test makes the exporter throw and checks the answer still returns.
7. **How are tokens and cost computed?** Tokens come from the response's `usage_metadata`. Cost = tokens times a per-million-token price table in config, so it is an estimate and overridable.
8. **How do you handle rate limits?** `ChatGroq` retries are turned off; my wrapper retries on 429, 5xx, timeouts and malformed structured output with exponential backoff and jitter, honouring `retry-after`, and records the retry count on the span. One eval had 44 retries, which is why average latency (11 s) overstates model speed.
9. **Why SQLite?** One file, transactional, plain SQL, easy JSON export for a static site. Limits: one writer, no retention, not for high volume. A real deployment would swap the exporter (OTel makes that cheap).
10. **What would you do next / what is not built?** Roadmap, explicitly not built: a second agent for comparison, lineage graph, Kafka streaming, Kubernetes, scheduled evals with trend charts, and human-labelled data to measure the judge's accuracy. Today's evals run on demand; CI runs lint and offline tests only.
