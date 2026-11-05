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

# Extensions (added after the first release)

## 12. Independent grader, two-step claim checking
- **Why:** with the same model answering and grading, shared blind spots and self-preference are likely. The default grader is now `qwen/qwen3.8-27b` (a different model family, still free on Groq); `--judge-model same` restores the old behaviour. Claim extraction and verification are separate calls: extraction never sees the retrieved context, so it cannot be nudged by it.
- **Cost:** two LLM calls per non-refusal answer instead of one (skipped when there are no claims). Eval wall-clock time roughly doubles on the free tier.
- **Measured, not assumed:** `acp.evals.judge_check` scores the grader on 38 labelled answers (swapped names, changed numbers, true-but-absent facts, refusals). Those labels are author-written adversarial cases, **not** independent human annotation; add real human labels in `evals/judge_labels_human.yaml`.

## 13. Confidence intervals instead of bare percentages
- 95% Wilson score intervals on every headline rate. With 58 questions an interval is still wide; showing it stops us over-reading a 2-question difference. Run-to-run variance is shown by keeping earlier evals and a trends view.

## 14. Larger corpus built to expose training-data leakage
- The Nolan entries were chosen because the answering model already knows many of these facts. Questions such as "who was the cinematographer of Interstellar?" have a true real-world answer that the knowledge base deliberately lacks; the correct behaviour is to refuse. That tests grounding discipline, not trivia.
- **Limit:** the corpus is still 31 chunks written by the author in their own words. It is not a benchmark corpus.

## 15. Second agent = naive RAG baseline, paired comparison
- Same retriever, prompt and model; the only difference is the judge/refine loop. This isolates what the loop buys. Results are compared per question with an exact sign test on discordant pairs, because with few differing questions raw win counts mislead.

## 16. Lineage graph without requiring a graph database
- Nodes/edges (Question, Run, Chunk, Document, Answer, Claim) are built in plain Python and rendered by the dashboard. The same graph exports as idempotent Cypher (MERGE on unique ids with constraints) and loads into a local Neo4j Community container. A claim is linked to a chunk when the judge's verbatim evidence quote matches that chunk's text (exact, then a fuzzy 80% word overlap); a quote that matches no chunk leaves the claim unlinked.

## 17. Kafka as an optional trace transport
- A `KafkaSpanExporter` publishes JSON span records keyed by `run_id` (so one run's spans stay ordered on one partition); `scripts/kafka_consumer.py` writes them into SQLite using the same `write_record` as the direct exporter, which is idempotent (so redelivery is harmless). Poison messages are skipped. Default mode stays direct SQLite; Kafka is opt-in via `ACP_TRACE_MODE=kafka`.
- Broker runs on host port 19092 to avoid clashing with a Kafka already on 9092.
- **Also added:** `batch` mode (background thread; flushed before a run returns).

## 18. Scheduled evals and trends
- `.github/workflows/eval.yml` runs the full eval weekly and appends a point to the committed `results/history.jsonl`; the dashboard's Trends tab draws it. It needs one repository secret (`GROQ_API_KEY`, free key). Without it the job exits cleanly. The workflow has not yet run on GitHub at the time of writing.

## 19. Kubernetes: what is and is not verified
- The static dashboard Deployment/Service was built and deployed to a local minikube and served correctly through a port-forward. The eval CronJob manifest was validated by the API server (`--dry-run=server`) but not executed (large image, needs a key).

## 20. Public human-made data, fetched not committed
- **Why:** our corpus and grader labels are author-written. SQuAD 2.0 gives human-written answerable *and unanswerable* questions (a refusal test the author did not design); RAGTruth and FEVER give grader labels made by human annotators. Results are reported per dataset and never blended with the author-written labels.
- **Fetched, not committed:** two of the three dataset cards state no redistribution licence, so `make benchmarks` downloads small seeded slices through the public Hugging Face rows API into a gitignored folder, with a manifest.
- **Caveat we saw immediately:** SQuAD 2.0 "unanswerable" questions are sometimes negation tricks that can be answered truthfully, and a strict grader may correctly reject a dropped qualifier ("most"); expect a ceiling below 100%. We report it instead of cleaning the data to look better.

## 21. Move to Amazon Bedrock; which Nova model (measured)
- **What changed:** the LLM provider is now Amazon Bedrock (Converse API) instead of Groq; embeddings are Titan Text
  Embeddings v2 (1024-dim) instead of local MiniLM; Pinecone index is `acp-docs-<dim>`. Groq stays selectable with
  `ACP_LLM_PROVIDER=groq`; MiniLM with `ACP_EMBEDDING_PROVIDER=minilm`. The legacy film demo still retrieves with local
  FAISS + MiniLM (not migrated).
- **Not free any more:** Bedrock bills per token. Guard rails: per-user daily question/upload quotas, per-model token
  budgets, and a global daily spend cap (`ACP_DAILY_BUDGET_USD`, default $1.00) counted from real token usage times a
  price table (`acp/config.py`, override with `ACP_PRICES_JSON`; prices are my best knowledge, verify on the AWS pricing
  page). Embedding spend is counted too.
- **Model choice by experiment, not by price alone.** Live A/B on the 410-page demo document, 14 questions
  (5 clean, 5 paraphrased, 2 unanswerable, 2 injected):

  | judge / answer | score | injected | est. cost for the 14 questions |
  |---|---|---|---|
  | Micro / Micro | 13/14 | 1/2 | $0.0022 |
  | Lite / Micro | 13/14 | 1/2 | $0.0031 |
  | Micro / Lite | 14/14 | 2/2 | $0.0028 |
  | Lite / Lite | 14/14 | 2/2 | $0.0036 |

  Chosen: Lite for everything, Micro fallback. Caveats: tiny sample; the decisive evidence is the targeted failures
  (Micro judge missed a fact among 10 chunks; Micro refused an injected question), not the 1-question gap.
- **Retries are now polite:** one shared helper caps each sleep at 10 s, gives each call a 60 s deadline, and abandons a
  provider that asks for more than 15 s, so a fallback model serves the request (this removed the earlier multi-minute stall).
- **Titan embeds one text per request** (no batch endpoint): a 410-page, 1,338-chunk document took 111 s to ingest (vs 42 s with
  local MiniLM) and cost $0.0045 in embeddings.
- **Not available here:** a Bedrock rerank model is not offered in ap-south-1, so reranking stays off (the optional
  cross-encoder is a local open-source model).

## 22. Assurance layer for payment agents (synthetic, measured)
- **Why:** an agent with payment tools fails in ways a document Q&A bot cannot: wrong merchant's data, money moved on
  injected text, card data in an answer, a figure the ledger never held. Prompts alone do not stop these, so enforcement
  lives in ordinary code between the model and the ledger (`acp/assure/policy.py`, `tools.py`).
- **Measure the enforcement, not the model:** every scenario runs guarded and unguarded with identical tools and
  prompt, so the gap is the value of the guards. Violations are counted as *attempted* (model behaviour, which drifts)
  and *effective* (outcome). Scoring uses ledger ground truth, no LLM judge.
- **Result (Nova Lite, 31 scenarios x 3):** 0/93 critical outcomes guarded vs 36/93 unguarded; pass rate 94.6% vs 49.5%.
- **Trade-offs accepted:** the model may not do arithmetic (totals must come from a tool), so a correct-looking sum is
  withheld; a refund must be named and sized by the user's own words (heuristic provenance); refunds are drafts only.
- **Honesty notes:** synthetic data; unguarded mode is a counterfactual; repeats are not independent so CIs are
  optimistic; one scenario was corrected after the first run (`inject-1`), one keyword list widened after the final run
  (`lookup-6`, not re-run).
- **Strip `<thinking>` text:** Nova prints reasoning tags into answers; they are removed in both modes before guards run.
- **Audit log:** SHA-256 hash chain in SQLite (migration 5). Tamper-evident only; anchor the head externally in production.

## 23. Dashboard: assurance tab, replay sandbox, server-side login
- **Replay, not a live model, in the browser:** the public page cannot call Bedrock (cost, keys, abuse). Instead a
  deterministic scripted attacker is run through the real policy engine, masking, numeric check and ledger, with guards
  off and on (`acp/assure/replay.py`), and the output is saved as data. It is labelled as a scripted attacker everywhere;
  it demonstrates enforcement, and says nothing about how often a real model misbehaves (the live suite measures that).
- **Login is enforced on the server, not in the page.** A password check in browser JavaScript is decoration. The
  data is bundled into a serverless function (`dashboard/api/_data.js`, gitignored, generated) and returned only when a
  signed cookie is valid; `.vercelignore` stops `data/` being uploaded as public static files. The session is
  `<expiry>.<HMAC-SHA256(password, expiry)>`, so there is no user table, it expires on its own, and changing the password
  invalidates every session. Comparison is constant-time; with no password configured everything is refused.
- **Tested adversarially** (`tests/dashboard_auth_check.js`, run by pytest): wrong/malformed passwords, forged,
  extended, expired and other-password cookies, path-style file names, password change. I confirmed the test fails when the
  signature check is disabled.
- **Limits accepted:** one shared password; no cross-instance brute-force counter on serverless (a fixed delay only); the HTML/JS
  shell is public, only data is gated; the repo still contains the demo data files, so making the repo public would
  expose them regardless of the login.
