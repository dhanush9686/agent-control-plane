"""Thin entry point: `python build_index.py` (logic lives in acp/index.py)."""

from acp.errors import ACPError
from acp.index import build_index

if __name__ == "__main__":
    try:
        n = build_index()
        print(f"Indexed {n} chunks.")
    except ACPError as e:
        raise SystemExit(f"error: {e}") from None
