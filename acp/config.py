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
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.75),
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
