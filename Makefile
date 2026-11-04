PY := .venv/bin/python
Q ?= Who kills Garuda, and how does Rocky's story end?

.PHONY: setup index run demo eval eval-naive judge-check compare gate export test test-live lint fmt serve up kafka-consumer neo4j k8s-up benchmarks squad-index eval-squad judge-check-human serve-api create-user longdoc

setup:  ## create venv, install deps, build the search index
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt
	@test -f .env || (cp .env.example .env && echo "Created .env: paste your GROQ_API_KEY into it")
	$(MAKE) index

index:  ## (re)build data/faiss_index from data/corpus.txt
	$(PY) build_index.py

run:  ## ask one question: make run Q="your question"
	$(PY) -m acp.cli "$(Q)"

demo:  ## run four demo questions
	$(PY) -m acp.cli --demo

eval:  ## run the golden set (19 questions; slow on the free tier)
	$(PY) -m acp.evals --pause 1

eval-naive:  ## same golden set with the one-shot RAG baseline (agent-vs-agent comparison)
	$(PY) -m acp.evals --agent naive --pause 1 --label naive

judge-check:  ## measure the grader itself on labelled answers (needs both judge models' quota)
	$(PY) -m acp.evals.judge_check --judge-model qwen/qwen3.8-27b openai/gpt-oss-120b

compare:  ## compare two eval runs: make compare A=<eval_id> B=<eval_id>
	$(PY) -m acp.evals.compare $(A) $(B)

gate:  ## fail (exit 1) if eval scores are below evals/thresholds.json
	$(PY) scripts/check_regression.py

export:  ## write dashboard/data/traces.json from the latest eval
	$(PY) scripts/export_traces.py

test:  ## offline unit tests (fake LLM, no network)
	$(PY) -m pytest -q

test-live:  ## real Groq + real FAISS end-to-end tests
	RUN_LIVE=1 $(PY) -m pytest -q -m live

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

serve:  ## preview the dashboard at http://localhost:8000
	$(PY) -m http.server 8000 -d dashboard

up:  ## free local services: Kafka (19092) + Neo4j (7474/7687) via Docker
	docker compose up -d kafka neo4j

kafka-consumer:  ## drain the span topic into SQLite (run the agent with ACP_TRACE_MODE=kafka)
	$(PY) scripts/kafka_consumer.py

neo4j:  ## load the lineage graph into the local Neo4j
	$(PY) scripts/load_neo4j.py

k8s-up:  ## deploy the dashboard to the current kube context (e.g. minikube)
	docker build -f Dockerfile.dashboard -t acp-dashboard:0.1 .
	kubectl apply -f k8s/dashboard.yaml

benchmarks:  ## download small public dataset slices (SQuAD 2.0, RAGTruth, FEVER) into data/benchmarks (gitignored)
	$(PY) scripts/fetch_benchmarks.py

squad-index:  ## build the search index for the SQuAD 2.0 knowledge base
	ACP_CORPUS_PATH=data/benchmarks/squad2/corpus.txt ACP_INDEX_PATH=data/faiss_squad2 $(PY) build_index.py

eval-squad:  ## 60 human-written SQuAD 2.0 questions (30 answerable, 30 unanswerable)
	ACP_CORPUS_PATH=data/benchmarks/squad2/corpus.txt ACP_INDEX_PATH=data/faiss_squad2 \
	$(PY) -m acp.evals --golden data/benchmarks/squad2/golden.yaml --workers 3 --label squad2

judge-check-human:  ## grade the grader on human-annotated RAGTruth + FEVER items
	$(PY) -m acp.evals.judge_check --judge-model qwen/qwen3.8-27b \
	  --labels data/benchmarks/judge_ragtruth.jsonl data/benchmarks/judge_fever.jsonl

serve-api:  ## the upload-and-chat service (Groq + Pinecone + MiniLM), http://localhost:8000
	$(PY) -m uvicorn acp.server.main:create_production_app --factory --host 127.0.0.1 --port 8000

create-user:  ## mint an API key: make create-user NAME=alice
	$(PY) -m acp.server.admin create-user --name "$(NAME)"

longdoc:  ## retrieval-only long-document eval on real Pinecone (no LLM tokens)
	$(PY) -m acp.evals.longdoc --retrieval-only --pages 450 --rerank
