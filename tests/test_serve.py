"""Tests for siftd serve — HTTP team sync server."""

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.testing import TestClient

from siftd.serve.app import create_app
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)


def _make_team_db(path, *, conversations=None):
    """Build a test database with optional conversations."""
    conn = create_database(path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "gpt-4")
    p = get_or_create_provider(conn, "openai")

    for conv in conversations or []:
        started = conv.get("started_at", "2024-01-15T10:00:00Z")
        cid = insert_conversation(
            conn, external_id=conv["external_id"], harness_id=h,
            workspace_id=w, started_at=started,
        )
        pid = insert_prompt(conn, cid, f"p-{conv['external_id']}", started)
        insert_prompt_content(conn, pid, 0, "text", '{"text": "hello"}')
        rid = insert_response(
            conn, cid, pid, m, p, f"r-{conv['external_id']}", started,
            input_tokens=10, output_tokens=5,
        )
        insert_response_content(conn, rid, 0, "text", '{"text": "hi"}')

    conn.commit()
    conn.close()
    return path


def _make_slice_bytes(tmp_path, *, external_id="c1"):
    """Build a source DB, slice it, and return slice bytes."""
    from siftd.api.slice import slice_database

    source = _make_team_db(
        tmp_path / "source.db",
        conversations=[{"external_id": external_id}],
    )
    slice_path = tmp_path / "slice.db"
    slice_database(source, slice_path, rebuild_fts=False)
    return slice_path.read_bytes()


class TestHealth:
    def test_health_returns_ok(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "db_size_bytes" in body
        assert "conversations" in body


class TestPush:
    def test_push_creates_db(self, tmp_path):
        slice_bytes = _make_slice_bytes(tmp_path)
        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/push",
                content=slice_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "created"
        assert body["conversations"] >= 1
        assert team_db.exists()

    def test_push_merges_into_existing(self, tmp_path):
        slice_bytes = _make_slice_bytes(tmp_path, external_id="c1")
        team_db = tmp_path / "team.db"
        create_database(team_db)
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.post(
                "/v1/push",
                content=slice_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "merged"
