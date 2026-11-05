"""Long-document evals: can retrieval find the right page in a 500-page document?

Analogy: hiding a $20 bill in a library and timing how often a searcher walks straight to it. We hide a fact
("needle") at different depths (start, middle, end) in a long document and check whether each search method
returns the chunk (and page) that holds it. Retrieval-only evals use ZERO LLM tokens, so they are free and fast
enough to run on every change.

Honesty notes:
 - The filler text is real English prose from local Python documentation (not random words), but it is technical
   prose and the needles are synthetic; results show retrieval mechanics, not performance on your documents.
 - Needle questions are phrased differently from the needle statements but share a rare name, which favours
   keyword search somewhat; "synthetic QA from chunks" is even more keyword-friendly. Real users paraphrase more.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import sysconfig
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from acp import store
from acp.config import ROOT, Settings
from acp.embeddings import Embedder
from acp.evals.report import wilson
from acp.hybrid import CANDIDATES, DocRetriever
from acp.ingest.chunking import Chunk, chunk_document
from acp.ingest.pdf import PageText, ParsedPdf
from acp.kb import KnowledgeBase
from acp.vectorstore import PineconeStore, VectorStore

OUT = ROOT / "results" / "longdoc_retrieval.json"
OWNER, DOC = "e" * 16, "d" * 16  # a dedicated namespace; the eval deletes its vectors when done
PAGE_CHARS = 2000  # a lighter-than-dense printed page; local prose supplies ~940K chars (~60% of a dense 500-page book)

_FIRST = ["Halvorsen", "Quillfeather", "Brannigan", "Ostrander", "Valdemar", "Kettleby", "Marchetti", "Lindqvist"]
_THING = [
    "arbitration clause",
    "warranty period",
    "escrow account",
    "service credit",
    "termination notice",
    "audit right",
]
_UNIT = ["dollars", "days", "euros", "units", "percent", "hours"]


def _stdlib_docstrings() -> list[str]:
    """Every module/class/function docstring in Python's standard library (real prose, no download, in sorted order)."""
    import ast

    root = Path(sysconfig.get_paths()["stdlib"])
    skip = ("test", "tests", "idlelib", "site-packages", "__pycache__", "lib2to3", "turtledemo", "ensurepip")
    out: list[str] = []
    for f in sorted(root.rglob("*.py")):
        if any(part in skip for part in f.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                d = ast.get_docstring(node)
                if d and len(d) >= 200:
                    out.append(d)
    return out


def local_prose(min_chars: int) -> str:
    """Deterministic-per-machine filler: Python's own docs plus README/rst files, de-duplicated by paragraph."""
    import pydoc_data.topics as topics

    paras: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        for p in re.split(r"\n\s*\n", text):
            p = re.sub(r"\s+", " ", p).strip()
            if len(p) >= 200:
                h = hashlib.md5(p.encode()).hexdigest()
                if h not in seen:
                    seen.add(h)
                    paras.append(p)

    for name in sorted(topics.topics):
        add(topics.topics[name])
    for doc in _stdlib_docstrings():
        add(doc)
        if sum(map(len, paras)) >= min_chars:
            return "\n\n".join(paras)
    site = Path(sysconfig.get_paths()["purelib"])
    for pat in ("**/*.rst", "**/*.md"):
        for f in sorted(site.glob(pat)):
            if re.search(r"licen[sc]e|copying|notice|changelog|history", f.name, re.I):
                continue
            try:
                add(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if sum(map(len, paras)) >= min_chars:
                break
    return "\n\n".join(paras)


def make_needles(n: int, seed: int = 0) -> list[dict[str, str]]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        name, thing, unit = rng.choice(_FIRST) + str(rng.randint(100, 999)), rng.choice(_THING), rng.choice(_UNIT)
        amount = f"{rng.randint(11, 98)},{rng.randint(100, 999)}"
        out.append(
            {
                "statement": f"The {thing} for the {name} contract is fixed at {amount} {unit}, by agreement.",
                "question": f"What is the {thing} in the {name} contract?",
                "answer": f"{amount} {unit}",
                "key": name,
            }
        )
    return out


def build_pages(
    n_pages: int, needles: list[dict[str, str]], depths: list[int]
) -> tuple[list[PageText], dict[int, int]]:
    """Pages of real prose with each needle inserted on the page at its depth. Returns (pages, needle_index -> page)."""
    prose = local_prose(n_pages * PAGE_CHARS * 2)
    pages = [prose[i * PAGE_CHARS : (i + 1) * PAGE_CHARS] for i in range(n_pages)]
    if sum(len(p) for p in pages) < n_pages * PAGE_CHARS * 0.9:
        raise SystemExit(f"Not enough local prose for {n_pages} pages; try --pages {len(prose) // PAGE_CHARS}")
    where: dict[int, int] = {}
    for i, nd in enumerate(needles):
        depth = depths[i % len(depths)]
        page = max(1, min(n_pages, round(depth / 100 * (n_pages - 1)) + 1))
        half = len(pages[page - 1]) // 2
        pages[page - 1] = pages[page - 1][:half] + f" {nd['statement']} " + pages[page - 1][half:]
        where[i] = page
    return [PageText(i + 1, None, t) for i, t in enumerate(pages)], where


def _rank_of(order: list[int], target_idxs: set[int]) -> int | None:
    for rank, idx in enumerate(order, 1):
        if idx in target_idxs:
            return rank
    return None


def evaluate_modes(
    retrievers: dict[str, DocRetriever], cases: list[dict[str, Any]], ks: tuple[int, ...] = (1, 5, 10)
) -> dict[str, Any]:
    """cases: [{query, targets:set[idx], pages:set[int]}]. Returns per-mode recall@k, MRR and page-hit rate."""
    out: dict[str, Any] = {}
    for mode, r in retrievers.items():
        ranks, page_hits = [], 0
        for c in cases:
            docs = r.search(c["query"], max(ks))
            order = [d.metadata["idx"] for d in docs]
            ranks.append(_rank_of(order, c["targets"]))
            page_hits += any(d.metadata["page"] in c["pages"] for d in docs[:5])
        n = len(cases)
        out[mode] = {
            "n": n,
            **{
                f"recall@{k}": {
                    "value": round(sum(1 for x in ranks if x and x <= k) / n, 4),
                    "ci95": wilson(sum(1 for x in ranks if x and x <= k), n),
                }
                for k in ks
            },
            "mrr": round(sum(1 / x for x in ranks if x) / n, 4),
            "page_hit@5": round(page_hits / n, 4),
        }
    return out


def distinctive_query(text: str, doc_freq: dict[str, int], words: int = 5, seed: int = 0) -> str:
    """A 5-word probe made of the chunk's rarest words (what a user typing key terms might search)."""
    toks = list(dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)))
    toks.sort(key=lambda w: (doc_freq.get(w, 0), w))
    return " ".join(toks[:words])


def run(
    embedder: Embedder,
    vstore: VectorStore,
    n_pages: int = 400,
    depths: tuple[int, ...] = (0, 25, 50, 75, 100),
    n_needles: int = 25,
    n_synthetic: int = 100,
    use_rerank: bool = False,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    needles = make_needles(n_needles)
    pages, where = build_pages(n_pages, needles, list(depths))
    chunks = chunk_document(ParsedPdf(pages=pages), 900, 150, max_chunks=n_pages * 20)
    progress(f"{n_pages} pages -> {len(chunks)} chunks; embedding...")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        kb = KnowledgeBase(Path(tmp) / "kb", embedder, vstore)
        conn = store.connect(db)
        kb.add_document(conn, OWNER, DOC, chunks)
        conn.close()
        try:
            return _measure(kb, db, chunks, needles, where, depths, n_pages, n_synthetic, use_rerank, embedder)
        finally:
            vstore.delete_doc(OWNER, DOC)  # never leave eval vectors behind in the user's Pinecone index


def _measure(
    kb: KnowledgeBase,
    db: Path,
    chunks: list[Chunk],
    needles: list[dict[str, str]],
    where: dict[int, int],
    depths: tuple[int, ...],
    n_pages: int,
    n_synthetic: int,
    use_rerank: bool,
    embedder: Embedder,
) -> dict[str, Any]:
    reranker = None
    if use_rerank:
        from acp.rerank import CrossEncoderReranker

        reranker = CrossEncoderReranker()
    retrievers: dict[str, DocRetriever] = {
        m: DocRetriever(kb, lambda: store.connect(db), OWNER, DOC, "demo.pdf", mode=m)
        for m in ("dense", "bm25", "hybrid")
    }
    if reranker:
        retrievers["hybrid+rerank"] = DocRetriever(
            kb, lambda: store.connect(db), OWNER, DOC, "demo.pdf", mode="hybrid", reranker=reranker
        )

    # needle cases: the target is every chunk containing the needle's rare key
    needle_cases = []
    for i, nd in enumerate(needles):
        targets = {c.idx for c in chunks if nd["key"] in c.text}
        needle_cases.append(
            {"query": nd["question"], "targets": targets, "pages": {where[i]}, "depth": depths[i % len(depths)]}
        )
    by_depth: dict[int, Any] = {}
    for d in depths:
        sub = [c for c in needle_cases if c["depth"] == d]
        by_depth[d] = evaluate_modes(retrievers, sub, ks=(5,)) if sub else {}
    # synthetic QA from random chunks
    df: dict[str, int] = {}
    for c in chunks:
        for w in set(w.lower() for w in re.findall(r"[A-Za-z]{4,}", c.text)):
            df[w] = df.get(w, 0) + 1
    rng = random.Random(1)
    sample = rng.sample(chunks, min(n_synthetic, len(chunks)))
    synth_cases = [{"query": distinctive_query(c.text, df), "targets": {c.idx}, "pages": {c.page}} for c in sample]
    return {
        "pages": n_pages,
        "chunks": len(chunks),
        "embedder": type(embedder).__name__,
        "vector_store": "pinecone",
        "needles": evaluate_modes(retrievers, needle_cases),
        "needles_by_depth": by_depth,
        "synthetic_qa": evaluate_modes(retrievers, synth_cases),
        "candidates_per_searcher": CANDIDATES,
    }


def render(report: dict[str, Any]) -> str:
    def row(name: str, r: dict[str, Any]) -> str:
        return (
            f"| {name} | {r['recall@1']['value']:.2f} | {r['recall@5']['value']:.2f} "
            f"({r['recall@5']['ci95'][0]:.2f}-{r['recall@5']['ci95'][1]:.2f}) | {r['recall@10']['value']:.2f} "
            f"| {r['mrr']:.2f} | {r['page_hit@5']:.2f} |"
        )

    head = "| mode | recall@1 | recall@5 (95% CI) | recall@10 | MRR | page hit@5 |\n|---|---|---|---|---|---|"
    out = [
        f"{report['pages']} pages, {report['chunks']} chunks, {report['embedder']}, {report.get('vector_store', '?')}",
        "",
        "**Needles**",
        head,
    ]
    out += [row(m, r) for m, r in report["needles"].items()]
    out += ["", "**Synthetic QA from chunks (keyword-friendly)**", head]
    out += [row(m, r) for m, r in report["synthetic_qa"].items()]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="acp.evals.longdoc", description="Retrieval-only long-document evals (no LLM).")
    p.add_argument("--retrieval-only", action="store_true", required=True, help="(the only mode implemented here)")
    p.add_argument("--pages", type=int, default=400)
    p.add_argument("--depths", default="0,25,50,75,100")
    p.add_argument("--needles", type=int, default=25)
    p.add_argument("--synthetic", type=int, default=100)
    p.add_argument("--index", default=None, help="Pinecone index (default acp-eval-<dim>; created if missing)")
    p.add_argument("--rerank", action="store_true", help="also evaluate hybrid + cross-encoder reranking")
    args = p.parse_args(argv)
    from dotenv import load_dotenv

    from acp.embeddings import make_embedder

    load_dotenv(ROOT / ".env")  # PINECONE_API_KEY lives here; never printed
    embedder = make_embedder(Settings())  # Bedrock Titan v2 by default (ACP_EMBEDDING_PROVIDER=minilm for local)
    vstore = PineconeStore.connect(dimension=embedder.dim, index_name=args.index or f"acp-eval-{embedder.dim}")
    report = run(
        embedder,
        vstore,
        args.pages,
        tuple(int(x) for x in args.depths.split(",")),
        args.needles,
        args.synthetic,
        args.rerank,
    )
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(render(report))
    print(f"\nSaved {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
