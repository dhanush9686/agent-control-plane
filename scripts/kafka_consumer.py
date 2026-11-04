"""Run the span consumer: Kafka topic -> SQLite. Usage: python scripts/kafka_consumer.py [max_messages]

Needs `pip install -r requirements-streaming.txt` and a broker (see docker-compose.yml: `docker compose up -d kafka`).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confluent_kafka import Consumer  # noqa: E402

from acp.config import get_settings  # noqa: E402
from acp.streaming import consume  # noqa: E402


def main(argv: list[str]) -> int:
    s = get_settings()
    c = Consumer(
        {
            "bootstrap.servers": s.kafka_bootstrap,
            "group.id": "acp-span-writer",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    c.subscribe([s.kafka_topic])
    try:
        n = consume(c, s.db_path, max_messages=int(argv[1]) if len(argv) > 1 else None, idle_polls=30)
    finally:
        c.close()
    print(f"stored {n} span records from {s.kafka_topic} into {s.db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
