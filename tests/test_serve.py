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
        import hashlib

        db = tmp_path / "team.db"
        create_database(db)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "siftd"
        assert body["status"] == "ok"
        assert body["db_id"] == hashlib.sha256(str(db.resolve()).encode("utf-8")).hexdigest()
        assert "db_path" not in body
        assert "db_size_bytes" in body
        assert "conversations" in body


class TestPush:
    def test_push_creates_db(self, tmp_path):
        slice_bytes = _make_slice_bytes(tmp_path)
        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/push",
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
                "/api/v1/push",
                content=slice_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "merged"


class TestPull:
    def test_pull_streams_slice(self, tmp_path):
        """Push data in, then pull it back out."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/pull")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/octet-stream"
        assert int(resp.headers["X-Siftd-Conversations"]) >= 1
        # Response body should be valid SQLite
        assert resp.content[:16].startswith(b"SQLite format 3")

    def test_pull_empty_db(self, tmp_path):
        team_db = tmp_path / "team.db"
        create_database(team_db)
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/pull")
        assert resp.status_code == 200
        assert int(resp.headers.get("X-Siftd-Conversations", 0)) == 0


class TestQuery:
    def test_query_lists_conversations(self, tmp_path):
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}, {"external_id": "c2"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/conversations")
        assert resp.status_code == 200
        body = resp.json()
        assert "conversations" in body
        assert len(body["conversations"]) >= 2

    def test_query_with_limit(self, tmp_path):
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}, {"external_id": "c2"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/conversations", params={"n": 1})
        body = resp.json()
        assert len(body["conversations"]) == 1

    def test_query_single_conversation(self, tmp_path):
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            # First get the list to find the ID
            resp = client.get("/api/v1/conversations")
            conv_id = resp.json()["conversations"][0]["id"]
            # Then get the detail
            resp = client.get(f"/api/v1/conversations/{conv_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert "conversation" in body
        assert body["conversation"]["id"] == conv_id


class TestStats:
    def test_stats_returns_counts(self, tmp_path):
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}, {"external_id": "c2"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"]["conversations"] == 2
        assert body["counts"]["prompts"] == 2
        assert body["counts"]["responses"] == 2
        assert "models" in body
        assert "top_workspaces" in body
        assert "token_coverage" in body

    def test_stats_on_empty_db(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"]["conversations"] == 0


class TestSearch:
    def test_search_without_embeddings_returns_501(self, tmp_path):
        """Search endpoint returns 501 when embeddings not installed."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/search", params={"q": "hello"})
        # Either 200 (if embeddings available) or 501 (if not)
        assert resp.status_code in (200, 501)


class TestAuthNoAuth:
    def test_no_auth_allows_all(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
            assert resp.status_code == 200


class TestAuthOIDC:
    def test_missing_token_returns_401(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"issuer": "https://example.com", "audience": "siftd"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/push",
                content=b"x" * 100,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp.status_code == 401

    def test_health_bypasses_auth(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"issuer": "https://example.com", "audience": "siftd"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
            assert resp.status_code == 200


class TestOwnershipEnforcement:
    def test_owner_scoping_across_read_and_write_paths(self, tmp_path):
        import json
        import time

        team_db = tmp_path / "team.db"
        auth_config = {"introspection_url": "https://idp/introspect", "identity_claim": "username"}
        app = create_app(db_path=team_db, auth_config=auth_config)

        # Pre-populate introspection cache to avoid network calls and simulate two users.
        # Cache entries use absolute deadline (time.time() + TTL), not cached_at timestamp.
        MW = app.middleware[0]
        MW._introspection_cache = {
            MW._cache_key("tokA"): ({"username": "alice"}, time.time() + 3600),
            MW._cache_key("tokB"): ({"username": "bob"}, time.time() + 3600),
        }

        slice_a = _make_slice_bytes(tmp_path, external_id="alice-1")
        slice_b = _make_slice_bytes(tmp_path, external_id="bob-1")

        with TestClient(app, raise_server_exceptions=False) as client:
            # Push as alice and bob.
            r1 = client.post(
                "/api/v1/push",
                content=slice_a,
                headers={"Authorization": "Bearer tokA", "Content-Type": "application/octet-stream"},
            )
            assert r1.status_code in (200, 201)
            r2 = client.post(
                "/api/v1/push",
                content=slice_b,
                headers={"Authorization": "Bearer tokB", "Content-Type": "application/octet-stream"},
            )
            assert r2.status_code in (200, 201)

            # Each user sees only their own conversations.
            alice_list = client.get("/api/v1/conversations", headers={"Authorization": "Bearer tokA"})
            bob_list = client.get("/api/v1/conversations", headers={"Authorization": "Bearer tokB"})
            assert alice_list.status_code == 200 and bob_list.status_code == 200
            alice_convs = alice_list.json()["conversations"]
            bob_convs = bob_list.json()["conversations"]
            assert len(alice_convs) == 1
            assert len(bob_convs) == 1
            alice_id = alice_convs[0]["id"]
            bob_id = bob_convs[0]["id"]

            # Detail is owner-scoped (404 on cross-tenant).
            forbidden = client.get(f"/api/v1/conversations/{bob_id}", headers={"Authorization": "Bearer tokA"})
            assert forbidden.status_code == 404

            # Export-by-id is owner-scoped (empty for cross-tenant).
            ex = client.get(
                "/api/v1/export",
                params={"id": alice_id},
                headers={"Authorization": "Bearer tokB"},
            )
            assert ex.status_code == 200
            assert ex.json()["conversations"] == []

            # Tag mutations are scoped to owned conversations.
            # Cross-owner tagging is blocked — entity_id not visible to alice,
            # so the route returns 404 (no matching entities found).
            tag_other = client.post(
                "/api/v1/tag",
                content=json.dumps({"action": "apply", "tags": ["t"], "entity_id": bob_id}).encode(),
                headers={"Authorization": "Bearer tokA", "Content-Type": "application/json"},
            )
            assert tag_other.status_code in (200, 404)
            body_other = tag_other.json()
            assert body_other.get("error") == "no matching entities found" or tag_other.status_code == 404

            tag_last = client.post(
                "/api/v1/tag",
                content=json.dumps({"action": "apply", "tags": ["t"], "last": 1}).encode(),
                headers={"Authorization": "Bearer tokA", "Content-Type": "application/json"},
            )
            assert tag_last.status_code == 200

            alice_detail = client.get(f"/api/v1/conversations/{alice_id}", headers={"Authorization": "Bearer tokA"})
            bob_detail = client.get(f"/api/v1/conversations/{bob_id}", headers={"Authorization": "Bearer tokB"})
            assert alice_detail.status_code == 200 and bob_detail.status_code == 200
            assert "t" in (alice_detail.json()["conversation"].get("tags") or [])
            assert "t" not in (bob_detail.json()["conversation"].get("tags") or [])

            # Aggregate endpoints are scoped when auth is enabled.
            alice_stats = client.get("/api/v1/stats", headers={"Authorization": "Bearer tokA"}).json()
            bob_stats = client.get("/api/v1/stats", headers={"Authorization": "Bearer tokB"}).json()
            assert alice_stats["counts"]["conversations"] == 1
            assert bob_stats["counts"]["conversations"] == 1


class TestAttribution:
    def test_push_records_push_log(self, tmp_path):
        """Push records an entry in push_log table."""
        import sqlite3

        slice_bytes = _make_slice_bytes(tmp_path)
        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/push",
                content=slice_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 201

        conn = sqlite3.connect(str(team_db))
        rows = conn.execute("SELECT * FROM push_log").fetchall()
        conn.close()
        assert len(rows) == 1

    def test_push_log_records_identity(self, tmp_path):
        """Push log captures the identity from request."""
        import sqlite3

        slice_bytes = _make_slice_bytes(tmp_path)
        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/push",
                content=slice_bytes,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Siftd-Identity": "alice",
                },
            )
        assert resp.status_code == 201

        conn = sqlite3.connect(str(team_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM push_log").fetchone()
        conn.close()
        assert row["user_identity"] == "alice"


class TestPullDryRun:
    """bug_008: dry_run estimated_size must be proportional, not the full DB size."""

    def test_dry_run_proportional_estimated_size(self, tmp_path):
        """Filtered dry_run returns a size proportional to filtered conv count."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[
                {"external_id": "c1", "started_at": "2024-01-15T10:00:00Z"},
                {"external_id": "c2", "started_at": "2024-02-15T10:00:00Z"},
                {"external_id": "c3", "started_at": "2024-03-15T10:00:00Z"},
            ],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            full_resp = client.get("/api/v1/pull", params={"dry_run": 1})
            filtered_resp = client.get(
                "/api/v1/pull",
                params={"dry_run": 1, "since": "2024-03-01T00:00:00Z"},
            )
        assert full_resp.status_code == 200
        assert filtered_resp.status_code == 200

        full_size = int(full_resp.headers["X-Siftd-Estimated-Size"])
        filtered_size = int(filtered_resp.headers["X-Siftd-Estimated-Size"])
        filtered_count = int(filtered_resp.headers["X-Siftd-Conversations"])

        assert filtered_count == 1
        # Proportional: 1/3 of total, so filtered_size < full_size
        assert filtered_size < full_size, (
            f"Filtered size {filtered_size} should be less than full size {full_size}"
        )

    def test_dry_run_zero_conversations_returns_zero_size(self, tmp_path):
        """Empty DB: proportional estimate is 0 with no division error."""
        team_db = tmp_path / "team.db"
        create_database(team_db)
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/pull", params={"dry_run": 1})
        assert resp.status_code == 200
        assert int(resp.headers["X-Siftd-Estimated-Size"]) == 0
        assert int(resp.headers["X-Siftd-Conversations"]) == 0

    def test_dry_run_does_not_call_slice_database(self, tmp_path, monkeypatch):
        """Y3 contract: dry_run never calls slice_database."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        called = []

        def fake_slice(*args, **kwargs):
            called.append(True)

        monkeypatch.setattr("siftd.api.slice.slice_database", fake_slice)
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/pull", params={"dry_run": 1})
        assert resp.status_code == 200
        assert not called, "dry_run must not call slice_database"


class TestSearchParams:
    """merged_bug_006: raw_fts and debug_ids must be accepted by the search route."""

    def test_search_accepts_raw_fts_param(self, tmp_path):
        """search_route accepts raw_fts without 422 Unprocessable Entity."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/search", params={"q": "hello", "raw_fts": "true"})
        assert resp.status_code != 422, "raw_fts param should not be rejected by Litestar"

    def test_search_accepts_debug_ids_param(self, tmp_path):
        """search_route accepts debug_ids without 422 Unprocessable Entity."""
        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/search", params={"q": "hello", "debug_ids": "true"})
        assert resp.status_code != 422, "debug_ids param should not be rejected by Litestar"


class TestCLI:
    def test_serve_help(self):
        """siftd serve --help exits cleanly."""
        from siftd.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["serve", "--help"])
        assert exc.value.code == 0
