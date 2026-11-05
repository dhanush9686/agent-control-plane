import pytest

from acp.cli import main
from acp.config import ROOT, Settings
from acp.errors import EmptyIndexError
from acp.index import load_and_chunk
from acp.retrieval import FaissRetriever


def test_corpus_chunks_match_known_count():
    docs = load_and_chunk(ROOT / "data" / "corpus.txt")
    assert len(docs) == 31
    assert {d.metadata["source"] for d in docs} >= {"Universe notes"}


def test_empty_index_gives_friendly_error(tmp_path):
    with pytest.raises(EmptyIndexError, match="make index"):
        FaissRetriever(Settings(index_path=tmp_path / "nope"))


def test_cli_missing_groq_key_prints_friendly_message(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("acp.cli.get_settings", lambda: Settings(db_path=tmp_path / "t.db", llm_provider="groq"))
    assert main(["hello", "--no-trace"]) == 1
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_cli_missing_aws_credentials_prints_friendly_message(monkeypatch, capsys, tmp_path):
    import boto3

    monkeypatch.setattr(boto3.Session, "get_credentials", lambda self: None)
    monkeypatch.setattr(
        "acp.cli.get_settings",
        lambda: Settings(db_path=tmp_path / "t.db", llm_provider="bedrock", aws_region="ap-south-1"),
    )
    assert main(["hello", "--no-trace"]) == 1
    err = capsys.readouterr().err
    assert "AWS credentials" in err and "aws configure" in err
