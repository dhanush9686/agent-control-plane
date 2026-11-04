"""Production wiring: build the real service (Groq models, Pinecone vectors, MiniLM embeddings).

Run:  uvicorn acp.server.main:create_production_app --factory --port 8000      (or: make serve-api)
"""

from __future__ import annotations

import atexit
import logging
from dataclasses import replace
from typing import Any

from dotenv import load_dotenv

from acp import store
from acp.config import ROOT, Settings, get_settings
from acp.embeddings import MiniLMEmbedder
from acp.ingest.pipeline import IngestDeps
from acp.ingest.worker import JobRunner
from acp.kb import KnowledgeBase
from acp.llm import LLM, GroqLLM
from acp.ratelimit import BudgetedLLM, DailyQuota, ModelBudget, SlidingWindow
from acp.server.app import AppDeps, create_app
from acp.tracing import Tracing
from acp.vectorstore import PineconeStore

log = logging.getLogger("acp.server")


def build_deps(settings: Settings | None = None) -> AppDeps:
    load_dotenv(ROOT / ".env")  # keys stay in the environment; nothing here prints them
    s = settings or get_settings()
    embedder = MiniLMEmbedder.shared(s.embedding_model)
    vstore = PineconeStore.connect(embedder.dim)
    kb = KnowledgeBase(s.kb_root, embedder, vstore)
    runner = JobRunner(IngestDeps(s, kb))

    def conn_factory():  # noqa: ANN202
        return store.connect(s.db_path)

    budget = ModelBudget(conn_factory, s.model_tpm, s.model_daily_tokens)
    llms: dict[str, LLM] = {}

    def llm_for_model(model: str) -> LLM:
        if model not in llms:  # one client per model, reused across requests
            llms[model] = GroqLLM(replace(s, model=model))
        return llms[model]

    chains: dict[str, tuple[str, ...]] = {
        "judge": s.judge_models,
        "refine_query": s.judge_models,
        "synthesize": s.synth_models,
    }
    return AppDeps(
        settings=s,
        kb=kb,
        runner=runner,
        llm_for_user=lambda: BudgetedLLM(chains, s.synth_models, llm_for_model, budget),
        ask_limiter=SlidingWindow(s.rate_per_min),
        auth_fail_limiter=SlidingWindow(10),
        quota=DailyQuota(conn_factory),
        tracing=Tracing(s),
    )


def _enable_stack_dump() -> None:
    """`kill -USR1 <pid>` prints every thread's stack to stderr: the quickest way to see where a stuck request waits."""
    import faulthandler
    import signal

    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)


def create_production_app() -> Any:
    _enable_stack_dump()
    deps = build_deps()
    deps.runner.start()  # also re-queues any ingestion interrupted by the previous shutdown
    atexit.register(deps.runner.stop)
    log.info("service ready: kb_root=%s db=%s", deps.settings.kb_root, deps.settings.db_path)
    return create_app(deps)
