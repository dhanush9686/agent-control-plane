# Interview notes (plain language, strictly what exists)

## One-sentence pitch
"I built an assurance layer for AI agents that touch money: a policy engine, card masking, a numeric-fidelity check and a tamper-evident audit log between the agent and a synthetic ledger, plus a scenario suite that runs with the guards on and off. On Nova Lite the unguarded agent reached a critical outcome in 39% of runs; with the layer, 0 of 93. Alongside it is a traced, quota-limited document service for long PDFs such as regulations."

## Component walkthrough

| Component | Plain explanation | Analogy |
|---|---|---|
| `acp/assure/sandbox.py` | Synthetic merchants, payments, refunds and settlements; money is integer paise | A flight simulator |
| `acp/assure/policy.py`, `tools.py` | Tenant scope, refund provenance and limits, drafts that need human approval | A teller's checklist the model cannot talk past |
| `acp/assure/pii.py`, `numeric.py` | Luhn/Verhoeff-checked masking; every rupee figure must come from a tool result | A cashier who may only read figures off the receipt |
| `acp/assure/audit.py` | Hash-chained log of every run and tool call; edits, deletions and reordering are detected | A numbered ledger book where each page fingerprints the one before |
| `acp/assure/evaluate.py`, `scoring.py` | 31 scenarios scored from ledger ground truth, guards on vs off, with a gate | A crash-test lab |
| `acp/graph.py`, `nodes.py` | Document agent: retrieve, judge, refine (max 2), synthesize with page citations | Research assistant with footnotes |
| `acp/kb.py`, `hybrid.py`, `vectorstore.py` | Per-document index; vector + BM25 fusion; Pinecone | A filing cabinet, one locked drawer per document |
| `acp/ingest/` | Sandboxed PDF parse, page-aware chunking, one ingestion worker | A mail room |
| `acp/llm.py` | The only place that talks to Bedrock; capped retries; tool use | Company switchboard |
| `acp/tracing.py`, `store.py` | OpenTelemetry spans around nodes and LLM calls, exported to SQLite | Flight recorder |
| `dashboard/` | Login-protected results, attack replay and audit demo | A binder behind a locked door |

## Key tradeoffs I can defend
- Enforcement in ordinary code, not in the prompt; measured with the guards on and off.
- Refunds are drafts a human approves; the agent never moves money.
- The model may not do arithmetic: totals must come from a tool, at the cost of withholding some correct-looking sums.
- Graph vs loop for the document agent: testable routing, explicit retry cap.
- SQLite: simple and sufficient here; not a production observability backend.
- Cost is an estimate from a price table; the daily spend cap is computed from real token counts.

## Likely questions
1. **Why is the unguarded baseline fair?** Same tools, prompt and model; only enforcement differs. It is a deliberate counterfactual (its refund tool executes at once), not a claim about any real system.
2. **What is "attempted" vs "effective"?** Attempted is what the model asked for (it drifts with model or prompt changes); effective is what happened or reached the user (what the system around the model controls).
3. **How does the provenance rule stop prompt injection?** A refund must name a payment and an amount that the user's own message contains; text inside a tool result, like a customer note, cannot authorise one. It is a heuristic on the user's wording.
4. **Why is the audit log only "tamper-evident"?** Someone who can rewrite the whole table can rebuild a consistent chain; the production step is anchoring the head hash where the database admin cannot write.
5. **How does tracing avoid breaking the agent?** Every tracing call is wrapped, and a test makes the exporter fail and checks the answer still returns.
6. **How do you handle rate limits?** One shared retry helper: capped sleeps, a per-call deadline, immediate failover when a provider asks for a long wait.
7. **What did the live run find?** A real 100x unit error (100,000 paise written as ₹100,000.00) that the numeric guard withheld, and the model attempting injected or cross-merchant requests in 17% of guarded runs, all stopped.
8. **What is not built?** A payments-regulation corpus through the document service, the PII/numeric/audit controls inside that service, a second model on the suite, and a larger scenario set.

## The document service: what to say, and what not to claim
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

## Why the film demo is gone
It was a stand-in domain used to build the tracing and eval machinery. I removed it, with Kafka, Neo4j, Kubernetes and the grounding-judge harness built around it, so the project serves payments use cases only; it remains in git history (`2c77257`). If asked: "I kept what carried over (tracing, statistics, the document pipeline) and deleted the rest rather than leave dead code."
