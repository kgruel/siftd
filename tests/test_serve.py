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

    def test_push_non_sqlite_returns_400_invalid_source(self, tmp_path):
        """I1: a non-SQLite body is a client error (400), not an opaque 500."""
        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/push",
                content=b"definitely not a sqlite database payload",
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 400
        assert resp.json()["error_type"] == "invalid_source"

    def test_push_schema_mismatch_returns_409(self, tmp_path):
        """I1: a version-mismatched slice is a distinguishable 409, not a 500."""
        import sqlite3

        slice_bytes = _make_slice_bytes(tmp_path, external_id="c1")
        bumped = tmp_path / "bumped.db"
        bumped.write_bytes(slice_bytes)
        c = sqlite3.connect(str(bumped))
        c.execute("PRAGMA user_version = 9999")
        c.commit()
        c.close()

        team_db = tmp_path / "team.db"
        create_database(team_db)  # target at the current schema version
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/push",
                content=bumped.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
        assert resp.status_code == 409
        assert resp.json()["error_type"] == "schema_mismatch"


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

    def test_search_config_error_maps_to_503(self, tmp_path, monkeypatch):
        """A configured remote backend that's unusable (e.g. a revoked key raises
        EmbeddingConfigError) maps to a structured 503 with an honest message, not a
        generic 500 (F5')."""
        from siftd.embeddings.base import EmbeddingConfigError

        team_db = _make_team_db(
            tmp_path / "team.db",
            conversations=[{"external_id": "c1"}],
        )

        def _boom(*a, **k):
            raise EmbeddingConfigError(
                "remote:openai: authentication failed (HTTP 401); check embed.api_key"
            )

        monkeypatch.setattr("siftd.api.search.search_view", _boom)
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/search", params={"q": "hello"})
        assert resp.status_code == 503
        assert "authentication failed" in resp.json()["error"]

    @pytest.mark.parametrize(
        "make_exc,expected_status",
        [
            ("EmbeddingsNotAvailable", 501),
            ("EmbeddingConfigError", 503),
            ("IncrementalCompatError", 503),
            ("IndexCompatError", 503),
            ("SchemaUpgradeRequiredError", 503),
            ("UserInputError", 400),
            ("DirectSiftdError", 500),
        ],
    )
    def test_dispatch_maps_taxonomy_exceptions_to_http_status(
        self, tmp_path, monkeypatch, make_exc, expected_status
    ):
        """`_dispatch`'s `except SiftdError: ... e.http_status` mapping, exercised per
        branch/override — not just the two class-name-string cases the old code special
        cased. UserInputError and a bare-SiftdError subclass pin the 400/500 defaults
        (the latter is what keeps a slice-4 direct-root joiner at today's generic-500
        wire behavior instead of AttributeError-ing)."""
        team_db = _make_team_db(tmp_path / "team.db", conversations=[{"external_id": "c1"}])

        def _build_exc():
            if make_exc == "EmbeddingsNotAvailable":
                from siftd.embeddings.availability import EmbeddingsNotAvailable

                return EmbeddingsNotAvailable("Search")
            if make_exc == "EmbeddingConfigError":
                from siftd.embeddings.base import EmbeddingConfigError

                return EmbeddingConfigError("bad config")
            if make_exc == "IncrementalCompatError":
                from siftd.embeddings.indexer import IncrementalCompatError

                return IncrementalCompatError("stale index")
            if make_exc == "IndexCompatError":
                from siftd.storage.embeddings import IndexCompatError

                return IndexCompatError("index drift")
            if make_exc == "SchemaUpgradeRequiredError":
                from siftd.storage.sqlite import SchemaUpgradeRequiredError

                return SchemaUpgradeRequiredError("schema stale")
            if make_exc == "UserInputError":
                from siftd.errors import UserInputError

                return UserInputError("bad input")
            if make_exc == "DirectSiftdError":
                from siftd.errors import SiftdError

                class _DirectJoiner(SiftdError):
                    """Pins the root's http_status default for a direct joiner."""

                return _DirectJoiner("boom")
            raise AssertionError(make_exc)

        exc = _build_exc()

        def _boom(*a, **k):
            raise exc

        monkeypatch.setattr("siftd.api.search.search_view", _boom)
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/search", params={"q": "hello"})
        assert resp.status_code == expected_status
        if make_exc == "SchemaUpgradeRequiredError":
            # Privacy: its message embeds the server's absolute DB path, so
            # _dispatch returns a generic body and logs the real message.
            assert resp.json()["error"] == "server database schema requires upgrade"
            assert str(exc) not in resp.json()["error"]
        else:
            assert str(exc) in resp.json()["error"]


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
        MW = next(m for m in app.middleware if hasattr(m, "_cache_key"))
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


class TestSecurityHeaders:
    """F3: every response carries CSP + hardening headers; assets are vendored."""

    def test_security_headers_present(self, tmp_path):
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/")
            assert r.status_code == 200
            csp = r.headers.get("content-security-policy", "")
            assert "connect-src 'self'" in csp        # blocks off-origin token exfil
            assert "frame-ancestors 'none'" in csp
            assert "unpkg.com" not in csp              # no external script origin
            assert r.headers.get("x-content-type-options") == "nosniff"
            assert r.headers.get("x-frame-options") == "DENY"
            assert r.headers.get("referrer-policy") == "no-referrer"

    def test_csp_widens_connect_src_to_oidc_issuer(self, tmp_path):
        # auth.js does the code->token exchange via fetch() to the issuer's token
        # endpoint, which connect-src governs. A configured issuer must be
        # allowlisted or client-side SSO login silently breaks.
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        auth_config = {"issuer": "https://idp.example.com/realms/x"}
        app = create_app(db_path=team_db, auth_config=auth_config)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/")
            csp = r.headers.get("content-security-policy", "")
            assert "connect-src 'self' https://idp.example.com" in csp

    def test_build_csp_widening_logic(self):
        # Unit-level cover for the issuer-widening logic itself (process-state
        # independent). Pairs with the create_app wiring test above.
        from siftd.serve.app import _build_csp

        assert "connect-src 'self';" in _build_csp(None)
        assert "connect-src 'self';" in _build_csp({})
        widened = _build_csp({"issuer": "https://idp.example.com/realms/x"})
        assert "connect-src 'self' https://idp.example.com;" in widened
        # A bare/origin-less issuer falls back to 'self' rather than emitting junk.
        assert "connect-src 'self';" in _build_csp({"issuer": "not-a-url"})

    def test_csp_does_not_bleed_across_apps_in_one_process(self, tmp_path):
        # Litestar memoizes route-handler resolution process-wide: with headers
        # attached via an after_request hook, once a no-auth app had served
        # GET /, every later create_app() instance's HTTP responses inherited
        # the FIRST app's CSP (narrow connect-src), silently dropping the OIDC
        # issuer allowance. Headers now ride per-app ASGI middleware, which is
        # resolved per-app. This test performs the poisoning sequence
        # explicitly so the regression can't hide behind collection order.
        team_db = tmp_path / "team.db"
        create_database(team_db).close()

        # Step 1: a no-auth app serves the path first (the poisoning step).
        app_first = create_app(db_path=team_db, auth_config=None)
        with TestClient(app_first, raise_server_exceptions=False) as client:
            csp = client.get("/").headers.get("content-security-policy", "")
            assert "connect-src 'self';" in csp

        # Step 2: a later issuer-configured app must still widen connect-src.
        auth_config = {"issuer": "https://idp.example.com/realms/x"}
        app_second = create_app(db_path=team_db, auth_config=auth_config)
        with TestClient(app_second, raise_server_exceptions=False) as client:
            csp = client.get("/").headers.get("content-security-policy", "")
            assert "connect-src 'self' https://idp.example.com" in csp

    def test_shell_references_vendored_assets_not_cdn(self, tmp_path):
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            html = client.get("/").text
            assert "/static/vendor/htmx.min.js" in html
            assert "unpkg.com/htmx" not in html
            assert "unpkg.com/prismjs" not in html

    def test_vendored_assets_served(self, tmp_path):
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        app = create_app(db_path=team_db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/static/vendor/htmx.min.js").status_code == 200
            assert client.get("/static/vendor/prism/prism-core.min.js").status_code == 200


class TestRateLimitAndLiveGate:
    """F4 (per-client rate limit) + F7 (live-endpoint gate)."""

    def test_live_endpoints_gated_off(self, tmp_path):
        """F7: with live endpoints off, /follow is unregistered AND the
        Sessions view renders no Live zone — the server host's session files
        are never shown to remote users."""
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        app = create_app(db_path=team_db, auth_config=None, allow_live_endpoints=False)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/follow?sid=abc").status_code == 404
            sessions = client.get("/view/sessions")
            assert sessions.status_code == 200
            assert "zone--live" not in sessions.text
            assert 'hx-get="/follow' not in sessions.text
            # The shell's ?follow= deep link degrades to the Sessions view,
            # never pointing #main at the unregistered route.
            shell = client.get("/", params={"follow": "abc"}).text
            assert "/view/sessions" in shell and "/follow?sid=" not in shell

    def test_live_endpoints_present_when_allowed(self, tmp_path):
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        app = create_app(db_path=team_db, auth_config=None, allow_live_endpoints=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            # /view/sessions is an htmx-only fragment (a direct GET 303s to the
            # canonical shell); fetch it as htmx to assert the fragment itself.
            client.headers.update({"HX-Request": "true"})
            sessions = client.get("/view/sessions")
            assert sessions.status_code == 200
            assert "zone--live" in sessions.text  # zone present (may be empty)
            shell = client.get("/", params={"follow": "abc"}).text
            assert "/follow?sid=abc" in shell

    def test_rate_limit_returns_429_when_exceeded(self, tmp_path):
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        # Tiny limit to exercise the limiter deterministically.
        app = create_app(db_path=team_db, auth_config=None, rate_limit_per_minute=3)
        with TestClient(app, raise_server_exceptions=False) as client:
            codes = [client.get("/").status_code for _ in range(8)]
            assert 429 in codes
            # health is exempt — never throttled
            assert client.get("/api/v1/health").status_code == 200

    def test_rate_limit_disabled_when_zero(self, tmp_path):
        team_db = tmp_path / "team.db"
        create_database(team_db).close()
        app = create_app(db_path=team_db, auth_config=None, rate_limit_per_minute=0)
        with TestClient(app, raise_server_exceptions=False) as client:
            codes = [client.get("/").status_code for _ in range(10)]
            assert 429 not in codes


class TestClientIpProvenance:
    """F8b: push_log records the real client IP, honoring XFF only from trusted proxies."""

    def test_push_log_uses_xff_only_from_trusted_proxy(self, tmp_path, monkeypatch):
        # Configure the TestClient peer ("testclient") as a trusted proxy, so the
        # forwarded client IP is recorded instead of the proxy address.
        import siftd.config as cfg

        monkeypatch.setattr(
            cfg, "get_config",
            lambda k: "testclient" if k == "serve.trusted_proxies" else None,
        )

        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None, rate_limit_per_minute=0)
        slice_a = _make_slice_bytes(tmp_path, external_id="c-xff")

        from siftd.storage.sqlite import open_database

        with TestClient(app, raise_server_exceptions=False) as client:
            client.post(
                "/api/v1/push", content=slice_a,
                headers={"Content-Type": "application/octet-stream",
                         "X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
            )

        conn = open_database(team_db, read_only=True)
        try:
            ip = conn.execute("SELECT source_ip FROM push_log").fetchone()[0]
        finally:
            conn.close()
        assert ip == "203.0.113.7"  # left-most XFF entry, trusted proxy honored

    def test_push_log_ignores_xff_without_trusted_proxy(self, tmp_path, monkeypatch):
        import siftd.config as cfg

        monkeypatch.setattr(cfg, "get_config", lambda k: None)  # no trusted proxies

        team_db = tmp_path / "team.db"
        app = create_app(db_path=team_db, auth_config=None, rate_limit_per_minute=0)
        slice_a = _make_slice_bytes(tmp_path, external_id="c-noxff")

        from siftd.storage.sqlite import open_database

        with TestClient(app, raise_server_exceptions=False) as client:
            client.post(
                "/api/v1/push", content=slice_a,
                headers={"Content-Type": "application/octet-stream",
                         "X-Forwarded-For": "203.0.113.7"},
            )

        conn = open_database(team_db, read_only=True)
        try:
            ip = conn.execute("SELECT source_ip FROM push_log").fetchone()[0]
        finally:
            conn.close()
        assert ip != "203.0.113.7"  # spoofed XFF must NOT be trusted


class TestAuditLog:
    """F6: state-changing tag mutations write an attributable audit_log row."""

    def test_tag_mutation_writes_audit_row(self, tmp_path):
        import time

        team_db = tmp_path / "team.db"
        auth_config = {"introspection_url": "https://idp/i", "identity_claim": "username"}
        app = create_app(db_path=team_db, auth_config=auth_config, rate_limit_per_minute=0)
        # Seed the introspection cache so "tokA" resolves to alice without a
        # network round-trip.
        mw = next(m for m in app.middleware if hasattr(m, "_cache_key"))
        mw._introspection_cache = {
            mw._cache_key("tokA"): ({"username": "alice"}, time.time() + 3600),
        }

        from siftd.storage.sqlite import open_database

        slice_a = _make_slice_bytes(tmp_path, external_id="alice-1")
        with TestClient(app, raise_server_exceptions=False) as client:
            client.post(
                "/api/v1/push", content=slice_a,
                headers={"Authorization": "Bearer tokA",
                         "Content-Type": "application/octet-stream"},
            )
            r = client.post(
                "/api/v1/tag",
                content=__import__("json").dumps(
                    {"action": "apply", "tags": ["reviewed"], "last": 1}
                ).encode(),
                headers={"Authorization": "Bearer tokA",
                         "Content-Type": "application/json"},
            )
            assert r.status_code == 200

        conn = open_database(team_db, read_only=True)
        try:
            rows = conn.execute(
                "SELECT actor, action, detail FROM audit_log WHERE action = 'tag.apply'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) >= 1
        assert rows[0]["actor"] == "alice"
        assert "reviewed" in (rows[0]["detail"] or "")


class TestErrorHygiene:
    """F8a: error bodies never leak the absolute server DB path."""

    def test_missing_db_error_does_not_leak_path(self, tmp_path):
        missing = tmp_path / "nope" / "team.db"  # does not exist
        app = create_app(db_path=missing, auth_config=None, rate_limit_per_minute=0)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/v1/conversations/abc123")
            assert r.status_code == 404
            body = str(r.json())
            assert str(missing) not in body          # no absolute path leaked
            assert "nope" not in body

    def test_session_queue_missing_db_no_path_leak(self, tmp_path):
        import json
        missing = tmp_path / "nope" / "team.db"
        app = create_app(db_path=missing, auth_config=None, rate_limit_per_minute=0)
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post(
                "/api/v1/sessions/sid1/tags",
                content=json.dumps({"tags": ["x"]}).encode(),
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 404
            assert str(missing) not in str(r.json())


class TestPublicBindAuthGuard:
    """F2: refuse to bind a non-loopback address with auth disabled (fail closed)."""

    @staticmethod
    def _args(tmp_path, **over):
        from types import SimpleNamespace

        base = dict(db=str(tmp_path / "team.db"), host="0.0.0.0", port=None,
                    no_auth=False, unsafe_public_no_auth=False)
        base.update(over)
        return SimpleNamespace(**base)

    def _patch_config(self, monkeypatch, *, auth_table):
        import siftd.config as cfg

        monkeypatch.setattr(cfg, "get_config", lambda _k=None: None)
        monkeypatch.setattr(cfg, "get_config_table", lambda _k: auth_table)

    def test_public_bind_no_auth_refused(self, monkeypatch, tmp_path):
        from siftd.cli.serve import cmd_serve

        self._patch_config(monkeypatch, auth_table=None)
        called = {"uvicorn": False}
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.__setitem__("uvicorn", True))

        rc = cmd_serve(self._args(tmp_path))
        assert rc == 2
        assert called["uvicorn"] is False  # never bound

    def test_public_bind_no_auth_with_override_proceeds(self, monkeypatch, tmp_path):
        from siftd.cli.serve import cmd_serve

        self._patch_config(monkeypatch, auth_table=None)
        import siftd.paths
        monkeypatch.setattr(siftd.paths, "state_dir", lambda: tmp_path)

        reached = {"uvicorn": False}
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: reached.__setitem__("uvicorn", True))

        rc = cmd_serve(self._args(tmp_path, unsafe_public_no_auth=True))
        assert rc == 0
        assert reached["uvicorn"] is True  # guard passed, server bound

    def test_public_bind_with_auth_proceeds(self, monkeypatch, tmp_path):
        from siftd.cli.serve import cmd_serve

        self._patch_config(monkeypatch, auth_table={"static_token": "x" * 32})
        import siftd.paths
        monkeypatch.setattr(siftd.paths, "state_dir", lambda: tmp_path)

        reached = {"uvicorn": False}
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: reached.__setitem__("uvicorn", True))

        rc = cmd_serve(self._args(tmp_path))
        assert rc == 0
        assert reached["uvicorn"] is True

    def test_loopback_no_auth_allowed(self, monkeypatch, tmp_path):
        from siftd.cli.serve import cmd_serve

        self._patch_config(monkeypatch, auth_table=None)
        import siftd.paths
        monkeypatch.setattr(siftd.paths, "state_dir", lambda: tmp_path)

        reached = {"uvicorn": False}
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: reached.__setitem__("uvicorn", True))

        rc = cmd_serve(self._args(tmp_path, host="127.0.0.1"))
        assert rc == 0
        assert reached["uvicorn"] is True  # loopback never triggers the guard


class TestServeStartupDbCreate:
    """F9: server pre-creates the team DB so the first push merges, not adopts."""

    def test_cmd_serve_creates_db_at_startup(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        import siftd.config as cfg
        import siftd.paths
        from siftd.cli.serve import cmd_serve

        monkeypatch.setattr(cfg, "get_config", lambda _k=None: None)
        monkeypatch.setattr(cfg, "get_config_table", lambda _k: None)
        monkeypatch.setattr(siftd.paths, "state_dir", lambda: tmp_path)
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

        db = tmp_path / "fresh" / "team.db"
        assert not db.exists()
        args = SimpleNamespace(db=str(db), host="127.0.0.1", port=None,
                               no_auth=True, unsafe_public_no_auth=False)
        rc = cmd_serve(args)
        assert rc == 0
        assert db.exists()  # F9: server created the schema before serving

        # And it's a real schema DB the merge path can use (not an empty file).
        from siftd.storage.sql_helpers import has_conversation_owners_table
        from siftd.storage.sqlite import open_database
        conn = open_database(db, read_only=True)
        try:
            assert has_conversation_owners_table(conn)
        finally:
            conn.close()


class TestPushOwnedCount:
    """Push response surfaces the server-stamped ownership count (`owned`)."""

    def test_authenticated_push_response_carries_owned(self, tmp_path):
        import time

        team_db = tmp_path / "team.db"
        auth_config = {"introspection_url": "https://idp/i", "identity_claim": "username"}
        app = create_app(db_path=team_db, auth_config=auth_config, rate_limit_per_minute=0)
        mw = next(m for m in app.middleware if hasattr(m, "_cache_key"))
        mw._introspection_cache = {
            mw._cache_key("tokA"): ({"username": "alice"}, time.time() + 3600),
        }
        headers = {"Authorization": "Bearer tokA",
                   "Content-Type": "application/octet-stream"}

        from siftd.storage.sqlite import open_database

        with TestClient(app, raise_server_exceptions=False) as client:
            # First push creates the DB: every received conversation is stamped.
            r1 = client.post(
                "/api/v1/push",
                content=_make_slice_bytes(tmp_path, external_id="own-1"),
                headers=headers,
            )
            assert r1.status_code == 201
            body1 = r1.json()
            assert body1["owned"] == body1["conversations"] == 1

            # Second push merges: only the new conversation is stamped.
            second_dir = tmp_path / "second"
            second_dir.mkdir()
            r2 = client.post(
                "/api/v1/push",
                content=_make_slice_bytes(second_dir, external_id="own-2"),
                headers=headers,
            )
            assert r2.status_code == 200
            body2 = r2.json()
            assert body2["owned"] == body2["conversations"] == 1

        # The reported counts match the actual ownership rows.
        conn = open_database(team_db, read_only=True)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM conversation_owners WHERE user_id = 'alice'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 2
