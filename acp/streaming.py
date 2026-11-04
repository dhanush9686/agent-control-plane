"""Kafka consumer side: read span records from a topic and file them into SQLite.

Analogy: the clerk who empties the outbox. The agent just drops span records on a topic; this loop picks them up
and writes them with the same `write_record` the direct SQLite exporter uses. If the clerk is slow or
down, the agent is unaffected and the messages wait on the topic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from acp import store
from acp.tracing import write_record

log = logging.getLogger("acp.streaming")


def consume(consumer: Any, db_path: Path | str, max_messages: int | None = None, idle_polls: int = 5) -> int:
    """Poll until `max_messages` were stored or `idle_polls` empty polls in a row. Returns messages stored.

    `consumer` needs poll(timeout) -> message|None, and message.error()/message.value(); plus commit().
    Bad messages are logged and skipped so one poison record cannot stall the pipeline.
    """
    stored, idle = 0, 0
    conn = store.connect(db_path)
    try:
        while (max_messages is None or stored < max_messages) and idle < idle_polls:
            msg = consumer.poll(1.0)
            if msg is None:
                idle += 1
                continue
            idle = 0
            if msg.error():
                log.warning("kafka error: %s", msg.error())
                continue
            try:
                with conn:
                    write_record(conn, json.loads(msg.value()))
                stored += 1
            except Exception:  # noqa: BLE001 - skip poison messages
                log.exception("skipping bad span record")
            if hasattr(consumer, "commit"):
                consumer.commit(message=msg, asynchronous=False)
    finally:
        conn.close()
    return stored
