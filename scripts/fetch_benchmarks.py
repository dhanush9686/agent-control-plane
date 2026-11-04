"""Download small slices of public datasets and convert them. Usage: python scripts/fetch_benchmarks.py

Uses Hugging Face's public rows API (no account, no token). Output goes to data/benchmarks/ which is gitignored
on purpose: some of these datasets do not state a redistribution licence on their Hugging Face card, so we fetch
them on demand instead of committing them. data/benchmarks/MANIFEST.json records exactly what was fetched.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acp.benchmarks import build_squad_kb, fever_items, ragtruth_items  # noqa: E402

API = "https://datasets-server.huggingface.co"
OUT = ROOT / "data" / "benchmarks"


def fetch_rows(dataset: str, config: str, split: str, offsets: list[int], length: int = 100) -> list[dict]:
    rows: list[dict] = []
    for off in offsets:
        q = urllib.parse.urlencode(
            {"dataset": dataset, "config": config, "split": split, "offset": off, "length": length}
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(f"{API}/rows?{q}", timeout=60) as r:
                    rows += [x["row"] for x in json.load(r)["rows"]]
                time.sleep(1.0)  # be polite: the public API rate-limits bursts (HTTP 429)
                break
            except Exception as e:  # noqa: BLE001 - public API can hiccup; retry politely
                if attempt == 4:
                    raise
                print(f"  retry {dataset}@{off}: {type(e).__name__}", file=sys.stderr)
                time.sleep(10 * (attempt + 1))
    return rows


def card_license(dataset: str) -> str:
    try:
        with urllib.request.urlopen(f"https://huggingface.co/api/datasets/{dataset}", timeout=30) as r:
            lic = json.load(r).get("cardData", {}).get("license")
        return ", ".join(lic) if isinstance(lic, list) else (lic or "not stated on the Hugging Face card")
    except Exception:  # noqa: BLE001
        return "unknown (could not read card)"


def main(argv: list[str]) -> int:
    """Optional args pick sources: squad2 ragtruth fever (default: all). Existing manifest entries are kept."""
    wanted = set(argv[1:]) or {"squad2", "ragtruth", "fever"}
    OUT.mkdir(parents=True, exist_ok=True)
    mpath = OUT / "MANIFEST.json"
    manifest: dict = json.loads(mpath.read_text()) if mpath.is_file() else {"sources": {}}
    manifest["fetched_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    if "squad2" in wanted:
        fetch_squad(manifest)
    if "ragtruth" in wanted:
        fetch_ragtruth(manifest)
    if "fever" in wanted:
        fetch_fever(manifest)
    mpath.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest["sources"], indent=2))
    return 0


def fetch_squad(manifest: dict) -> None:
    # SQuAD 2.0 validation: spread offsets so several articles (titles) are covered.
    print("SQuAD 2.0 ...")
    offsets = list(range(0, 11800, 1000))
    rows = fetch_rows("rajpurkar/squad_v2", "squad_v2", "validation", offsets)
    corpus, cases = build_squad_kb(rows, n_questions=60)
    (OUT / "squad2").mkdir(exist_ok=True)
    (OUT / "squad2" / "corpus.txt").write_text(corpus, encoding="utf-8")
    (OUT / "squad2" / "golden.yaml").write_text(
        yaml.safe_dump(cases, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    manifest["sources"]["squad2"] = {
        "dataset": "rajpurkar/squad_v2",
        "split": "validation",
        "offsets": offsets,
        "rows_read": len(rows),
        "license": card_license("rajpurkar/squad_v2"),
        "questions": len(cases),
        "kb_chunks": corpus.count("\n\n") + 1,
    }


def fetch_ragtruth(manifest: dict) -> None:
    print("RAGTruth ...")
    rows = fetch_rows("wandb/RAGTruth-processed", "default", "test", list(range(0, 2700, 100)))
    items = ragtruth_items(rows, per_class=20)
    (OUT / "judge_ragtruth.jsonl").write_text(
        "\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8"
    )
    manifest["sources"]["ragtruth"] = {
        "dataset": "wandb/RAGTruth-processed",
        "split": "test",
        "rows_read": len(rows),
        "license": card_license("wandb/RAGTruth-processed"),
        "items": len(items),
    }


def fetch_fever(manifest: dict) -> None:
    print("FEVER ...")
    rows = fetch_rows("pietrolesci/nli_fever", "default", "dev", list(range(0, 2000, 100)))
    items = fever_items(rows, per_class=20)
    (OUT / "judge_fever.jsonl").write_text(
        "\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8"
    )
    manifest["sources"]["fever"] = {
        "dataset": "pietrolesci/nli_fever",
        "split": "dev",
        "rows_read": len(rows),
        "license": card_license("pietrolesci/nli_fever"),
        "items": len(items),
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv))
