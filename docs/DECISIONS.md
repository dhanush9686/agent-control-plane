# Design decisions

Each entry: the choice, why, and what it costs us. Everything here describes code that exists. Entries for the removed film demo and its extras (LLM grounding judge, golden set, regression gate, FAISS index, Kafka, Neo4j lineage, Kubernetes, public benchmarks) were deleted; see #24 and git history at `2c77257`.

## 1. LangGraph StateGraph with a conditional edge (not a while-loop)
- **Choice:** `retrieve -> judge -> (refine_query -> retrieve | synthesize) -> END`, with the routing in `acp/graph.py::make_router`.
- **Why:** the loop decision ("sufficient? attempts left?") is a pure function of state, so unit tests cover all three branches without any LLM. The retry cap (`max_attempts=2`) is a visible setting, not a hidden `break`.
- **Cost:** one more dependency and a little ceremony for a graph this small.

## 2. Dependency injection for the LLM and retriever
- **Choice:** `make_nodes(llm, retriever, settings)`; the LLM is a small protocol (`complete`, `structured`, plus `tool_step` for the assurance agent).
- **Why:** unit tests use a scripted fake LLM and fake retriever and run offline in under a second; the real pieces (Bedrock + Pinecone) are covered by the opt-in live tests (`RUN_LIVE=1`) and by the live evals.
- **Cost:** a fake can drift from reality; that is why the live tests and real eval exist.

## 3. One LLM door with our own retry/backoff
- **Choice:** one shared `call_with_retries` (`acp/llm.py`) used by the Bedrock and Groq clients. It retries throttling, 5xx, timeouts, dropped connections and malformed structured output, honouring `Retry-After`.
- **Why:** owning the loop lets us count retries per call and put them in the trace. It also lets us stop waiting: every sleep is capped at 10 s, each call has a 60 s deadline, and a provider that asks for more than 15 s is abandoned so a fallback model serves the request (a real run once sat in a 14-minute sleep; see KNOWN_ISSUES #1).
- **Cost:** retries inflate latency numbers; the README says so.

## 4. OpenTelemetry spans, custom SQLite exporter
- **Choice:** standard OTel SDK for spans; our own `SpanExporter` writes `runs` and `spans` tables.
- **Why:** OTel is the industry vocabulary (spans, parents, attributes) and keeps the door open to swap the exporter for a vendor later. A private `TracerProvider` (no global state) keeps tests isolated.
- **Never crash the agent:** span creation, attribute setting and export are all wrapped; failures are logged and swallowed. A test forces the exporter to fail.
- **Cost:** `SimpleSpanProcessor` writes synchronously, fine at this scale, not for high throughput.

## 5. SQLite as the trace store
- **Why:** a single file, no server, transactional, queryable with plain SQL, trivially exported to JSON for a static dashboard. Indexes on `spans(run_id)`, `spans(name)`, `runs(started_at)`.
- **Cost:** one writer at a time, no retention policy, not a production observability backend.

## 7. Citations accepted in two bracket styles
- A real run showed the model writing `【Source: X】` despite being told to use `[X]`. The prompt was tightened and the extractor accepts both, instead of failing correct answers on formatting.

## 10. Static dashboard, no build step, no third-party scripts
- Vanilla JS. All data is rendered via `textContent`, so exported text cannot inject HTML. The deployed site serves its data only behind a login (#23).

## 13. Confidence intervals instead of bare percentages
- 95% Wilson score intervals on every headline rate (`acp/evals/report.py`). With 31 scenarios an interval is still wide, and repeats of one scenario are not independent, so the intervals are optimistic; showing them still stops us over-reading a small difference.

## 21. Move to Amazon Bedrock; which Nova model (measured)
- **What changed:** the LLM provider is now Amazon Bedrock (Converse API) instead of Groq; embeddings are Titan Text
  Embeddings v2 (1024-dim) instead of local MiniLM; Pinecone index is `acp-docs-<dim>`. Groq stays selectable with
  `ACP_LLM_PROVIDER=groq`. The local MiniLM embedder was later removed with the film demo (#24).
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

## 24. Removed the film demo and everything built around it
- **Why:** the project now serves payments use cases only. The film corpus was a stand-in domain; keeping it, its golden
  set and the dashboard tabs built on it diluted the story and left dead code.
- **Removed:** the film corpus and local FAISS/MiniLM retrieval, the CLI, the 19-question golden set and judge labels,
  the golden-set eval runner, the LLM grounding judge and its accuracy harness, the agent-vs-agent comparison and the
  naive-RAG variant, the regression gate for that eval, trend history, the lineage export, the Kafka transport and
  consumer, the Neo4j loader, the Kubernetes manifests and dashboard/eval Dockerfiles, the public-benchmark loaders,
  the scheduled eval workflow, the film dashboard tabs, and the film fixtures in the tests.
- **Kept:** the assurance layer, the document service (PDF Q&A on Bedrock + Pinecone), tracing, the audit log, the
  Wilson-interval statistics, the long-document retrieval eval and the login-protected dashboard.
- **Cost of this choice:** the grounding judge and the golden-set machinery were reusable for a payments corpus; if a
  documents eval is wanted later it has to be rebuilt (or restored from `2c77257`). The `eval_runs` / `eval_results`
  tables stay in the schema (no migration), unused.

## 25. Live data from a Razorpay test account, published to Redis
- **Why:** the dashboard was a snapshot built offline. Production merchant data is neither obtainable nor wanted, so
  the honest "real" source is a payment provider's **test mode** (real API shapes, fake money) plus real model decisions.
- **Read-only by construction:** the adapter (`acp/assure/razorpay.py`) can only GET, refuses non-test keys, copies a
  field whitelist (no email, phone, VPA, card ids, raw JSON), and keeps refund requests in an in-memory overlay that is
  never sent to Razorpay. Seeding is a separate module that can only `POST /orders` and defaults to a dry run.
- **Ledger protocol:** the agent, policy and scorer now depend on `LedgerBackend`, so the synthetic sandbox and the
  Razorpay adapter run the same code. Scenarios for real data are generated from the snapshot, with expected figures
  read from it, so there is nothing to hand-maintain.
- **Redis as a noticeboard:** a sync writes with a write token that exists only where the sync runs; the dashboard
  function uses only a read-only token, only read commands, and only after the login. A test fails if the dashboard
  could use the write token.
- **Honesty:** tenancy is emulated with notes tags; settlements may be unavailable in test mode; capture is periodic;
  every provenance line is shown on the Live tab.
- **Verified so far:** the offline tests, the Node tests and a read-only run against the real test account (keys valid,
  account empty). **Not yet verified:** a sync on seeded data, publishing to a real Upstash database, and how the
  deployed page looks on live data.

## Live events: queue worker, one write path on Vercel, polling

- **Agent runs in a worker, not in the webhook function.** The webhook only verifies the HMAC signature, whitelists the payment and queues it.
  Running the Python agent inside Vercel would need long-lived AWS keys in Vercel env vars, the whole package bundled, and an audit chain that
  loses continuity between invocations. Cost: events wait for the worker (shown plainly on the page).
- **One deliberate exception to "Vercel only holds the read-only Redis token".** The webhook function uses the integration's write token
  (`KV_REST_API_TOKEN`) because it must enqueue. It is confined to `dashboard/api/_events.js`, which refuses any command outside
  SET/LPUSH/LTRIM/EXPIRE/LLEN; page-serving code still uses the read-only token only. Upstash tokens cannot be scoped per command.
- **No Razorpay secret on Vercel.** Checkout needs only the public `rzp_test_` key id; the webhook secret verifies deliveries.
- **Polling, not streaming.** Upstash REST plus Vercel functions do not give a cheap push channel; 3 s polling while the tab is visible is honest and simple.
- **Re-whitelisting.** The Node receiver keeps only the fields the worker reads, and the worker runs `whitelist_payment` again (`snapshot_from_webhook`).
- **Daily spend cap in Redis**, because the sync's cap is in-process and resets each run.

## Personal accounts instead of one shared password

- **Accounts in Redis, read-only on the website.** `acp:user:<email>` holds a scrypt hash (N=16384, r=8, p=1, 64-byte output) and the
  role; `make dashboard-user` writes them from a trusted machine. No new database, and the sign-in function reads the register with the
  read-only token.
- **Stateless signed sessions, revocable by lookup.** The cookie is a signed `{email, role, exp}`; each request re-checks the register
  (cached 30 s) so a disabled account loses access within a minute. If the register cannot be read, the session is refused (fail closed).
- **A second, narrow write path** for failed-attempt counters and the sign-in log (`INCR/EXPIRE/DEL/LPUSH/LTRIM/GET`, confined to
  `_users.js`), added to the webhook's existing exception. Without the write token the lockout is skipped and the fixed delay remains.
- **Bootstrap.** While no account exists the shared `DASHBOARD_PASSWORD` still signs in (so the first admin can be created); once any account
  exists it stops working. Old shared-password sessions expire on their own within 8 hours.
- **No user enumeration.** An unknown email and a wrong password give the same message and cost the same time (a dummy hash is computed).
- **Rejected:** a hosted identity provider (right for production, but the project's point is a small, inspectable system), magic links (need
  email delivery), roles beyond admin and viewer (nothing to protect yet beyond the register).

## A cited rules library, off by default

- **Curated notes, not a crawl.** Six short paraphrased notes, each naming its RBI source and listing what was **not confirmed** from the
  primary text. Claims that only a secondary summary made (for example a card-storage clause in the 2025 directions, or a fixed settlement
  deadline) are recorded as unconfirmed, not stated as rules.
- **Keyword search, no vectors.** At this size BM25-style scoring is testable and enough; the existing Pinecone service is a separate product.
- **Rupee figures live in `*_paise` fields**, so the existing numeric-fidelity guard checks an answer's regulation figures exactly as it checks
  ledger figures. A made-up "₹250 per day" is withheld.
- **Off unless `policy_docs=True`.** Adding a tool changes how a model behaves, so the saved benchmark (run without it) stays comparable.
- **Abstention is scored.** Two of the eight scenarios ask something the notes cannot answer; passing means saying so.
- **Not run yet.** The suite exists and its plumbing is tested with a scripted model; no real model has been scored on it.

