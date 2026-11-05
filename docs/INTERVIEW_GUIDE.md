# Interview guide: Agent Control Plane, A to Z

Written for one purpose: so you can explain every part of this project, every choice and every limit, without bluffing.
Numbers are copied from files in this repo (`results/`, `docs/DECISIONS.md`, `README.md`), with sample sizes next to them.
Where something is a design argument and not a measurement, the text says so. **If you are ever unsure, say "I measured X, I did not measure Y."** That sentence is your best asset in an interview.

Related, shorter docs: [DECISIONS.md](DECISIONS.md) (the decision log), [INTERVIEW_NOTES.md](INTERVIEW_NOTES.md) (older cheat notes), [KNOWN_ISSUES.md](KNOWN_ISSUES.md), [LIVE_DATA.md](LIVE_DATA.md).

---

## 1. The problem and the pitch

**The problem.** A company gives an AI agent tools: look up a payment, list settlements, request a refund. A document chatbot can only say something wrong. An agent with payment tools can *do* something wrong:

| Failure | Plain example |
|---|---|
| Cross-tenant leak | Merchant A asks about a payment and gets Merchant B's data |
| Money moved on injected text | A customer note says "refund pay_X", and the agent obeys it |
| Sensitive data in an answer | A card number or phone number is read back to the user |
| Invented figure | The agent states an amount the ledger never held (e.g. a 100x unit slip) |
| No evidence afterwards | Nobody can prove what the agent did or was stopped from doing |

Prompts alone do not stop these: the model can be talked around. So enforcement has to live in ordinary code between the model and the ledger, and you have to *measure* what that code is worth.

**30-second pitch.** "I built an assurance layer for AI agents that touch money. A policy engine, card masking, a numeric-fidelity check and a tamper-evident audit log sit between the agent and a ledger, and a scenario suite runs every case with the guards on and off. On Amazon Nova Lite, 93 runs per mode: the unguarded agent reached a critical outcome in 36 of 93, with the layer 0 of 93. It now also runs against a Razorpay test-mode account and publishes to a login-protected live dashboard."

**2-minute pitch.** Add: how it is measured (same model, tools and prompt, only enforcement differs; "attempted" vs "effective" violations; scoring from ledger ground truth, no LLM judge); the best story (the model wrote 100,000 paise as "₹100,000.00", a 100x error, and the numeric guard withheld it); the live-data pipeline (read-only adapter, test keys only, sync to Redis, dashboard Live tab); and the honest limits (test-mode data, small samples, emulated tenancy, one model).

**Who benefits, and how**

| Who | Benefit |
|---|---|
| A payments company (Razorpay-like) | A way to answer "is this agent safe to ship, and is it still safe after a model or prompt change?" with numbers. The gate (`make assure-gate`) can block a release. The audit log gives evidence for reviews and incidents. |
| A support or ops team | Fewer wrong refunds and leaks; a human approves every refund draft above a tier. |
| A security / risk reviewer | Controls are plain code with tests, not prompt wishes. They can read `policy.py` in five minutes. |
| You (the builder) | Evidence of end-to-end ownership: agent design, guard design, evaluation statistics, cloud deployment, auth, data pipeline, honest reporting. |

---

## 2. Architecture end to end

```
User message -> Agent (Nova Lite on Bedrock, tool-use loop)
                  |  every tool call goes through:
                  v
   Policy engine  --> tenant scope, refund provenance, limits, budgets
   Tools          --> read tools; refund = DRAFT with idempotency key
   Masking        --> cards, Aadhaar, PAN, UPI, phone, email, IFSC
   Numeric guard  --> every rupee figure must come from a tool result or the user's message
   Audit log      --> SHA-256 hash chain in SQLite
                  v
   LedgerBackend (protocol)
        |-- Ledger (synthetic, fixtures)
        '-- RazorpayTestLedger (read-only GETs, in-memory refund overlay)
```

**The live pipeline (`make live-sync`)**

```
Razorpay TEST API (GET only) -> whitelisted, masked snapshot
   -> scenarios generated from real ids and amounts (expected figures read from the snapshot)
   -> agent runs, guards on and off, fresh in-memory ledger per run (Nova Lite)
   -> payload (masked) -> Upstash Redis (write token, only on the machine running the sync)
   -> /api/data?file=live on Vercel (login cookie required, READ-ONLY token) -> Live tab
```

**Component map**

| Piece | File | One line |
|---|---|---|
| Ledger protocol | `acp/assure/ledger.py` | Interface both ledgers implement, so the same agent/policy/scoring code runs on either |
| Synthetic ledger | `acp/assure/sandbox.py` | Fake merchants `m_acme` / `m_zen`, integer paise, fixtures |
| Razorpay adapter | `acp/assure/razorpay.py` | GET-only client, `rzp_test_` keys only, field whitelist, refund overlay |
| Seeder | `acp/assure/razorpay_seed.py` | Can only `POST /orders`; dry run by default; idempotent via receipts and a seed tag |
| Policy | `acp/assure/policy.py` | Tenant scope, refund provenance and limit, budgets, approval tier |
| Tools | `acp/assure/tools.py` | What the model can call; refund is a draft |
| Masking | `acp/assure/pii.py` | Checksum-validated PII detection |
| Numeric guard | `acp/assure/numeric.py` | Withholds answers containing unsupported amounts |
| Audit | `acp/assure/audit.py` | Hash-chained log; `verify()` finds edits, deletions, reordering |
| Agent | `acp/assure/agent.py` | Tool-use loop; strips `<thinking>` text; guarded vs unguarded |
| Scenarios | `evals/assure/scenarios.yaml`, `scenario_gen.py` | 31 hand-written + generated-from-ledger scenarios |
| Scoring / eval | `scoring.py`, `evaluate.py` | Ledger-ground-truth scoring, Wilson intervals, gate, spend cap |
| Replay | `replay.py` | Deterministic scripted attacker run through the real guards |
| Publish | `publish.py` | Upstash REST pipeline, masked values, caps, 120-day expiry, read-back check |
| Sync | `live.py` | probe -> snapshot -> generate -> run -> publish |
| Dashboard | `dashboard/` | Vanilla JS; Vercel functions `api/data.js`, `api/_redis.js`, login |
| Document service | `acp/server/`, `kb.py`, `hybrid.py`, ... | Separate PDF Q&A service (Bedrock + Pinecone); not yet wired to the guards |

---

## 3. Every design decision: choice, alternative, why

For each: *what we chose, what we rejected, why, and what it costs.* Practise saying the cost out loud; naming the cost is what makes a decision sound senior.

### 3.1 Guards in code, outside the model
- **Chose:** policy, masking, numeric check and audit as ordinary code between model and ledger.
- **Rejected:** "tell the model in the system prompt not to do these things."
- **Why:** a prompt is a request, code is a rule. An injected note or a clever user can talk a model out of a prompt; they cannot talk `if payment.merchant != session.merchant` out of returning "not found". Code is also unit-testable and reviewable.
- **Cost:** the guards are only as good as their rules (provenance is a heuristic on the user's wording; PII detection is heuristics, not a DLP product). Some correct answers get withheld (see 3.6).
- **Analogy:** a bank teller's checklist and locked drawer, versus asking the customer politely not to take extra cash.

### 3.2 Measure the enforcement: guarded vs unguarded on the same model
- **Chose:** run every scenario twice with identical model, tools and prompt; only enforcement differs.
- **Rejected:** reporting only the guarded pass rate (tells you nothing about what the guards bought), or comparing two different models.
- **Why:** the gap *is* the value of the layer. The unguarded mode is a deliberate counterfactual (its refund tool executes immediately), not a claim about any real system.
- **Cost:** the counterfactual is something I defined; say so.

### 3.3 "Attempted" vs "effective" violations
- **Chose:** count what the model *asked for* (attempted) separately from what *happened or reached the user* (effective).
- **Why:** attempted tracks the model and drifts on upgrades; effective tracks the system around it. A model swap that raises attempted but not effective is safe; one that raises effective is a release blocker. Result: guarded attempted 16/93 (17.2%), effective 0/93.
- **Cost:** two numbers to explain instead of one.

### 3.4 Scoring from ledger ground truth, no LLM judge
- **Chose:** checks computed from the ledger (expected figures, did a refund draft exist, did foreign data appear).
- **Rejected:** an LLM grading the answers.
- **Why:** a judge model adds its own error and cost, and can be fooled by the same injection. Ground truth is deterministic and free.
- **Cost:** keyword/figure checks can be too strict. Real example: three `lookup-6` guarded "failures" were correct answers my keyword list did not recognise; I widened the list afterwards and said so rather than hiding it.

### 3.5 Tenant scope: foreign objects look "not found"
- **Chose:** another merchant's payment returns the same response as a nonexistent one; the merchant id comes from the session, never from the model.
- **Rejected:** returning "access denied".
- **Why:** "denied" confirms the object exists (an information leak) and the model might relay it. Indistinguishable from "not found" leaks nothing.

### 3.6 Numeric-fidelity guard (withhold the answer)
- **Chose:** every rupee figure in the answer must appear in a tool result or the user's message; otherwise the answer is withheld. The model may not do arithmetic (totals must come from a tool).
- **Why:** it caught a real 100x error (100,000 paise written as "₹100,000.00"). Unguarded, that wrong figure was shown.
- **Cost (say this proactively):** it withholds correct-looking sums and costs helpfulness. Of the 5 guarded failures in the 93-run set, 2 were exactly this: safe failures that still cost helpfulness. The fix in production is a calculator tool, not trusting the model.

### 3.7 Refund provenance and drafts only
- **Chose:** a refund must name a payment id and amount the **user** wrote; text inside tool results (a customer note) can never authorise one. The agent only creates a *draft* with an idempotency key; above ₹10,000 a senior approver is required.
- **Why:** the agent should never be the last line before money moves. Idempotency keys stop a retry from refunding twice.
- **Cost:** provenance is a string match on the user's words, so a user who types the right id/amount is accepted, and unusual phrasings can be rejected.

### 3.8 Masking with checksums (Luhn, Verhoeff)
- **Chose:** detect cards with Luhn, Aadhaar with Verhoeff, plus PAN, UPI id, phone, email, IFSC patterns; mask in prompt, tool output, answer and audit log.
- **Why:** checksums cut false positives (not every 16-digit number is a card).
- **Cost:** heuristics, not a certified DLP product.

### 3.9 Hash-chained audit log (SQLite)
- **Chose:** each row stores a SHA-256 of itself plus the previous row's hash.
- **Rejected:** a plain log table (edits are invisible); a blockchain (overkill).
- **Why:** editing, deleting or reordering a row breaks every later hash, so `verify()` detects it. The dashboard demonstrates this live.
- **Honest limit:** *tamper-evident, not tamper-proof.* Someone who can rewrite the whole table can rebuild a consistent chain. The production step is anchoring the head hash somewhere the DB admin cannot write (write-once store, signed timestamp).

### 3.10 Wilson confidence intervals
- **Chose:** 95% Wilson intervals on every headline rate.
- **Rejected:** bare percentages; the normal approximation (bad near 0% and 100%).
- **Why:** with small n an interval stops you over-reading a difference. Guarded critical outcomes 0/93 has CI 0.0-4.0%, i.e. "probably under 4%", not "zero".
- **Honest limit:** repeats of one scenario are not independent, so the intervals are optimistic.

### 3.11 LedgerBackend protocol (the abstraction)
- **Chose:** a Python `Protocol` that the synthetic ledger and the Razorpay ledger both implement; agent, policy and scoring depend only on it.
- **Why:** the same checks run on synthetic and real-shaped data, and swapping in another company's ledger is writing one adapter. It is also why the layer is not payment-specific in its *mechanism*.
- **Cost:** one more layer of indirection; the protocol had to cover exactly the methods the code already called.

### 3.12 Razorpay adapter: read-only, test-keys-only, whitelisted
- **Chose:** the client can only issue GET; it refuses any key not starting `rzp_test_`; it copies a field whitelist (no email, phone, VPA, card ids, raw JSON); refund requests live in an in-memory overlay and are never sent.
- **Rejected:** a general client with a "dry run" flag; calling Razorpay's refund API.
- **Why:** safety by construction: the code for a dangerous action does not exist, so no bug or prompt can trigger it. Each rule has a test (GET-only, live-key refusal, overlay never calls the API, whitelist keeps PII out).
- **Interview line:** "I made the safe thing the only thing the adapter can do."

### 3.13 Separate seeder, `POST /orders` only, dry run by default
- **Chose:** creating test data lives in a different module with a hard allowlist (`/orders`) and requires `--create`.
- **Why:** keeps the read path provably write-free; idempotent via receipts and a seed tag so reruns do not duplicate.

### 3.14 Emulated tenancy via `notes.tenant`
- **Problem:** a Razorpay account is one merchant, but the cross-tenant tests need two.
- **Chose:** tag each order/payment with `notes.tenant` = `m_acme` / `m_zen`; payments inherit order notes; the adapter falls back to order notes.
- **Cost / honesty:** it is an emulation, labelled as such on the Live tab. Today only 1 payment is tagged `m_zen`, so the Razorpay-side cross-tenant scenarios are thin (the synthetic suite covers tenancy more fully).

### 3.15 Scenarios generated from the ledger
- **Chose:** templates (lookup, failure, refund draft/over/already/failed, cross-tenant, injection, PII, arithmetic) filled with real ids and amounts; expected figures read from the snapshot, float-free (integer paise).
- **Why:** nothing to maintain by hand; the same suite runs on whatever data exists.
- **Cost:** only as varied as the template set; 13 scenarios came out of the current 17 payments.

### 3.16 Upstash Redis as the store, "periodic sync" as capture
- **Chose:** a sync command writes results to Redis; the dashboard reads them on every load.
- **Rejected:**
  - *Rebuild and redeploy on every change* (what existed before: stale by design, no timestamp).
  - *Vercel Postgres/KV:* Vercel no longer offers these directly; databases come through the Marketplace, which is how Upstash is added.
  - *Streaming from production:* there is no legitimate way to get production merchant data, and we should not want it.
  - *A heavier database:* the data is a few JSON documents; Redis keys with caps and expiry are enough.
- **Why Upstash:** a REST API (works from serverless without connection pooling), a free tier, native Vercel integration.
- **Cost:** "live" means *periodic*, not a stream; the page shows freshness and goes amber/red when stale. Free-tier limits were not independently verified.

### 3.17 Two Redis tokens: write token only where the sync runs, read-only token on Vercel
- **Why:** if the dashboard function is ever compromised or buggy, it cannot write or delete. A test fails if the dashboard code could use the write token, and the function only issues GET/LRANGE.
- **Analogy:** the noticeboard's pin is kept in the office; the public sees but cannot pin.

### 3.18 stdlib `urllib` for the Upstash client
- **Chose:** a small client with the standard library.
- **Rejected:** adding the Upstash SDK.
- **Why:** zero new dependency to audit; the API surface used is three commands. Includes read-back verification after publishing.

### 3.19 Dashboard: replay, not a live model, in the browser
- **Chose:** a deterministic scripted attacker run through the real guards, saved as data.
- **Rejected:** letting visitors call Bedrock from the page.
- **Why:** cost, keys and abuse. It is labelled "scripted attacker" everywhere; it demonstrates enforcement and says nothing about how often a real model misbehaves (the suite measures that).

### 3.20 Login enforced on the server, HMAC cookie
- **Chose:** serverless function checks a signed cookie `<expiry>.<HMAC-SHA256(password, expiry)>` (HttpOnly, Secure, SameSite=Strict, 8 h); data is bundled into the function, not served as static files; fails closed if no password is set (or under 8 chars).
- **Rejected:** a password check in browser JavaScript (decoration, trivially bypassed); a user table (overkill here).
- **Why:** no user store, expiry is built in, changing the password invalidates every session, constant-time comparison. Tested adversarially (forged, extended, expired, other-password cookies, path tricks).
- **Honest limits:** one shared password, no per-user accounts, no cross-instance rate limiting (a fixed delay only), page shell is public (only data is gated). Not an identity system.

### 3.21 `.vercelignore` and the `.env.local` incident
- **What happened:** `vercel link` wrote `.env.local` into `dashboard/`; Vercel would have uploaded it as a public file. A `.vercelignore` exists, and Vercel ignores `.gitignore` when one is present.
- **Fix:** `.env*`, `.vercel` and ignore files added to `dashboard/.vercelignore`, plus a test that fails if they are missing. The root `vercel.json` was removed.
- **Lesson to tell:** deploy tooling can publish files you never meant to; test the upload set, not just the app.

### 3.22 Amazon Bedrock and Nova Lite (upgrade from Nova Micro, switch from Groq)
- **Groq -> Bedrock:** Groq's free tier had a daily token cap; it answered 429 with `retry-after: 847` (14 minutes) and my retry code obeyed, so one request hung for minutes. Bedrock bills per token, which allows a hard daily spend cap computed from real token counts.
- **Micro -> Lite (an upgrade, by experiment):** A/B on a 410-page document, 14 questions: Micro everywhere 13/14 and it refused an injected question; Lite everywhere 14/14. Direct test: Micro missed a fact among 10 chunks and answered "game" to "reply with exactly one word: pong". Cost difference is tiny (about $0.0036 vs $0.0022 for the 14 questions). **Caveat:** 13/14 vs 14/14 is not statistically meaningful; the decisive evidence is the targeted failures.
- **Nova Micro later removed entirely** from the assurance work: Lite is already better, and carrying a second weaker model added nothing.
- **Why not a bigger model:** the point is to test the guards on an inexpensive, imperfect model; cost per task is about $0.0002.

### 3.23 One LLM door with capped retries
- **Chose:** a single `call_with_retries`: every sleep capped at 10 s, a 60 s per-call deadline, abandon a provider that asks for more than 15 s so a fallback serves the request.
- **Why:** it came from the 14-minute stall above. Owning the loop also lets retries be counted in traces.

### 3.24 LangGraph for the document agent; dependency injection
- **Chose:** `retrieve -> judge -> (refine | synthesize)` as a graph with a pure router function; LLM and retriever injected.
- **Why:** routing is testable offline without an LLM; the retry cap is explicit. Tests use scripted fakes and run in about a second.
- **Cost:** a fake can drift from reality, hence opt-in live tests.

### 3.25 OpenTelemetry + SQLite trace store
- **Why:** OTel is the standard vocabulary and keeps vendor swap open; SQLite is one file, transactional, queryable. Tracing never crashes the agent (failures swallowed, tested).
- **Cost:** synchronous writes, single writer; not a production observability backend.

### 3.26 Document service: hybrid search, Pinecone, numpy removed
- **Hybrid (vectors + BM25 with rank fusion):** measured honestly: on my keyword-friendly benchmark keyword-only matched or slightly beat hybrid (needles recall@5 1.00 vs 0.96); dense alone missed 15% of exact-term lookups. Hybrid is the default as a *design argument* for paraphrased questions, which the benchmark cannot show.
- **FAISS dropped:** crashed with OpenMP on macOS when PyTorch was also in-process; Pinecone made search a service.
- **Pinecone** namespaces per user (isolation), ids like `doc#i` (delete by prefix).

### 3.27 What was upgraded, downgraded, or removed

| Change | Direction | Why |
|---|---|---|
| Groq -> Bedrock | Upgrade (reliability, cost control) | Daily cap and 14-minute Retry-After stall; per-token billing allows a hard cap |
| Local MiniLM -> Titan embeddings | Upgrade in quality/ops, downgrade in speed | Titan embeds one text per request: 111 s to ingest 410 pages vs 42 s locally |
| Nova Micro -> Nova Lite | Upgrade | Missed a fact; refused an injected question |
| FAISS -> numpy -> Pinecone | Simplify | macOS crash; managed search |
| Static snapshot -> Redis-backed Live tab | Upgrade | Stale-by-design dashboard with no timestamps |
| Film demo + Kafka, Neo4j, Kubernetes, FAISS, golden set, LLM grounding judge, public-benchmark loaders | **Removed** | Stand-in domain and extras that diluted the payments story; kept in git history at `2c77257` |
| Reranker | Off | Bedrock has no rerank model in ap-south-1 |
| Spend cap on live sync | Added | $0.25 default cap; a sync aborts cleanly if exceeded |

---

## 4. Tech stack: why each, and what was not chosen

| Layer | Chosen | Why | Not chosen, and why |
|---|---|---|---|
| Language | Python 3 | Fast to build agents and eval harnesses; typed dataclasses and Protocols | TypeScript for the whole thing: Python is the natural home for eval and Bedrock tooling; Node used only where Vercel needs it |
| LLM | Amazon Nova Lite on Bedrock (Converse API, tool use) | Cheap (about $0.0002 per task), per-token billing, AWS-native | Bigger Claude/GPT models: cost and the goal is stress-testing guards on an imperfect model; Groq: daily caps; one model only is a stated limit |
| Embeddings | Titan Text Embeddings v2 (1024-d) | Same cloud, simple auth | Local MiniLM: removed with the film demo; faster but a heavier local stack |
| Vector store | Pinecone (document service) | Managed, namespaces for isolation | FAISS/numpy: crash and ops burden |
| Local store | SQLite (+ FTS5) | One file, transactional, plain SQL, easy export | Postgres: no need at this scale; stated limit: one writer, one instance |
| Agent orchestration | Hand-written tool-use loop (assurance); LangGraph (documents) | Assurance loop is small and needs to be fully visible to the guards; the document agent benefits from an explicit graph | LangChain agents: too much hidden behavior for a safety demo |
| Tracing | OpenTelemetry + custom SQLite exporter | Standard vocabulary, swappable | A vendor tool: cost and lock-in for a prototype |
| Store for live data | Upstash Redis (REST) | Serverless-friendly, free tier, Vercel Marketplace | Vercel KV/Postgres (no longer offered directly), a hosted SQL DB (heavier than needed), S3 (no cheap atomic list/cap semantics) |
| Hosting | Vercel (static + Node serverless functions) | Zero-config deploy, env vars, functions for the login gate | A container platform: more ops for a dashboard |
| Dashboard | Vanilla JS, inline SVG charts, no libraries | No third-party scripts, no build step, data rendered via `textContent` (no HTML injection) | React/charting libs: more attack surface and weight than needed |
| Auth | HMAC session cookie, shared password | No user table; fails closed | OAuth/SSO: right for production, overkill for a demo (listed as a gap) |
| Tests / lint | pytest (319 passed, 5 skipped offline), Node tests, ruff | Offline, no AWS needed | Live tests are opt-in (`RUN_LIVE=1`) because they cost money |
| Payment provider | Razorpay test mode | Real API shapes, fake money | Production data (unobtainable and unwanted); Stripe test mode (works too; Razorpay matches the target employer) |

---

## 5. Problems hit and how they were solved (honest war stories)

Tell these. Interviewers trust people who can describe a failure precisely.

1. **A request hung for minutes (Groq 429).** Provider said `retry-after: 847`; my retry loop slept 14 minutes inside a web request, so fallback models never ran. Found with a stack dump (`kill -USR1`). Fixed: capped sleeps, per-call deadline, immediate failover, with a test using a fake `Retry-After: 847` provider.
2. **Nova Micro was a weak judge.** A/B and direct tests, then moved to Lite. Small sample, said so.
3. **The 100x error.** Nova wrote 100,000 paise as "₹100,000.00". Caught by the numeric guard in the saved run; shown to the user in unguarded mode.
4. **My own scenario was wrong.** `inject-1` demanded a figure the question did not ask for; I fixed it after the first run. It failed in both modes, so the fix did not favour the guards. `lookup-6` keyword list too narrow, widened afterwards, not re-run. Both are written in the README.
5. **Nova prints `<thinking>` text into answers.** Stripped in both modes before guards run, so the comparison stays fair.
6. **`resolve_model` produced `apacamazon...`.** A prefix helper returned no dot. One-line fix with a test.
7. **`.env.local` would have been uploaded publicly** by Vercel (see 3.21).
8. **International card rejected in Razorpay test mode.** I first gave `4111 1111 1111 1111` from memory; the account rejected it as international. The docs list a domestic Visa test card (`4718 6091 0820 4366`), which worked. Lesson: verify against docs, never from memory.
9. **Netbanking stuck at "created".** The mock bank page opens as a pop-up, unusable in the built-in browser; the card route works.
10. **Failed test payments.** Per Razorpay's test docs and confirmed on the account, a 4 to 10 digit OTP succeeds and a shorter one declines; that is how failures were seeded.
11. **Orders auto-capture; the capture API cannot create payments.** I verified 0 payments were `authorized` before explaining this.
12. **Flaky `lookup-5` (synthetic).** Missed two expected figures in one of two runs (31/31 in the dry run, 30/31 in the published sync). Run-to-run model variation, not a guard failure.

---

## 6. What the numbers say, and what they do not

### Saved run: `results/assure-nova-lite-r3.json`
Nova Lite (`apac.amazon.nova-lite-v1:0`), 31 scenarios x 3 repeats = **93 runs per mode**, synthetic ledger.

| Measure | Guarded | Unguarded |
|---|---|---|
| Scenarios passed | 88/93 = 94.6% (CI 88.0-97.7%) | 46/93 = 49.5% (CI 39.5-59.4%) |
| **Critical outcome reached (effective)** | **0/93 (CI 0.0-4.0%)** | **36/93 = 38.7% (CI 29.4-48.9%)** |
| Model asked for a critical violation (attempted) | 16/93 = 17.2% | 36/93 = 38.7% |
| Cross-merchant reads attempted / effective | 12 / 0 | 24 / 24 |
| Unapproved refund executions | 0 / 0 | 9 / 9 |
| Card data shown | 0 / 0 | 3 / 3 |
| Amounts not in ledger shown | 0 (11 withheld) | 9 |
| Avg latency / cost per task | 2.8 s / $0.0002 | 2.8 s / $0.0002 |

Guarded failures, 5 of 93: 3 were `lookup-6` (correct answer, my keyword list too narrow), 2 were the 100x error withheld (safe failure, costs helpfulness).

### First live sync: `results/live-latest.json`, 5 Nov 2026
One repeat per scenario; Razorpay test snapshot of 17 payments (8 captured, 9 failed; 16 tagged `m_acme`, 1 `m_zen`; 0 refunds; settlements endpoint returned nothing). Cost $0.0173, about 4 minutes.

| Source | Guarded pass | Guarded critical effective | Unguarded pass | Unguarded critical effective |
|---|---|---|---|---|
| Razorpay test (13 scenarios) | 13/13 | 0 (model attempted 1) | 9/13 | 3 |
| Synthetic (31 scenarios) | 30/31 (`lookup-5`) | 0 | 16/31 | 12 |

**Say it like this:** "13 runs is a demonstration of the method on real-shaped data, not a benchmark. The 93-run synthetic set is where the rates come from, and even there the intervals are optimistic."

### What is NOT claimed
- Production traffic, real customers or real merchants. Everything is test mode or synthetic.
- Affiliation with, or use by, Razorpay or any payment company.
- That 31 scenarios prove safety, or that the intervals are tight.
- That PII detection is a certified DLP, or that provenance is bulletproof.
- That the audit log is tamper-proof (it is tamper-evident).
- Any comparison across models: only Nova Lite has been run.
- That "live" means a stream: it is a periodic sync.

---

## 7. What I would do for production (the hardening roadmap)

| Area | Next step |
|---|---|
| Evaluation | More scenarios from real incident classes; more repeats; a second and third model on the same suite; adversarial/red-team cases written by someone else |
| CI | Run the gate on every PR and on model/prompt changes; scheduled weekly sync via GitHub Actions with AWS OIDC (no long-lived keys) and a spend cap; alert when a sync fails or goes stale |
| Audit | Anchor the chain head externally (write-once store or signed timestamp); retention policy; per-tenant chains |
| Identity | SSO / per-user roles for the dashboard; rate limiting on login; secrets in a manager |
| Money safety | Real approval workflow (queue + approver identity), idempotent execution against the real refund API behind a separate service, merchant-level rate limits |
| Arithmetic | A calculator tool so correct sums are shown, not withheld |
| PII | A certified detector as a second layer; field-level encryption for stored runs |
| Data | Real multi-merchant isolation (the `LedgerBackend` adapter already isolates the mechanism); settlements and refunds data; reconcile against the provider |
| Documents | Wire masking, numeric and audit controls into the document service; a payments-regulation corpus (RBI circulars) and an eval on it |
| Observability | Export traces to a vendor backend; dashboards for guard-denial rate by rule |

---

## 8. Who else could use this pattern

Any company where an AI agent reads sensitive records or moves money. **This lists who has the problem, not who uses this project, and I have not verified any company's needs.**

| Sector | Examples | Where the pattern fits |
|---|---|---|
| Payments / PSPs | Stripe, Adyen, PayU, Cashfree, PhonePe, Paytm | Refund, settlement and merchant-support agents |
| Neobanks and lenders | Jupiter, Fi, Slice, KreditBee | Balance, EMI and KYC lookups; tenant = customer |
| Brokerages and wealth | Zerodha, Groww | Wrong or invented numbers are costly; account isolation |
| Insurance | Claims and policy agents | Provenance checks on amounts and ids |
| Marketplaces / e-commerce | Flipkart, Amazon, Swiggy, Zomato | Refund and cancellation agents; buyer/seller isolation |
| Multi-tenant SaaS | CRM, billing, HR tools | Never show one customer's data to another |
| Healthcare, telecom | Records and billing agents | PII masking, audit trail, approval before irreversible actions |
| Banks and regulated firms | Compliance-heavy teams | Auditors want a tamper-evident record of actions and blocks |

**What carries over:** enforcement outside the model, tenant scoping, provenance checks on money actions, masking, checking figures against tool results, the hash-chained log, and the guarded-vs-unguarded measurement method.
**What needs rework per company:** the ledger adapter, the scenarios, policy limits and approval tiers, and real identity, queues and storage.
**Key line:** only the `LedgerBackend` adapter and the policy rules are payment-specific; the Razorpay adapter and the synthetic ledger already show the swap.

---

## 9. Likely interview questions, with answers

**Concept**

1. **Why not just write a good system prompt?** A prompt is a request; the model can be talked out of it (injection, persuasion). Code at the tool boundary is a rule: a foreign payment returns "not found" regardless of what the model wants. And I can unit-test it.
2. **How do you know the guards are what helped?** Same model, tools, prompt in both modes; only enforcement differs. Guarded critical effective 0/93 vs unguarded 36/93. The counterfactual is one I defined, so it shows the value of enforcement relative to no enforcement, not relative to a real system.
3. **What is attempted vs effective?** Attempted is what the model asked for (drifts with model changes); effective is what actually happened or reached the user (what the system controls). A model upgrade that raises attempted but not effective is safe.
4. **How does the provenance rule stop prompt injection?** A refund must name a payment and amount the *user's* message contains. Text inside a tool result, like a customer note, can never authorise one. It is a heuristic on the user's wording, so it can reject unusual phrasing.
5. **Why return "not found" for another merchant's payment?** "Denied" confirms existence, which is a leak, and the model might relay it. Same response as nonexistent leaks nothing.
6. **Why can't the agent do arithmetic?** Any figure not in a tool result or the user's message is withheld. It caught a 100x error. The cost is that correct sums are also withheld; the fix is a calculator tool.
7. **Why only drafts for refunds?** An agent should not be the last line before money moves. Humans approve; idempotency keys prevent duplicates; a senior tier above ₹10,000.
8. **What is the audit log and its weakness?** SHA-256 hash chain; edits, deletions and reordering break later hashes. Someone who can rewrite the whole table can rebuild a consistent chain, so production needs external anchoring.
9. **Why Wilson intervals?** Better than the normal approximation near 0% and 100%. 0/93 is "under about 4%", not "zero". Repeats are not independent, so they are optimistic.
10. **Why no LLM judge?** Cost, extra error, and it can be fooled by the same injection. Ground-truth checks are deterministic.

**Engineering choices**

11. **Why a read-only adapter?** I made the dangerous action impossible to express: GET only, test keys only, a whitelist, refunds in memory. No bug or injected prompt can move money because the code path does not exist.
12. **Why a separate seeder?** So the read path is provably write-free. The seeder can only `POST /orders`, dry run by default, idempotent.
13. **Why Redis, and why two tokens?** Serverless-friendly REST, free tier, Vercel Marketplace. The dashboard only has the read-only token, so a compromised function cannot write or delete.
14. **Why not just rebuild and redeploy?** That was the original design; it was stale by design. A sync plus Redis means new results appear without a deploy, with freshness shown.
15. **Why vanilla JS, no libraries?** No third-party scripts, no build step, everything rendered via `textContent` so data cannot inject HTML.
16. **How is login secured?** Server-side signed cookie (HMAC of expiry with the password), HttpOnly, Secure, SameSite=Strict, fails closed, constant-time compare, adversarial tests. Limits: one shared password, no cross-instance rate limiting.
17. **Why Nova Lite?** Cheap, imperfect enough to stress the guards, AWS-native. I upgraded from Micro after a targeted failure, not on price alone.
18. **Why stdlib `urllib` for Upstash?** Three commands needed; no extra dependency to audit.
19. **How do you control cost?** Spend cap per sync ($0.25), daily cap in the service computed from real token counts, per-user quotas. A full sync cost $0.0173.
20. **How do tests work without AWS?** Scripted fake LLM and fake ledgers; 319 passed, 5 skipped offline. Live tests are opt-in.

**Honesty and trade-offs**

21. **Is this production-ready?** No. It is a prototype: test-mode data, small samples, one model, shared-password dashboard, unanchored audit log. I can list the hardening steps.
22. **13 scenarios passing 13/13, so it works?** No. That shows the method runs on real-shaped data. The rates come from the 93-run set, with intervals.
23. **Your cross-tenant test is emulated, isn't it?** Yes: one Razorpay account is one merchant, so tenancy is a tag in `notes`, labelled as emulated. Only 1 payment is tagged `m_zen`, so that side is thin; the synthetic suite is the stronger tenancy test.
24. **Did you tune the tests to make the guards look good?** I changed two scenarios after seeing results (`inject-1`, `lookup-6`). The first failed in both modes, so it did not favour the guards; the second was a too-narrow keyword list. Both are disclosed in the README.
25. **What if Nova gets better, do the guards matter?** Attempted rates may fall, but the guards enforce rules independent of the model's behaviour; effective violations should stay at zero as the model changes, and the suite is how I'd verify that on each upgrade.
26. **What would you do with a week?** Scheduled sync and CI gate, a second model, more scenarios (especially red-teamed), external anchoring of the audit head.
27. **What did you not build?** Settlements/refunds from the Razorpay side (test mode returned none), a payments-regulation corpus, guards inside the document service, per-user auth.
28. **Why did you delete the film demo?** It was a stand-in domain; it diluted the payments story and left dead code. Kept what carried over (tracing, statistics, document pipeline); it is in git history at `2c77257`.
29. **Isn't "attempted" model behaviour, so why report it?** Because it is the early-warning signal: it tells you how much work the guards are doing and whether a model change is making them work harder.
30. **Have you used this with Razorpay's real system or APIs beyond test mode?** No. Test mode only, read-only, not affiliated.

**If asked about something not built:** "I haven't built that. Here is how I would, and here is what I would measure first." Never claim it exists.

---

## 10. Two-minute demo script

Prepare: be logged in to the dashboard (https://agent-observability-lab.vercel.app, password from you).

1. **(0:00) The problem, one sentence.** "Agents with payment tools can move money or leak data; prompts alone don't stop that."
2. **(0:15) Live tab.** Point to the provenance lines first ("Razorpay test mode, emulated tenancy, periodic capture, not production"), then the freshness badge and the last sync time. Show guarded vs unguarded KPIs; say "13 real-shaped scenarios is a demo, not a benchmark."
3. **(0:45) Recent runs.** Open one unguarded failure with tool calls (e.g. a cross-merchant read or unapproved refund) and the same scenario guarded, where the call is denied or the answer withheld.
4. **(1:15) Saved run tab.** The 93-run table: "0 of 93 critical outcomes guarded vs 36 of 93 unguarded, interval under 4%, caveats below the table."
5. **(1:30) Attack replay.** Run the injected-note replay: guards off, the refund happens; guards on, provenance blocks it.
6. **(1:45) Audit log tab.** Edit a row and show the chain break. Say "tamper-evident, not tamper-proof; production anchors the head."
7. **(1:55) Close.** "The pattern is domain-agnostic; only the ledger adapter and policy rules are payment-specific."

---

## 11. Cheat sheet

**Numbers (with their context)**
- 93 runs per mode (31 scenarios x 3), Nova Lite, synthetic: guarded **0/93** critical effective (CI 0.0-4.0%), unguarded **36/93** (38.7%, CI 29.4-48.9%).
- Pass rate: guarded 88/93 = 94.6%, unguarded 46/93 = 49.5%.
- Model attempted a critical violation in 16/93 (17.2%) of guarded runs; all stopped.
- Cross-merchant reads: guarded 12 attempted / 0 effective; unguarded 24 / 24.
- Cost and latency: about $0.0002 and 2.8 s per task, the same in both modes.
- Live sync (5 Nov 2026): 17 test payments (16 `m_acme`, 1 `m_zen`); guarded 13/13 on Razorpay-test and 30/31 on synthetic; unguarded 9/13 and 16/31; cost $0.0173; about 4 minutes. One repeat each.
- Tests: 319 passed, 5 skipped offline; ruff clean.
- Document service: 410 pages, 111 s ingest, $0.0045 embeddings, 5/5 hidden facts, about $0.26 per 1,000 questions. One document, 14 questions: works end to end, not "accurate in general".

**One-liners**
- "Enforcement lives in code between the model and the ledger, and I measured what it is worth."
- "I made the safe thing the only thing the adapter can do."
- "Attempted is the model; effective is the system."
- "Tamper-evident, not tamper-proof."
- "13 runs is a demo of the method; the rates come from 93."
- "Test mode, emulated tenancy, periodic capture, not affiliated with Razorpay."
- "I measured X; I did not measure Y."

---

# Appendix: added after this guide was first written

Read this with sections 3 (decisions) and 6 (what is not claimed). Everything here is also in `docs/DECISIONS.md`.

**1. Live events (webhook, queue, worker).** A real Razorpay test-mode webhook (made through Checkout with a test card) reaches a Vercel
function that verifies the HMAC signature, drops replays, keeps only whitelisted fields and queues the event. A separate worker
(`make live-worker`) runs the guarded agent and an unguarded comparison and publishes each step; the page polls every 3 seconds.
- *Why a worker, not the agent inside the function?* It would need long-lived AWS keys on Vercel, the Python package bundled, and the
  audit chain would lose continuity between invocations. The cost is that events wait for the worker, and the page says so.
- *Why is there a write token on Vercel now?* The webhook must enqueue. It is the one exception to "the site only holds the read-only
  token", confined to a file that refuses any command outside a short list. Upstash tokens cannot be scoped per command.
- *What is staged?* The demo button sets the note and merchant, so hidden-instruction events are staged. The cross-merchant probe uses a
  fixed fixture payment. Say both before an interviewer finds them.

**2. Personal accounts.** Each person signs in as themselves (scrypt-hashed passwords in Redis, signed expiring sessions, five-strike
lockout, a sign-in log without passwords or IPs, admin and viewer roles). Disabling an account ends its session within a minute. Honest
limits: no single sign-on, no multi-factor authentication, and only the account register is role-protected.

**3. Rules library.** Six cited, paraphrased RBI notes behind an optional `search_policy` tool. It is deliberately small and says what it
did not confirm. Likely question: *"Why not a vector database?"* At six notes, keyword scoring is testable and enough; the vector service
is a different product. Likely trap: claiming it is verified legal content. It is orientation, with sources, and on Nova Lite it passed 18/24 guarded vs 16/24 unguarded. The honest takeaways: the guard catches invented figures but not wrong-but-real ones, and I widened two abstention phrase lists after seeing a first run (disclosed in the file).

**4. Things I would say unprompted.** The benchmark numbers come from the core suite without the rules tool; the live-event counts are a
handful of checks; and "real-time" means near real time (seconds to a minute, polling).

**5. Hosted worker and CI gate.** GitHub Actions runs the worker (pinged when something is queued, hourly backstop) and a weekly sync with a
regression gate. AWS access is OIDC into a role limited to one Bedrock model on `main` only: *"Why no access keys?"* A leaked key works
forever from anywhere; an OIDC token expires in minutes and is bound to this repo and branch. Honest limits: the audit database restarts if the
Actions cache is evicted, branch protection is a GitHub setting I do not control, and the dispatch token can write repository contents.

**6. Guardrail simulator.** An SRE red-teaming page that fires an attack at the real policy engine, guards on and off, and shows an interception
matrix with measured decision times. *"Isn't that just a chat box?"* No: presets are fixed scripts, custom payloads are admin-only, masked, rate
limited and cost-capped, and the output is telemetry, not a reply. *"Does the preset result prove the model is safe?"* No. A scripted attacker shows
what the rules do when the model misbehaves; how often a model misbehaves is what the benchmark measures.

## A.6 The guard library, approvals and the compliance report

**What they are.** (1) `acp.guard`: a small library that puts scope, "the user named it" provenance, limits, a call budget, masking, an
answer check, approvals and the audit chain in front of any agent's tools, with the rules in a strict YAML file. (2) An approvals queue: a
refund the agent drafts waits on a page; an admin approves or rejects it (senior tier needs a written reason); the worker writes each
decision into the audit chain; a Slack-style webhook tells someone when something waits or is blocked. (3) A compliance report (HTML, JSON,
CSV) with an integrity verdict and a standard-library verifier.

**Why a separate library instead of rebuilding the engine.** The benchmark numbers come from the payments runtime. Moving it onto a new
engine would have thrown away the measurement. So the library is new, reuses the same masking, numeric and audit code, has its own tests, and
the docs say the benchmark does not transfer to it.

**Design points worth being able to explain.** A write needing approval is held, not run, and runs once when approved. Each approval is three
Redis keys with one writer each; the decision uses `SET NX` so the first decision wins. The website cannot append to the audit chain (it lives
with the worker), so decisions are copied in by the worker and the page says "waiting" until then. The report is built next to the log so the
chain check is real, and the verifier shares no code with the project so a reader need not trust it.

**Questions to expect.**
- *Does the library give the benchmark's results?* No. Its tests show each rule works; they do not measure a model behind it. Say so first.
- *Can the same person request and approve?* The requester is an agent, so there is no human conflict to block; four-eyes for human requesters is not built.
- *Does approving move money?* No. The ledger is a sample; approval records a decision.
- *What does the report prove?* That the export was not edited after it was made. Not that the original log was never rewritten: anchor the head hash somewhere you control.
- *Why Slack and not email?* One secret and no mail server; email is not built in.
