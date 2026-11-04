from acp.lineage import build_lineage, document_stats, find_supporting_chunk, link_claims, to_cypher

RETRIEVED = [
    {"source": "KGF 1", "text": "Rocky kills Garuda in a final confrontation."},
    {"source": "Dragon", "text": "Dragon is unreleased; slated for June 2027."},
]


def test_evidence_quote_maps_to_chunk():
    assert find_supporting_chunk("Rocky kills Garuda", RETRIEVED) == 0
    assert find_supporting_chunk("slated for June 2027", RETRIEVED) == 1
    assert find_supporting_chunk("", RETRIEVED) is None
    assert find_supporting_chunk("completely unrelated words here", RETRIEVED) is None


def test_fuzzy_paraphrased_quote_still_matches():
    assert find_supporting_chunk("Rocky kills Garuda in the final confrontation", RETRIEVED) == 0


def test_link_claims_only_links_supported():
    out = link_claims(
        [
            {"claim": "a", "supported": True, "evidence": "Rocky kills Garuda"},
            {"claim": "b", "supported": False, "evidence": ""},
        ],
        RETRIEVED,
    )
    assert out[0]["chunk_index"] == 0 and out[1]["chunk_index"] is None


def _graph():
    results = [
        {
            "case_id": "c1",
            "question": "q",
            "category": "answerable",
            "run_id": "r1",
            "answer": "A",
            "passed": False,
            "claims": [
                {"claim": "Rocky kills Garuda", "supported": True, "evidence": "Rocky kills Garuda"},
                {"claim": "Rocky is a pirate", "supported": False, "evidence": ""},
            ],
        }
    ]
    runs = [{"run_id": "r1", "latency_ms": 5.0, "cost_usd": 0.0, "retrieved": RETRIEVED}]
    return build_lineage(results, runs)


def test_graph_shape():
    g = _graph()
    labels = [n["label"] for n in g["nodes"]]
    assert labels.count("Chunk") == 2 and labels.count("Claim") == 2 and labels.count("Document") == 2
    types = [e["type"] for e in g["edges"]]
    assert types.count("SUPPORTED_BY") == 1 and types.count("RETRIEVED") == 2 and types.count("MAKES") == 2


def test_document_stats_flags_distractors():
    stats = {d["document"]: d for d in document_stats(_graph())}
    assert stats["KGF 1"]["supported_claims"] == 1
    assert stats["Dragon"]["supported_claims"] == 0 and stats["Dragon"]["unsupported_claims_in_runs"] == 1


def test_cypher_escapes_quotes_and_is_idempotent():
    g = {"nodes": [{"id": "n1", "label": "Claim", "text": 'it\'s "fine"', "supported": True}], "edges": []}
    cy = to_cypher(g)
    assert "MERGE (n:Claim {id: 'n1'}) SET n.text = 'it\\'s \"fine\"', n.supported = true;" in cy
    assert "CREATE CONSTRAINT claim_id IF NOT EXISTS" in cy


def test_relationship_statements_use_labels():
    from acp.lineage import cypher_statements

    stmts = cypher_statements(_graph())
    assert any(s.startswith("MATCH (a:Claim") and "SUPPORTED_BY" in s for s in stmts)
