PY := .venv/bin/python

.PHONY: setup test lint fmt serve serve-api create-user longdoc assure assure-gate audit-verify audit-report guard-demo dashboard-build live-probe live-seed live-seed-create live-sync-dry live-sync live-worker live-worker-once dashboard-user

setup:  ## create venv and install dependencies
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt
	@test -f .env || (cp .env.example .env && echo "Created .env: add PINECONE_API_KEY and set AWS_REGION (AWS credentials via aws configure)")

test:  ## offline unit tests (fake models, no network, no AWS)
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

serve:  ## preview the dashboard locally at http://localhost:5173 (no login locally; data comes from dashboard/data/)
	cd dashboard && npm install --no-audit --no-fund && npm run dev

serve-api:  ## the upload-and-chat document service (Bedrock + Pinecone), http://localhost:8000
	$(PY) -m uvicorn acp.server.main:create_production_app --factory --host 127.0.0.1 --port 8000

create-user:  ## mint an API key: make create-user NAME=alice
	$(PY) -m acp.server.admin create-user --name "$(NAME)"

longdoc:  ## retrieval-only long-document eval on real Pinecone (no LLM tokens)
	$(PY) -m acp.evals.longdoc --retrieval-only --pages 450 --rerank

assure:  ## run the payments-agent assurance suite on Bedrock, guards on vs off (a few cents)
	$(PY) -m acp.assure --repeats 3 --label latest

assure-gate:  ## same, and exit 1 if evals/assure/thresholds.json is violated
	$(PY) -m acp.assure --repeats 3 --label latest --gate

audit-verify:  ## check the audit hash chain of the traces database
	$(PY) -m acp.assure.audit verify

audit-report:  ## export the worker's audit log as a report (HTML); open it and print to PDF
	$(PY) -m acp.assure.report --db results/ingest-audit.db --format html --out results/audit-report.html

guard-demo:  ## run the guard library in front of sample tools, no model needed
	$(PY) examples/guard_quickstart.py

dashboard-build:  ## rebuild the dashboard's assurance data and the login-protected data bundle
	$(PY) scripts/build_dashboard.py

live-probe:  ## read-only check of the Razorpay TEST account (counts and field names only)
	$(PY) scripts/razorpay_probe.py

live-seed:  ## dry run: show the test orders that would be created in the Razorpay TEST account
	$(PY) scripts/razorpay_seed.py

live-seed-create:  ## create those test orders (a write to your TEST account; then pay them via the checkout page)
	$(PY) scripts/razorpay_seed.py --create

live-sync-dry:  ## full live sync without publishing (Bedrock cost: cents); writes results/live-latest.json
	$(PY) -m acp.assure.live --dry-run

live-sync:  ## full live sync and publish to Upstash Redis, then read it back to verify
	$(PY) -m acp.assure.live

live-worker:  ## run the event worker: takes Razorpay webhook events off the queue, runs the agent, publishes each step (Ctrl-C to stop)
	$(PY) -m acp.assure.ingest

live-worker-once:  ## process whatever is queued right now, then exit
	$(PY) -m acp.assure.ingest --once

dashboard-user:  ## manage dashboard accounts, e.g. make dashboard-user ARGS="add you@example.com --role admin"
	$(PY) scripts/dashboard_user.py $(ARGS)
