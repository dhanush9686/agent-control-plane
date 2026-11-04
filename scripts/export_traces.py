"""Write dashboard/data/traces.json from the SQLite DB. Usage: python scripts/export_traces.py [eval_id]"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acp import store  # noqa: E402
from acp.config import get_settings  # noqa: E402
from acp.export import build_export, lineage_cypher, strip_private  # noqa: E402

OUT = ROOT / "dashboard" / "data" / "traces.json"


def main(argv: list[str]) -> int:
    conn = store.connect(get_settings().db_path)
    try:
        data = build_export(conn, argv[1] if len(argv) > 1 else None)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "lineage.cypher").write_text(lineage_cypher(data))
    data = strip_private(data)
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"wrote {OUT} ({len(data['evals'])} evals, {sum(len(e['runs']) for e in data['evals'])} runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
