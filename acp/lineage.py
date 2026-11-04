"""Lineage graph: which source chunks supported (or failed to support) which claims.

Analogy: footnotes with receipts. For every answer we draw lines
    Question -> Run -> retrieved Chunks -> (part of) Documents
    Run -> Answer -> Claims -> supporting Chunk (the quote the judge cited)
so that when a claim is wrong you can see exactly what was retrieved and what was missing.

It is stored as plain nodes + edges, and can be exported as Cypher for Neo4j (free Community edition,
`docker-compose.yml`) or read directly by the dashboard's Lineage tab. No graph database is required.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from acp.evals.checks import normalize


def chunk_id(source: str, text: str) -> str:
    return "chunk:" + hashlib.sha1(f"{source}\n{text}".encode()).hexdigest()[:10]


def find_supporting_chunk(evidence: str, retrieved: list[dict[str, str]]) -> int | None:
    """Index of the retrieved chunk containing the judge's evidence quote (None if no chunk matches).

    Exact normalized substring first; otherwise accept a chunk containing >=80% of the quote's words
    (judges sometimes paraphrase slightly while "quoting").
    """
    ev = normalize(evidence).strip()
    if not ev:
        return None
    for i, c in enumerate(retrieved):
        if ev in normalize(c["text"]):
            return i
    words = set(ev.split())
    best, best_i = 0.0, None
    for i, c in enumerate(retrieved):
        overlap = len(words & set(normalize(c["text"]).split())) / len(words)
        if overlap > best:
            best, best_i = overlap, i
    return best_i if best >= 0.8 else None


def link_claims(claims: list[dict[str, Any]], retrieved: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Add `chunk_index` (or None) to each claim verdict."""
    return [
        {**c, "chunk_index": find_supporting_chunk(c.get("evidence", ""), retrieved) if c.get("supported") else None}
        for c in claims
    ]


def build_lineage(results: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Nodes/edges for one evaluation. `results` = eval case results; `runs` = exported runs (with `retrieved`)."""
    runs_by_id = {r["run_id"]: r for r in runs}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def node(nid: str, label: str, **props: Any) -> None:
        nodes.setdefault(nid, {"id": nid, "label": label, **props})

    for res in results:
        run = runs_by_id.get(res.get("run_id"))
        if run is None:
            continue
        qid, rid, aid = f"question:{res['case_id']}", f"run:{run['run_id']}", f"answer:{run['run_id']}"
        node(qid, "Question", text=res["question"], category=res["category"])
        node(rid, "Run", latency_ms=run["latency_ms"], cost_usd=run["cost_usd"])
        node(aid, "Answer", text=res.get("answer", ""), passed=bool(res.get("passed")))
        edges += [{"from": qid, "type": "HAD_RUN", "to": rid}, {"from": rid, "type": "PRODUCED", "to": aid}]
        chunk_ids = []
        for rank, c in enumerate(run["retrieved"]):
            cid = chunk_id(c["source"], c["text"])
            chunk_ids.append(cid)
            did = f"doc:{c['source']}"
            node(did, "Document", name=c["source"])
            node(cid, "Chunk", text=c["text"])
            edges += [
                {"from": cid, "type": "PART_OF", "to": did},
                {"from": rid, "type": "RETRIEVED", "to": cid, "rank": rank},
            ]
        for i, claim in enumerate(link_claims(res.get("claims", []), run["retrieved"])):
            clid = f"claim:{run['run_id']}:{i}"
            node(clid, "Claim", text=claim["claim"], supported=bool(claim["supported"]))
            edges.append({"from": aid, "type": "MAKES", "to": clid})
            if claim["chunk_index"] is not None:
                edges.append(
                    {
                        "from": clid,
                        "type": "SUPPORTED_BY",
                        "to": chunk_ids[claim["chunk_index"]],
                        "quote": claim.get("evidence", ""),
                    }
                )
    return {"nodes": list(nodes.values()), "edges": edges}


def document_stats(graph: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Per-document: how often its chunks were retrieved, how many claims they supported, and how many wrong claims
    appeared in answers whose retrieval included the document (a hint that it is a distractor or too thin)."""
    node_by_id = {n["id"]: n for n in graph["nodes"]}
    doc_of = {e["from"]: e["to"] for e in graph["edges"] if e["type"] == "PART_OF"}
    stats: dict[str, dict[str, int]] = {}

    def s(did: str) -> dict[str, int]:
        return stats.setdefault(did, {"retrieved": 0, "supported_claims": 0, "unsupported_claims_in_runs": 0})

    run_docs: dict[str, set[str]] = {}
    for e in graph["edges"]:
        if e["type"] == "RETRIEVED":
            s(doc_of[e["to"]])["retrieved"] += 1
            run_docs.setdefault(e["from"], set()).add(doc_of[e["to"]])
        elif e["type"] == "SUPPORTED_BY":
            s(doc_of[e["to"]])["supported_claims"] += 1
    answer_run = {e["to"]: e["from"] for e in graph["edges"] if e["type"] == "PRODUCED"}
    claim_answer = {e["to"]: e["from"] for e in graph["edges"] if e["type"] == "MAKES"}
    for n in graph["nodes"]:
        if n["label"] == "Claim" and not n["supported"]:
            for did in run_docs.get(answer_run.get(claim_answer.get(n["id"], ""), ""), ()):
                s(did)["unsupported_claims_in_runs"] += 1
    return sorted(({"document": node_by_id[d]["name"], **v} for d, v in stats.items()), key=lambda r: -r["retrieved"])


def _q(value: Any) -> str:
    """Cypher string/number/bool literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + re.sub(r"[\\']", lambda m: "\\" + m.group(0), str(value)).replace("\n", " ") + "'"


LABELS = ("Question", "Run", "Chunk", "Document", "Answer", "Claim")


def cypher_statements(graph: dict[str, list[dict[str, Any]]]) -> list[str]:
    """One Cypher statement per item: constraints first, then nodes (MERGE on id), then relationships.

    Re-running is safe: MERGE on a unique id updates instead of duplicating.
    """
    label_of = {n["id"]: n["label"] for n in graph["nodes"]}
    out = [f"CREATE CONSTRAINT {lab.lower()}_id IF NOT EXISTS FOR (n:{lab}) REQUIRE n.id IS UNIQUE" for lab in LABELS]
    for n in graph["nodes"]:
        sets = ", ".join(f"n.{k} = {_q(v)}" for k, v in n.items() if k not in ("label", "id"))
        out.append(f"MERGE (n:{n['label']} {{id: {_q(n['id'])}}})" + (f" SET {sets}" if sets else ""))
    for e in graph["edges"]:
        extra = {k: v for k, v in e.items() if k not in ("from", "to", "type")}
        props = (" {" + ", ".join(f"{k}: {_q(v)}" for k, v in extra.items()) + "}") if extra else ""
        out.append(
            f"MATCH (a:{label_of[e['from']]} {{id: {_q(e['from'])}}}), (b:{label_of[e['to']]} {{id: {_q(e['to'])}}}) "
            f"MERGE (a)-[:{e['type']}{props}]->(b)"
        )
    return out


def to_cypher(graph: dict[str, list[dict[str, Any]]]) -> str:
    """Statements joined with ';' lines: paste into Neo4j Browser or pipe into cypher-shell."""
    return "// Generated by acp.lineage (safe to re-run)\n" + ";\n".join(cypher_statements(graph)) + ";\n"
