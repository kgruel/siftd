"""CLI tests for 'siftd embed' — build / rebuild / status / error surfaces."""

import argparse
import json

import pytest

pytestmark = pytest.mark.embeddings

pytest.importorskip("fastembed")

from siftd.cli.embed import cmd_embed
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)


def make_args(**kwargs):
    defaults = {"db": None, "embed_db": None, "rebuild": False, "status": False, "json": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


@pytest.fixture
def main_db(tmp_path):
    db = tmp_path / "main.db"
    conn = create_database(db)
    h = get_or_create_harness(conn, "t", source="t", log_format="jsonl")
    m = get_or_create_model(conn, "test-model")
    w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    for i in range(2):
        cid = insert_conversation(conn, external_id=f"c{i}", harness_id=h, workspace_id=w, started_at=f"2024-01-0{i + 1}T00:00:00Z")
        pid = insert_prompt(conn, cid, f"p{i}", f"2024-01-0{i + 1}T00:00:01Z")
        insert_prompt_content(conn, pid, 0, "text", '{"text": "How do I handle Python errors gracefully?"}')
        rid = insert_response(conn, cid, pid, m, None, f"r{i}", f"2024-01-0{i + 1}T00:00:02Z", input_tokens=5, output_tokens=10)
        insert_response_content(conn, rid, 0, "text", '{"text": "Use try/except blocks."}')
    conn.commit()
    conn.close()
    return db


def test_embed_build(main_db, tmp_path, capsys):
    edb = tmp_path / "embed.db"
    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    assert rc == 0
    assert edb.exists()
    out = capsys.readouterr().out
    assert "chunk" in out.lower()


def test_embed_incremental_noop(main_db, tmp_path, capsys):
    edb = tmp_path / "embed.db"
    cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    capsys.readouterr()
    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    assert rc == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_embed_rebuild(main_db, tmp_path, capsys):
    edb = tmp_path / "embed.db"
    cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    capsys.readouterr()
    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb), rebuild=True))
    assert rc == 0
    assert "chunk" in capsys.readouterr().out.lower()


def test_embed_status_human(main_db, tmp_path, capsys):
    edb = tmp_path / "embed.db"
    cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    capsys.readouterr()
    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb), status=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "fastembed" in out
    assert "Coverage" in out or "coverage" in out.lower()
    assert "Chunks" in out or "chunks" in out.lower()


def test_embed_status_json(main_db, tmp_path, capsys):
    edb = tmp_path / "embed.db"
    cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    capsys.readouterr()
    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb), status=True, json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["index_exists"] is True
    assert data["stored_backend"] == "fastembed"
    assert data["conversations_total"] == 2
    assert data["conversations_indexed"] == 2
    assert data["schema_version"] == 2


def test_embed_status_no_index(main_db, tmp_path, capsys):
    edb = tmp_path / "never-built.db"
    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb), status=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "not built" in out


def test_embed_missing_db(tmp_path, capsys):
    rc = cmd_embed(make_args(db=str(tmp_path / "missing.db"), embed_db=str(tmp_path / "e.db")))
    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_embed_no_backend_configured(main_db, tmp_path, monkeypatch, capsys):
    edb = tmp_path / "embed.db"
    monkeypatch.setattr("siftd.api.embeddings_available", lambda: False, raising=False)
    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    assert rc == 1
    err = capsys.readouterr().err
    assert "No embedding backend is configured" in err
    assert "siftd install embed" in err or "embed.backend" in err


def test_embed_v1_index_rebuild_hint(main_db, tmp_path, capsys):
    """An incremental build over a v1 index errors, pointing at --rebuild."""
    import sqlite3

    edb = tmp_path / "embed.db"
    cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    capsys.readouterr()

    raw = sqlite3.connect(edb)
    raw.execute("UPDATE index_meta SET value='1' WHERE key='schema_version'")
    raw.commit()
    raw.close()

    rc = cmd_embed(make_args(db=str(main_db), embed_db=str(edb)))
    assert rc == 1
    assert "siftd embed --rebuild" in capsys.readouterr().err
