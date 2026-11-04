"""Load the lineage graph into a local Neo4j (free Community edition via docker-compose.yml).

    docker compose up -d neo4j && python scripts/export_traces.py && python scripts/load_neo4j.py
Then open http://localhost:7474 (user neo4j, the dev password in docker-compose.yml) and try the queries in the README.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from neo4j import GraphDatabase  # noqa: E402

from acp import store  # noqa: E402
from acp.config import get_settings  # noqa: E402
from acp.export import build_export  # noqa: E402
from acp.lineage import cypher_statements  # noqa: E402


def main() -> int:
    uri = os.environ.get("ACP_NEO4J_URI", "bolt://localhost:7687")
    auth = (os.environ.get("ACP_NEO4J_USER", "neo4j"), os.environ.get("ACP_NEO4J_PASSWORD", "acp-local-dev"))
    conn = store.connect(get_settings().db_path)
    try:
        graph = build_export(conn)["evals"][0]["_graph"]
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    stmts = cypher_statements(graph)
    with GraphDatabase.driver(uri, auth=auth) as driver, driver.session() as session:
        for stmt in stmts:
            session.run(stmt).consume()
        counts = session.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label").data()
    print(f"ran {len(stmts)} statements; node counts: {json.dumps(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
