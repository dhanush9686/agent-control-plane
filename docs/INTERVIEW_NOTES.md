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


## Extensions added later (be precise about what is measured)

- **Independent grader + two-step checking:** the grader is a different model family; extraction never sees the context. Built and unit-tested; its accuracy on the 38 labelled answers is **not yet measured end to end**, so do not quote an accuracy.
- **Naive-RAG baseline and paired sign test:** built to isolate what the judge loop buys; the 58-question comparison has **not been run**, so make no claim about the result.
- **Lineage graph:** claims linked to the chunk containing the judge's evidence quote; loaded into a real local Neo4j and queried. Built from the 19-question run.
- **Kafka transport:** spans keyed by run_id, an idempotent consumer, demonstrated end to end with a real broker.
- **Kubernetes:** dashboard deployed on a local minikube; the eval CronJob only passed an API-server dry run.
- **Real-world constraint worth telling:** Groq's free tier capped `gpt-oss-120b` at 200,000 tokens/day, which stopped the big evaluation; I diagnosed it from the 429 message and the rate-limit headers rather than guessing, and added parallel workers and shorter timeouts. Ask yourself the follow-up: "why did evals stall?"

### Three more likely questions
11. **Why a different model as grader?** To reduce self-preference and shared blind spots; I would measure it with labelled examples (the harness exists) before claiming it is better.
12. **Why Kafka if SQLite works?** It decouples the agent from the database and lets several consumers read spans; it is opt-in and I only demonstrated a single local broker, not scale.
13. **How would you know the judge loop helps?** Run the naive baseline on the same questions and use a paired sign test on discordant questions; the comparison is built but not run yet.


## The document service (new): what to say, and what not to claim
- **Pitch:** "Upload a 400-page PDF and ask questions with page citations. Bedrock for models and embeddings, Pinecone for vectors,
  hybrid search, a sandboxed parser, per-user isolation, quotas and a daily spend cap, and every question traced."
- **Measured, with caveats:** 5/5 hidden facts answered with the right page on a 410-page document; ingest 111 s and $0.0045; about $0.26 per 1,000
  questions. That is one synthetic document and 14 questions per setup, so say "it works end to end", not "it is accurate".
- **Honest findings to volunteer:** the cheapest model (Nova Micro) missed a fact among 10 chunks and refused an injected question, so I measured Nova
  Lite against it and upgraded one notch; keyword-only search matched hybrid on my keyword-friendly benchmark, so hybrid's advantage on paraphrases is
  a design argument, not a measurement; a real run stalled for minutes because the provider asked for a 14-minute wait and my retry code obeyed it,
  which I fixed with capped sleeps, a per-call deadline and immediate failover.
- **Likely questions:** why numpy was removed (FAISS and PyTorch crash in one process on macOS, and Pinecone makes search a service); why Pinecone
  namespaces per user and ids like `doc#i` (isolation, and delete-by-prefix because delete-by-filter is not on every plan); why eventual consistency
  needs a "wait until visible" step; how the spend cap works (tokens x price table, global per day); what a prompt-injection defense can and cannot do.

## Assurance layer for payment agents: what to say, and what not to claim
- **Pitch (20 s):** "I built a harness that tests an agent with payment tools the way you would test a payments
  service: a policy layer the model cannot talk past, an audit trail, and a scenario suite that runs with the guards on
  and off. On Nova Lite the unguarded agent reached a critical outcome in 39% of runs; with the layer, 0 of 93."
- **Best story:** the model turned 100,000 paise into "₹100,000.00" (100x); the numeric guard caught it. Also the
  injected "refund pay_A002" note: the model *tried* it in some runs, the provenance rule stopped it.
- **Why drafts only:** an agent should never be the last line before money moves; humans approve, with idempotency keys.
- **Why attempted vs effective:** attempted tracks the model (it drifts on upgrades); effective tracks the system.
  A model swap that raises attempted but not effective is safe; one that raises effective is a release blocker.
- **Do not claim:** that this is how any company does it, that it is production-grade, that 31 scenarios prove
  safety, or that the CIs are tight (repeats are not independent). It is a prototype on synthetic data.
- **If asked what next:** more scenarios from real incident classes, a second model on the same suite, anchoring the
  audit head hash externally, merchant-level rate limits, and wiring the gate into the release pipeline.
