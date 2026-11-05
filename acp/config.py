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

# $ per 1 million tokens: (input, output). These are ESTIMATES for cost tracking and for the daily spend guard, not a
# billing record -- AWS prices change and differ by region: check https://aws.amazon.com/bedrock/pricing/ and override
# with the ACP_PRICES_JSON env var (path to a JSON file {"model-id": [input, output]}). Keys are matched after removing
# the regional prefix ("apac.", "us.", "eu.", "global."), so one entry covers every region's inference profile.
# A model missing from this table is costed at $0 AND logged, so an unknown price can never silently look free.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "amazon.nova-micro-v1:0": (0.035, 0.14),
    "amazon.nova-lite-v1:0": (0.06, 0.24),
    "amazon.nova-pro-v1:0": (0.80, 3.20),
    "anthropic.claude-haiku-4-5-20251001-v1:0": (1.00, 5.00),
    "anthropic.claude-sonnet-4-5-20250929-v1:0": (3.00, 15.00),
    "amazon.titan-embed-text-v2:0": (0.02, 0.0),  # embeddings bill input tokens only
    # Groq (free tier; kept so older eval runs still price correctly)
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "qwen/qwen3.8-27b": (0.29, 0.59),
}
_REGION_PREFIXES = ("apac.", "us.", "eu.", "us-gov.", "global.", "jp.", "au.", "ca.", "in.")


def price_key(model: str) -> str:
    """'apac.amazon.nova-micro-v1:0' -> 'amazon.nova-micro-v1:0' (inference-profile prefix removed)."""
    for pre in _REGION_PREFIXES:
        if model.startswith(pre):
            return model[len(pre) :]
    return model


def bedrock_profile_prefix(region: str | None) -> str:
    """Which inference-profile prefix serves a region: ap-south-1 -> 'apac', us-east-1 -> 'us', eu-west-1 -> 'eu'."""
    r = (region or "us-east-1").lower()
    if r.startswith("ap-"):
        return "apac"
    if r.startswith("eu-"):
        return "eu"
    return "us"


def _aws_region() -> str | None:
    env = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env:
        return env
    try:
        import boto3

        return boto3.Session().region_name
    except Exception:  # noqa: BLE001 - boto3 missing or misconfigured: leave unset, the client will explain
        return None


def _bedrock_models() -> tuple[str, str, str]:
    """(micro, lite, pro): Amazon Nova text models served by this region's inference-profile prefix."""
    pre = bedrock_profile_prefix(_aws_region())
    return f"{pre}.amazon.nova-micro-v1:0", f"{pre}.amazon.nova-lite-v1:0", f"{pre}.amazon.nova-pro-v1:0"


# Which Nova model does what (measured, see docs/DECISIONS.md #21): the cheapest model (Micro) missed a fact among 10
# chunks and refused an injected question; Nova Lite fixed both for ~60% more money (about $0.26 per 1,000 questions).
# So Lite answers and judges, Micro is the fallback, and Pro (stronger than the answerer) grades evals.


def _provider() -> str:
    return os.environ.get("ACP_LLM_PROVIDER", "bedrock").strip().lower()


def _default_model() -> str:
    return os.environ.get("ACP_MODEL") or (_bedrock_models()[1] if _provider() == "bedrock" else "openai/gpt-oss-120b")


def _default_judge() -> str:
    # The eval grader should be stronger than (and different from) the model it grades: Nova Pro grades Nova Lite.
    return os.environ.get("ACP_JUDGE_MODEL") or (
        _bedrock_models()[2] if _provider() == "bedrock" else "qwen/qwen3.8-27b"
    )


def _chain(env: str, bedrock: tuple[str, ...], groq: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(env)
    if raw:
        return tuple(m.strip() for m in raw.split(",") if m.strip())
    return bedrock if _provider() == "bedrock" else groq


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
    llm_provider: str = field(default_factory=_provider)  # "bedrock" (default) or "groq"
    embedding_provider: str = field(default_factory=lambda: os.environ.get("ACP_EMBEDDING_PROVIDER", "bedrock").lower())
    aws_region: str | None = field(default_factory=_aws_region)
    model: str = field(default_factory=_default_model)
    temperature: float = 0.0
    # Grader for evals: a DIFFERENT model family than the answerer to reduce self-preference bias.
    # Override with ACP_JUDGE_MODEL, or pass --judge-model same to grade with the answering model.
    judge_model: str = field(default_factory=_default_judge)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"  # only for the legacy local film-corpus index
    bedrock_embed_model: str = field(
        default_factory=lambda: os.environ.get("ACP_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    )
    embed_dimensions: int = field(
        default_factory=lambda: _env_int("ACP_EMBED_DIMENSIONS", 1024)
    )  # Titan v2: 256|512|1024
    max_output_tokens: int = field(default_factory=lambda: _env_int("ACP_MAX_OUTPUT_TOKENS", 1024))
    llm_deadline_s: float = field(
        default_factory=lambda: _env_float("ACP_LLM_DEADLINE_S", 60.0)
    )  # per call, all retries
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
    # Shared guard rails, per model: tokens per minute and per day. Bedrock bills per token, so these (plus the USD cap
    # below) stop a runaway client from spending unbounded money. Defaults are generous but finite.
    model_tpm: int = field(default_factory=lambda: _env_int("ACP_MODEL_TPM", 150_000))
    model_daily_tokens: int = field(default_factory=lambda: _env_int("ACP_MODEL_DAILY_TOKENS", 5_000_000))
    daily_budget_usd: float = field(
        default_factory=lambda: _env_float("ACP_DAILY_BUDGET_USD", 1.00)
    )  # all models + embeddings
    context_token_budget: int = field(default_factory=lambda: _env_int("ACP_CONTEXT_TOKENS", 3500))
    # Which models answer which step, in fallback order (comma separated env vars). Cheap/fast model judges,
    # the strongest synthesizes; when one is out of free-tier budget the next is used.
    judge_models: tuple[str, ...] = field(
        default_factory=lambda: _chain(
            "ACP_JUDGE_MODELS", _bedrock_models()[1::-1], ("openai/gpt-oss-20b", "qwen/qwen3.8-27b")
        )
    )
    synth_models: tuple[str, ...] = field(
        default_factory=lambda: _chain(
            "ACP_SYNTH_MODELS", _bedrock_models()[1::-1], ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
        )
    )
    rerank: bool = field(default_factory=lambda: _env_bool("ACP_RERANK", False))
    store_chunk_text: bool = field(default_factory=lambda: _env_bool("ACP_STORE_CHUNK_TEXT", False))  # privacy default

    def cost_usd(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        """Estimated dollars for one LLM call, from the price table (for `model`, default the main model)."""
        name = model or self.model
        p_in, p_out = self.prices.get(price_key(name)) or self.prices.get(name) or (0.0, 0.0)
        return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


def get_settings() -> Settings:
    """Load .env (never printed) and return the settings object."""
    load_dotenv(ROOT / ".env")
    return Settings()
