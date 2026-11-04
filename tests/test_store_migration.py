import sqlite3
import threading

import pytest

from acp import store
from acp.tracing import write_record


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}


def test_fresh_database_gets_latest_schema(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.LATEST
    assert {"runs", "spans", "users", "docs", "jobs", "chunks", "usage", "chunks_fts"} <= _tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"user_id", "doc_id"} <= cols


def test_old_version1_database_is_migrated_without_losing_data(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)  # exactly what releases before multi-user support created
    old.executescript(store.SCHEMA)
    old.execute(
        "INSERT INTO runs (run_id, question, started_at, status) VALUES ('r1', 'old question', '2026-01-01', 'ok')"
    )
    old.commit()
    assert old.execute("PRAGMA user_version").fetchone()[0] == 0
    old.close()

    conn = store.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.LATEST
    row = conn.execute("SELECT question, user_id, doc_id FROM runs WHERE run_id='r1'").fetchone()
    assert tuple(row) == ("old question", None, None)  # data kept; new columns default to NULL


def test_reopening_is_a_no_op_and_idempotent(tmp_path):
    store.connect(tmp_path / "t.db").close()
    conn = store.connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.LATEST


def test_concurrent_first_opens_do_not_collide(tmp_path):
    errors = []

    def open_it():
        try:
            store.connect(tmp_path / "race.db").close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=open_it) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert store.connect(tmp_path / "race.db").execute("PRAGMA user_version").fetchone()[0] == store.LATEST


def test_same_owner_cannot_register_same_file_twice(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    q = "INSERT INTO docs (id, owner_id, sha256, filename, status, created_at) VALUES (?,?,?,?,?,?)"
    conn.execute(q, ("d1", "u1", "abc", "a.pdf", "queued", "now"))
    conn.execute(q, ("d2", "u2", "abc", "a.pdf", "queued", "now"))  # a different owner may upload the same bytes
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(q, ("d3", "u1", "abc", "again.pdf", "queued", "now"))


def test_full_text_search_filters_by_document(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    conn.executemany(
        "INSERT INTO chunks_fts (rowid, text, doc_id, owner_id) VALUES (?,?,?,?)",
        [(1, "the quick brown fox", "d1", "u1"), (2, "the quick red fox", "d2", "u1")],
    )
    hits = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'fox' AND doc_id = 'd2'").fetchall()
    assert [h[0] for h in hits] == [2]


def test_run_record_carries_user_and_doc_ids(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    base = {
        "span_id": "s1",
        "run_id": "r1",
        "parent_id": None,
        "name": "agent.run",
        "kind": "run",
        "start_ns": 1_000_000_000,
        "end_ns": 2_000_000_000,
        "run_start_ns": 1_000_000_000,
        "status": "ok",
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "retries": None,
        "question": "q",
        "error": None,
        "attrs": {"answer": "a"},
    }
    with conn:
        write_record(conn, {**base, "user_id": "u1", "doc_id": "d1"})
    row = conn.execute("SELECT user_id, doc_id FROM runs WHERE run_id='r1'").fetchone()
    assert tuple(row) == ("u1", "d1")
