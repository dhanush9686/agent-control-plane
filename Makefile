PY := .venv/bin/python
Q ?= Who kills Garuda, and how does Rocky's story end?

.PHONY: setup index run demo eval gate export test test-live lint fmt serve

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
