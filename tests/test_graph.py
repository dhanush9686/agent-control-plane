from acp.graph import build_graph, make_router
from acp.nodes import clean_query, extract_citations
from acp.state import initial_state
from tests.conftest import FakeLLM, FakeRetriever


def test_router_sufficient_goes_to_synthesize():
    assert make_router(2)({"sufficient_context": True, "search_attempts": 1}) == "synthesize"


def test_router_insufficient_with_tries_left_refines():
    assert make_router(2)({"sufficient_context": False, "search_attempts": 1}) == "refine_query"


def test_router_retry_cap_forces_synthesize():
    assert make_router(2)({"sufficient_context": False, "search_attempts": 2}) == "synthesize"


def test_graph_single_pass_when_sufficient(settings):
    llm, ret = FakeLLM([True]), FakeRetriever()
    final = build_graph(llm, ret, settings).invoke(initial_state("q"))
    assert final["search_attempts"] == 1
    assert llm.calls == ["judge", "synthesize"]
    assert final["answer"]


def test_graph_stops_at_retry_cap(settings):
    llm, ret = FakeLLM([False]), FakeRetriever()
    final = build_graph(llm, ret, settings).invoke(initial_state("q"))
    assert final["search_attempts"] == settings.max_attempts == len(ret.queries)
    assert llm.calls == ["judge", "refine_query", "judge", "synthesize"]
    assert ret.queries[1] == "refined query"  # second search used the refined query


def test_graph_recovers_on_second_attempt(settings):
    llm, ret = FakeLLM([False, True]), FakeRetriever()
    final = build_graph(llm, ret, settings).invoke(initial_state("q"))
    assert final["search_attempts"] == 2 and final["sufficient_context"] is True


def test_retrieval_dedups_across_attempts(settings):
    ret = FakeRetriever()
    final = build_graph(FakeLLM([False]), ret, settings).invoke(initial_state("q"))
    contents = [d.page_content for d in final["retrieved_docs"]]
    assert len(contents) == len(set(contents))


def test_clean_query_unstutters_and_strips():
    stutter = "refund timeline for failed card payments"  # real model output was this string repeated back-to-back
    assert clean_query(stutter + stutter) == stutter
    assert clean_query("abcabc") == "abc"
    assert clean_query('  "hello world"\nextra line') == "hello world"
    assert clean_query("") == ""


def test_extract_citations_handles_both_bracket_styles():
    text = "A [TAT p.3]. B 【Source: Circular notes】. C [TAT p.3]."
    assert extract_citations(text) == ["TAT p.3", "Circular notes"]
