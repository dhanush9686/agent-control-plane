# Agent Control Plane

A thin reliability layer around an LLM agent. Every run is **traced** (steps, latency, tokens, cost) and every answer is **scored for grounding** (is each claim supported by the retrieved sources?), so a team can see *when* and *why* the agent fails.

- **Live dashboard:** _LIVE_URL_
- **Repo:** _REPO_URL_

![Dashboard overview](docs/dashboard-overview.png)
![Trace waterfall](docs/dashboard-runs.png)

## The problem

LLM agents fail quietly: they sound confident while inventing a detail the sources never said. This project wraps a small retrieval agent (a film knowledge base) with (1) a flight recorder for every step and (2) an automatic fact-checker, then gates releases on the scores.

The knowledge base (`data/corpus.txt`) contains deliberate traps: a **thin** entry (Dragon, unreleased), a note that **KGF and Salaar are *not* one universe**, and questions it simply cannot answer.

## Architecture

```mermaid
flowchart LR
    Q[Question] --> R[retrieve<br/>FAISS, k=3, dedup]
    R --> J{judge<br/>LLM, structured verdict}
    J -- sufficient --> S[synthesize<br/>answer only from context + cite]
    J -- insufficient, attempts &lt; 2 --> F[refine_query]
    F --> R
    J -- insufficient, cap reached --> S
    S --> A[Answer]
    subgraph Tracing
      T[OpenTelemetry spans<br/>per node + per LLM call] --> DB[(SQLite<br/>runs, spans)]
    end
    R -.-> T
    J -.-> T
    S -.-> T
    DB --> E[Eval runner<br/>19 golden questions]
    E --> G[Grounding judge + deterministic checks]
    G --> REP[results/eval_report.json<br/>+ SQLite eval tables]
    REP --> GATE[Regression gate<br/>exit 1 below thresholds]
    DB --> X[export script] --> D[Static dashboard<br/>dashboard/]
```

Code map: `acp/graph.py` (LangGraph StateGraph and routing), `acp/nodes.py` (the four nodes), `acp/llm.py` (Groq client with retry/backoff), `acp/tracing.py` + `acp/store.py` (spans to SQLite), `acp/evals/` (golden set, checks, grounding judge, report), `scripts/` (gate, export), `dashboard/` (static site).

## Quickstart (about 5 minutes)

Requires Python 3.11+ and a free [Groq API key](https://console.groq.com/keys).

```bash
git clone _REPO_URL_ && cd agent-control-plane
make setup            # venv, deps, builds the FAISS index; creates .env from .env.example
# put your key in .env  (GROQ_API_KEY=...)
make run Q="Who kills Garuda, and how does Rocky's story end?"
```

`make setup` downloads the MiniLM embedding model (~80 MB) on first use. Other commands:

| Command | What it does |
|---|---|
| `make run Q="..."` | Ask one question; prints answer, citations and the trace summary |
| `make demo` | Four demo questions |
| `make eval` | Run the 19-question golden set (several minutes on the free tier) |
| `make gate` | Exit 1 if `results/eval_report.json` is below `evals/thresholds.json` |
| `make export` | Write `dashboard/data/traces.json` from the latest evals |
| `make serve` | Preview the dashboard at http://localhost:8000 |
| `make test` / `make test-live` | Offline unit tests / real Groq+FAISS end-to-end tests |
| `make lint` | ruff check + format check |

Equivalent without make: `python -m acp.cli "question"`, `python -m acp.evals`.

## How tracing works

Each question gets one `run_id`. OpenTelemetry spans wrap **every graph node** (`node.retrieve`, `node.judge`, `node.refine_query`, `node.synthesize`) and **every LLM call** (`llm.judge`, `llm.synthesize`, ...). A custom `SpanExporter` (`SQLiteSpanExporter`) writes them to `data/traces.db`:

- `runs`: question, answer, latency, tokens, estimated cost, LLM calls, retries, retrieved chunks.
- `spans`: name, kind, parent, offset/latency, tokens, cost, retries, and attributes (the judge's verdict and reasoning, the search query, which sources were retrieved).
- Tokens come from the provider's response metadata. **Cost is an estimate** from a configurable price table (`acp/config.py`, override with `ACP_PRICES_JSON`); check Groq's pricing page.
- Tracing never crashes the agent: every tracing call is wrapped, and a unit test forces the exporter to fail and checks the answer still returns.

## How evals work

For each of 19 golden questions (`evals/golden.yaml`: answerable, multi-hop, thin-data, contradiction trap, unanswerable, prompt injection):

1. The traced agent answers.
2. **Deterministic checks** (no LLM): did it answer vs refuse as expected, required/banned phrases, a citation present, and cited sources actually among the retrieved chunks.
3. **Grounding judge** (LLM, structured output): splits the answer into atomic claims and checks each against the retrieved chunks only, quoting evidence. Any unsupported claim counts as a hallucination.

Metric definitions: *answered-correctly* = of questions that should be answered, share answered with the right content; *hallucination rate* = share of non-refused answers with at least one unsupported claim; *refusal correctness* = share of all questions where answer-vs-refuse matched; *pass rate* = share with no failure of any kind.

## Results (real run, `openai/gpt-oss-120b` on Groq free tier)

Eval `4276d06599`, 19 questions, 0 errors, full data in `results/eval_report.json`:

| Metric | Value |
|---|---|
| Answered-correctly rate | 100.0% |
| Hallucination rate | 0.0% |
| Refusal correctness | 100.0% |
| Citation rate | 100.0% |
| Overall pass rate | 100.0% |
| Avg latency | 11.1 s (see note) |
| Avg cost / question (est.) | $0.00055 (about 1,430 input + 450 output tokens, 2.7 LLM calls) |

| Category | n | Pass rate | Hallucinations |
|---|---|---|---|
| answerable | 4 | 100% | 0 |
| multi_hop | 3 | 100% | 0 |
| thin_data | 3 | 100% | 0 |
| contradiction_trap | 3 | 100% | 0 |
| unanswerable | 3 | 100% | 0 |
| prompt_injection | 3 | 100% | 0 |

**Read these numbers carefully.**
- This is **one run of 19 questions**. It is a small sample, and the agent is not deterministic. An **earlier full run** (kept in the dashboard under "Earlier") scored 89.5%: the judge caught a real embellishment on the thin Dragon entry (the agent added "Luger's role... drives the narrative", which the corpus never says), and it also falsely flagged the sentence "That is not in the knowledge base." as an unsupported claim. The second was a bug in my evaluator, fixed by filtering refusal sentences deterministically (`acp/evals/grounding.py`). The first is the system working as intended.
- **Latency is inflated by free-tier rate limiting.** 44 retries with backoff happened across the 19 questions; individual runs ranged 1.4 s to 20 s. Do not read 11 s as the model's speed.
- The grounding judge uses the same model that wrote the answers (see limitations).

## Limitations

- Tiny corpus (13 chunks) and a small golden set (19): good for demonstrating the method, not a statistically strong benchmark.
- **LLM-as-judge is imperfect.** It can share blind spots with the answering model (same model here by default; `--judge-model` lets you pick another), it can be lenient toward fluent text, and its verdicts vary run to run. Deterministic checks reduce but do not remove this. Claim splitting and checking happen in one call.
- Keyword checks for expected content are coarse; the grounding judge, not the keywords, is what catches wrong facts.
- Cost is estimated from a price table, not read from a bill. Latency includes rate-limit waits.
- Tracing is synchronous and writes to local SQLite: fine for this scale, not for high-volume production.
- CI runs lint + offline tests only. Evals call a paid/free-tier API, so they run on demand (`make eval`), not on every push; the regression gate checks the committed report.

## Roadmap (NOT built)

These are ideas only; none exist in this repo:
- A second agent and agent-vs-agent comparison on the same golden set.
- A lineage graph (e.g. Neo4j) linking claims to chunks to source documents.
- Streaming traces through Kafka; Kubernetes deployment.
- Scheduled evals in CI with a stored secret, and trend charts across many eval runs.
- Larger corpora, human-labelled grounding data to measure the judge's own accuracy.

## Design decisions

Short version here; full reasoning in [docs/DECISIONS.md](docs/DECISIONS.md). A plain-language walkthrough for interviews is in [docs/INTERVIEW_NOTES.md](docs/INTERVIEW_NOTES.md).

- **LangGraph conditional edges** instead of a while-loop: the routing rule is one pure function that is unit tested, and the retry cap is explicit.
- **LLM-as-judge** because "is this claim supported" needs language understanding; mitigated with a strict prompt, evidence quotes and deterministic checks.
- **SQLite** because it is one file, zero setup, and enough for thousands of runs.
