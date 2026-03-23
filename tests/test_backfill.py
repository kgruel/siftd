"""Tests for siftd.backfill module."""

import json
import sqlite3

import pytest

from siftd.backfill import (
    backfill_derivative_tags,
    backfill_filter_binary,
    backfill_models,
    backfill_providers,
    backfill_response_attributes,
    backfill_shell_tags,
)
from siftd.storage.sqlite import open_database


@pytest.fixture()
def db(tmp_path):
    conn = open_database(tmp_path / "test.db")
    yield conn
    conn.close()


def _chain(c, *, hid="h1", wid="w1", cid="c1", mid="m1", rid="r1", ext_id=None):
    """Harness→workspace→conversation→model→response in one call."""
    c.execute("INSERT OR IGNORE INTO harnesses (id, name, source) VALUES (?, 'claude_code', 'anthropic')", (hid,))
    c.execute("INSERT OR IGNORE INTO workspaces (id, path, discovered_at) VALUES (?, '/p', '2024-01-01')", (wid,))
    c.execute("INSERT OR IGNORE INTO conversations (id, external_id, harness_id, workspace_id, started_at) VALUES (?, ?, ?, ?, '2024-01-01')", (cid, f"e_{cid}", hid, wid))
    c.execute("INSERT OR IGNORE INTO models (id, raw_name, name) VALUES (?, 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet-20241022')", (mid,))
    c.execute("INSERT OR IGNORE INTO responses (id, conversation_id, model_id, external_id, timestamp) VALUES (?, ?, ?, ?, '2024-01-01')", (rid, cid, mid, ext_id or f"e_{rid}"))


def _tid(c, name="shell.execute"):
    row = c.execute("SELECT id FROM tools WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def _tc(c, tcid, rid, cid, tid, inp=""):
    s = json.dumps(inp) if isinstance(inp, dict) else inp
    c.execute("INSERT OR IGNORE INTO tool_calls (id, response_id, conversation_id, tool_id, input) VALUES (?, ?, ?, ?, ?)", (tcid, rid, cid, tid, s))


def _bare_db(tmp_path, name="bare.db"):
    """Bare DB with only a tools table (no seeded data)."""
    conn = sqlite3.connect(str(tmp_path / name))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT UNIQUE)")
    return conn


def _jsonl(tmp_path, records, name="session.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records))
    return p


def _ingest(c, path, fid="f1"):
    c.execute(
        "INSERT INTO ingested_files (id, path, file_hash, harness_id, conversation_id, ingested_at) "
        "VALUES (?, ?, 'h', 'h1', 'c1', '2024-01-01')", (fid, str(path)))


class TestBackfillModels:
    def test_empty(self, db):
        assert backfill_models(db) == 0

    def test_parseable(self, db):
        db.execute("INSERT INTO models (id, raw_name, name) VALUES ('m1', 'claude-3-5-sonnet-20241022', 'claude-3-5-sonnet-20241022')")
        db.commit()
        assert backfill_models(db) == 1
        assert db.execute("SELECT creator FROM models WHERE id='m1'").fetchone()["creator"] == "anthropic"

    def test_unparseable(self, db):
        db.execute("INSERT INTO models (id, raw_name, name) VALUES ('m1', 'x', 'x')")
        db.commit()
        assert backfill_models(db) == 0

    def test_multiple(self, db):
        for i, n in enumerate(["gemini-2.0-flash", "claude-3-5-sonnet-20241022"]):
            db.execute("INSERT INTO models (id, raw_name, name) VALUES (?, ?, ?)", (f"m{i}", n, n))
        db.commit()
        assert backfill_models(db) == 2


class TestBackfillProviders:
    def test_no_source(self, db):
        db.execute("INSERT INTO harnesses (id, name) VALUES ('h1', 'test')")
        db.commit()
        assert backfill_providers(db) == 0

    def test_fills(self, db):
        _chain(db)
        db.commit()
        assert backfill_providers(db) == 1
        assert db.execute("SELECT provider_id FROM responses WHERE id='r1'").fetchone()["provider_id"] is not None

    def test_skips_set(self, db):
        _chain(db)
        db.execute("INSERT INTO providers (id, name) VALUES ('p1', 'anthropic')")
        db.execute("UPDATE responses SET provider_id='p1' WHERE id='r1'")
        db.commit()
        assert backfill_providers(db) == 0


class TestBackfillShellTags:
    def test_no_calls(self, db):
        assert backfill_shell_tags(db) == {}

    def test_tags_and_idempotent(self, db):
        _chain(db)
        tid = _tid(db)
        _tc(db, "tc1", "r1", "c1", tid, {"command": "git status"})
        _tc(db, "tc2", "r1", "c1", tid, {"command": "pytest tests/"})
        db.commit()
        first = backfill_shell_tags(db)
        assert sum(first.values()) >= 1
        assert sum(backfill_shell_tags(db).values()) == 0  # idempotent

    def test_empty_cmd(self, db):
        _chain(db)
        _tc(db, "tc1", "r1", "c1", _tid(db), {"command": ""})
        db.commit()
        assert sum(backfill_shell_tags(db).values()) == 0

    def test_raw_string_input(self, db):
        _chain(db)
        _tc(db, "tc1", "r1", "c1", _tid(db), "ls -la")
        db.commit()
        counts = backfill_shell_tags(db)  # exercises L127: cmd = raw_input or ""
        assert isinstance(counts, dict)

    def test_bare_db(self, tmp_path):
        conn = _bare_db(tmp_path)
        assert backfill_shell_tags(conn) == {}
        conn.close()


class TestBackfillResponseAttributes:
    def test_no_harness(self, db):
        db.execute("INSERT INTO harnesses (id, name, source) VALUES ('h1', 'aider', 'openai')")
        db.commit()
        assert backfill_response_attributes(db) == 0

    def test_missing_file(self, db):
        _chain(db)
        _ingest(db, "/nonexistent/file.jsonl")
        db.commit()
        assert backfill_response_attributes(db) == 0

    def test_cache_tokens(self, db, tmp_path):
        uuid_val = "msg-uuid-123"
        _chain(db, ext_id=f"claude_code::{uuid_val}")
        p = _jsonl(tmp_path, [{"type": "assistant", "uuid": uuid_val, "message": {
            "usage": {"cache_creation_input_tokens": 500, "cache_read_input_tokens": 1200}}}])
        _ingest(db, p)
        db.commit()
        assert backfill_response_attributes(db) == 2
        keys = {r["key"] for r in db.execute("SELECT key FROM response_attributes WHERE response_id='r1'").fetchall()}
        assert keys == {"cache_creation_input_tokens", "cache_read_input_tokens"}

    @pytest.mark.parametrize("record,desc", [
        ({"type": "human", "uuid": "x"}, "non-assistant"),
        ({"type": "assistant", "uuid": "x", "message": {"usage": {"input_tokens": 100}}}, "no cache"),
        ({"type": "assistant", "message": {"usage": {"cache_creation_input_tokens": 1}}}, "no uuid"),
        ({"type": "assistant", "uuid": "missing", "message": {"usage": {"cache_creation_input_tokens": 1}}}, "not in DB"),
    ], ids=["non_assistant", "no_cache", "no_uuid", "not_in_db"])
    def test_skips(self, db, tmp_path, record, desc):
        _chain(db)
        p = _jsonl(tmp_path, [record])
        _ingest(db, p)
        db.commit()
        assert backfill_response_attributes(db) == 0


class TestBackfillDerivativeTags:
    def test_no_calls(self, db):
        assert backfill_derivative_tags(db) == 0

    def test_tags_siftd_search(self, db):
        _chain(db)
        _tc(db, "tc1", "r1", "c1", _tid(db), {"command": "siftd search 'q'"})
        db.commit()
        assert backfill_derivative_tags(db) == 1

    def test_skips_non_derivative(self, db):
        _chain(db)
        _tc(db, "tc1", "r1", "c1", _tid(db), {"command": "git status"})
        db.commit()
        assert backfill_derivative_tags(db) == 0

    def test_null_input(self, db):
        _chain(db)
        db.execute("INSERT INTO tool_calls (id, response_id, conversation_id, tool_id, input) VALUES ('tc1', 'r1', 'c1', ?, NULL)", (_tid(db),))
        db.commit()
        assert backfill_derivative_tags(db) == 0

    def test_bare_db(self, tmp_path):
        conn = _bare_db(tmp_path)
        assert backfill_derivative_tags(conn) == 0
        conn.close()


class TestBackfillFilterBinary:
    def _img_blob(self):
        return json.dumps({"content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo"}}]})

    def test_empty(self, db):
        s = backfill_filter_binary(db)
        assert s["filtered"] == 0 and s["skipped"] == 0

    def test_dry_run(self, db):
        db.execute("INSERT INTO content_blobs (hash, content, ref_count, created_at) VALUES ('h1', ?, 1, '2024-01-01')", (json.dumps({"type": "base64"}),))
        db.commit()
        backfill_filter_binary(db, dry_run=True)
        assert db.execute("SELECT 1 FROM content_blobs WHERE hash='h1'").fetchone()

    def test_filters_with_tool_calls(self, db):
        from siftd.storage.blobs import compute_content_hash
        content = self._img_blob()
        h = compute_content_hash(content)
        db.execute("INSERT INTO content_blobs (hash, content, ref_count, created_at) VALUES (?, ?, 1, '2024-01-01')", (h, content))
        _chain(db)
        db.execute("INSERT INTO tool_calls (id, response_id, conversation_id, tool_id, result_hash) VALUES ('tc1', 'r1', 'c1', ?, ?)", (_tid(db), h))
        db.commit()
        assert backfill_filter_binary(db)["filtered"] == 1

    def test_non_dict_error(self, db):
        db.execute("INSERT INTO content_blobs (hash, content, ref_count, created_at) VALUES ('h1', ?, 1, '2024-01-01')", ('"iVBORw0KGgo"',))
        db.commit()
        assert backfill_filter_binary(db)["errors"] == 1

    def test_unchanged_skip(self, db):
        from siftd.storage.blobs import compute_content_hash
        content = json.dumps({"type": "base64", "note": "clean"})
        db.execute("INSERT INTO content_blobs (hash, content, ref_count, created_at) VALUES (?, ?, 1, '2024-01-01')", (compute_content_hash(content), content))
        db.commit()
        assert backfill_filter_binary(db)["skipped"] == 1

    def test_zero_refcount(self, db):
        from siftd.storage.blobs import compute_content_hash
        content = self._img_blob()
        db.execute("INSERT INTO content_blobs (hash, content, ref_count, created_at) VALUES (?, ?, 1, '2024-01-01')", (compute_content_hash(content), content))
        db.commit()
        assert backfill_filter_binary(db)["filtered"] == 1

    def test_reencoded_but_same_hash_skips(self, db, monkeypatch):
        from siftd.storage.blobs import compute_content_hash

        content = json.dumps({"type": "base64", "data": "iVBORw0KGgo"})
        h = compute_content_hash(content)
        db.execute("INSERT INTO content_blobs (hash, content, ref_count, created_at) VALUES (?, ?, 1, '2024-01-01')", (h, content))
        db.commit()

        monkeypatch.setattr("siftd.content.filters.filter_tool_result_binary", lambda d: dict(d))
        monkeypatch.setattr("siftd.storage.blobs.compute_content_hash", lambda _s: h)
        stats = backfill_filter_binary(db)
        assert stats["skipped"] == 1 and stats["filtered"] == 0
