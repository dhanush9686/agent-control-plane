"""All tunable settings in one place.

Analogy: the thermostat panel. Nodes never hard-code a number; they read it
from here, so a reviewer can see every knob the system has on one screen.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# $ per 1 million tokens: (input, output). These are ESTIMATES for cost
# tracking, not a billing record -- check https://groq.com/pricing and override
# with the ACP_PRICES_JSON env var (path to a JSON file of the same shape).
# Groq's free tier means real spend is $0; the cost column is "what this would cost at paid rates".
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "qwen/qwen3.8-27b": (0.29, 0.59),
}


def load_prices() -> dict[str, tuple[float, float]]:
    """Default price table, optionally overridden by a JSON file."""
    prices = dict(DEFAULT_PRICES)
    override = os.environ.get("ACP_PRICES_JSON")
    if override and Path(override).is_file():
        raw = json.loads(Path(override).read_text())
        prices.update({k: (float(v[0]), float(v[1])) for k, v in raw.items()})
    return prices


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0.0
    # Grader for evals: a DIFFERENT model family than the answerer to reduce self-preference bias.
    # Override with ACP_JUDGE_MODEL, or pass --judge-model same to grade with the answering model.
    judge_model: str = field(default_factory=lambda: os.environ.get("ACP_JUDGE_MODEL", "qwen/qwen3.8-27b"))
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    top_k: int = 3  # chunks fetched per search
    max_attempts: int = 2  # retrieval attempts before we stop looping
    llm_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("ACP_LLM_TIMEOUT_S", "25"))
    )  # hung calls retry sooner
    llm_max_retries: int = field(
        default_factory=lambda: int(os.environ.get("ACP_LLM_MAX_RETRIES", "4"))
    )  # extra tries on 429/5xx/timeouts, with backoff
    trace_mode: str = field(default_factory=lambda: os.environ.get("ACP_TRACE_MODE", "sync"))  # sync | batch | kafka
    kafka_bootstrap: str = field(default_factory=lambda: os.environ.get("ACP_KAFKA_BOOTSTRAP", "localhost:19092"))
    kafka_topic: str = field(default_factory=lambda: os.environ.get("ACP_KAFKA_TOPIC", "acp.spans"))
    # Point these at another knowledge base (e.g. the SQuAD 2.0 slice) with ACP_CORPUS_PATH / ACP_INDEX_PATH.
    corpus_path: Path = field(
        default_factory=lambda: Path(os.environ.get("ACP_CORPUS_PATH", ROOT / "data" / "corpus.txt"))
    )
    index_path: Path = field(
        default_factory=lambda: Path(os.environ.get("ACP_INDEX_PATH", ROOT / "data" / "faiss_index"))
    )
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("ACP_DB_PATH", ROOT / "data" / "traces.db")))
    prices: dict[str, tuple[float, float]] = field(default_factory=load_prices)

    # --- uploaded-document mode (server). Every number is an env var so limits can change without code. ---
    domain_name: str = field(default_factory=lambda: os.environ.get("ACP_DOMAIN_NAME", "a small film-knowledge corpus"))
    citation_example: str = field(
        default_factory=lambda: os.environ.get("ACP_CITATION_EXAMPLE", "[KGF: Chapter 1 (2018)]")
    )  # shown to the model as the citation style; the server uses "[report.pdf p.37]"
    kb_root: Path = field(default_factory=lambda: Path(os.environ.get("ACP_KB_ROOT", ROOT / "data" / "kb")))
    max_upload_mb: int = field(default_factory=lambda: _env_int("ACP_MAX_UPLOAD_MB", 30))
    max_pages: int = field(default_factory=lambda: _env_int("ACP_MAX_PAGES", 600))
    max_chunks: int = field(default_factory=lambda: _env_int("ACP_MAX_CHUNKS", 3000))
    chunk_chars: int = field(default_factory=lambda: _env_int("ACP_CHUNK_CHARS", 900))
    chunk_overlap: int = field(default_factory=lambda: _env_int("ACP_CHUNK_OVERLAP", 150))
    parse_timeout_s: int = field(default_factory=lambda: _env_int("ACP_PARSE_TIMEOUT_S", 180))
    rate_per_min: int = field(default_factory=lambda: _env_int("ACP_RATE_PER_MIN", 5))  # asks per user per minute
    daily_asks: int = field(default_factory=lambda: _env_int("ACP_DAILY_ASKS", 25))  # per user per UTC day
    daily_uploads: int = field(default_factory=lambda: _env_int("ACP_DAILY_UPLOADS", 3))
    model_tpm: int = field(default_factory=lambda: _env_int("ACP_MODEL_TPM", 6500))  # headroom under Groq's 8,000
    model_daily_tokens: int = field(default_factory=lambda: _env_int("ACP_MODEL_DAILY_TOKENS", 180_000))
    context_token_budget: int = field(default_factory=lambda: _env_int("ACP_CONTEXT_TOKENS", 3500))
    # Which models answer which step, in fallback order (comma separated env vars). Cheap/fast model judges,
    # the strongest synthesizes; when one is out of free-tier budget the next is used.
    judge_models: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            m.strip() for m in os.environ.get("ACP_JUDGE_MODELS", "openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",")
        )
    )
    synth_models: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            m.strip() for m in os.environ.get("ACP_SYNTH_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",")
        )
    )
    rerank: bool = field(default_factory=lambda: _env_bool("ACP_RERANK", False))
    store_chunk_text: bool = field(default_factory=lambda: _env_bool("ACP_STORE_CHUNK_TEXT", False))  # privacy default

    def cost_usd(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        """Estimated dollars for one LLM call, from the price table (for `model`, default the main model)."""
        p_in, p_out = self.prices.get(model or self.model, (0.0, 0.0))
        return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


def get_settings() -> Settings:
    """Load .env (never printed) and return the settings object."""
    load_dotenv(ROOT / ".env")
    return Settings()
