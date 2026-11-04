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
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 4  # extra tries on 429/5xx/timeouts, with backoff
    corpus_path: Path = ROOT / "data" / "corpus.txt"
    index_path: Path = ROOT / "data" / "faiss_index"
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("ACP_DB_PATH", ROOT / "data" / "traces.db")))
    prices: dict[str, tuple[float, float]] = field(default_factory=load_prices)

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        """Estimated dollars for one LLM call, from the price table."""
        p_in, p_out = self.prices.get(self.model, (0.0, 0.0))
        return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


def get_settings() -> Settings:
    """Load .env (never printed) and return the settings object."""
    load_dotenv(ROOT / ".env")
    return Settings()
