"""E2E smoke tests for the serve HTTP path.

Unit tests in test_serve_delegation_wire.py call route handlers directly
via `.fn()`, which bypasses Litestar's URL parsing, query coercion, and
default-value resolution. These tests fire requests through TestClient so
the full wire-format contract is exercised end-to-end:

  - urlencoded query string produced by the delegation client
  - Litestar's Parameter coercion (str/int/bool/list)
  - default values when params are absent
  - 400/401 status mapping from raised exceptions

The goal is to catch silent drift between what the CLI sends on the wire
and what the routes actually accept. The unit tests catch logic bugs;
these catch contract bugs.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

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


# ---------------------------------------------------------------------------
# Fixture: multi-turn conversation
# ---------------------------------------------------------------------------


def _make_multi_turn_db(path: Path, *, n_turns: int = 6) -> tuple[Path, str]:
    """Build a DB with one conversation containing ``n_turns`` distinct turns.

    Each turn has a unique prompt text including its index, so `--around <phrase>`
    can target a specific turn deterministically.

    Returns (db_path, external_id_of_conversation).
    """
    conn = create_database(path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "gpt-4")
    p = get_or_create_provider(conn, "openai")

    cid = insert_conversation(
        conn, external_id="c-multi", harness_id=h,
        workspace_id=w, started_at="2024-01-15T10:00:00Z",
    )
    for i in range(n_turns):
        ts = f"2024-01-15T10:{i:02d}:00Z"
        pid = insert_prompt(conn, cid, f"p-{i}", ts)
        insert_prompt_content(
            conn, pid, 0, "text",
            f'{{"text": "turn-{i}-unique-marker-{["alpha","beta","gamma","delta","epsilon","zeta"][i % 6]}"}}',
        )
        rid = insert_response(
            conn, cid, pid, m, p, f"r-{i}", ts,
            input_tokens=10, output_tokens=5,
        )
        insert_response_content(conn, rid, 0, "text", f'{{"text": "response-{i}"}}')

    # Build the FTS5 index so `anchor=around <phrase>` can actually find matches.
    # Real ingest pipelines do this implicitly; the bare _make_*_db helpers don't.
    from siftd.api.search import rebuild_fts_index
    rebuild_fts_index(conn)

    conn.commit()
    conn.close()
    return path, "c-multi"


def _resolve_conv_id(client: TestClient) -> str:
    """List conversations and return the first ID — auth-off mode helper."""
    resp = client.get("/api/v1/conversations")
    assert resp.status_code == 200, resp.text
    return resp.json()["conversations"][0]["id"]


# ---------------------------------------------------------------------------
# Anchor + window pass-through over real HTTP
# ---------------------------------------------------------------------------


class TestAnchorWindowOverHttp:
    """Verify the anchor/window axes survive Litestar's query parsing.

    Prior to the parity fix in commit `2f230d6e`, these params were silently
    dropped — the route declared no Parameter() for them so Litestar ignored
    them. Any of these tests would have failed on main pre-fix.
    """

    def test_at_turn_returns_anchored_slice(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=6)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={
                    "anchor": "at_turn", "anchor_value": "2",
                    "window_start": "-1", "window_end": "1",
                },
            )
        assert resp.status_code == 200, resp.text
        turns = resp.json()["conversation"]["turns"]
        # Slice should be turns 1..3 inclusive (anchor 2 +/- 1).
        assert len(turns) == 3
        assert "turn-1-" in turns[0]["prompt"]
        assert "turn-2-" in turns[1]["prompt"]
        assert "turn-3-" in turns[2]["prompt"]

    def test_at_turn_zero_window_returns_single_turn(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=6)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"anchor": "at_turn", "anchor_value": "4"},
            )
        assert resp.status_code == 200, resp.text
        turns = resp.json()["conversation"]["turns"]
        assert len(turns) == 1
        assert "turn-4-" in turns[0]["prompt"]

    def test_at_turn_out_of_range_returns_400(self, tmp_path):
        """Index >= len(turns) raises AnchorOutOfRange → 400 via _dispatch."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=3)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"anchor": "at_turn", "anchor_value": "99"},
            )
        # AnchorOutOfRange is a ValueError subclass; _dispatch maps to 400.
        assert resp.status_code == 400

    def test_at_turn_non_numeric_value_returns_400(self, tmp_path):
        """The route's own coercion catches non-int anchor_value for at_turn."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=3)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"anchor": "at_turn", "anchor_value": "not-a-number"},
            )
        assert resp.status_code == 400
        assert "integer" in resp.text.lower()

    def test_from_start_with_window(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=6)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"anchor": "from_start", "window_end": "2"},
            )
        assert resp.status_code == 200, resp.text
        turns = resp.json()["conversation"]["turns"]
        assert len(turns) == 3
        assert "turn-0-" in turns[0]["prompt"]
        assert "turn-2-" in turns[2]["prompt"]

    def test_from_end_with_window(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=6)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"anchor": "from_end", "window_start": "-2"},
            )
        assert resp.status_code == 200, resp.text
        turns = resp.json()["conversation"]["turns"]
        assert len(turns) == 3
        assert "turn-3-" in turns[0]["prompt"]
        assert "turn-5-" in turns[2]["prompt"]

    def test_around_phrase_anchors_to_matching_turn(self, tmp_path):
        """anchor=around uses FTS to find the phrase; anchor_value is a str."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=6)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={
                    "anchor": "around", "anchor_value": "delta",
                    "window_start": "0", "window_end": "0",
                },
            )
        assert resp.status_code == 200, resp.text
        turns = resp.json()["conversation"]["turns"]
        assert len(turns) == 1
        # "delta" appears in turn index 3 (i%6 == 3 → "delta")
        assert "delta" in turns[0]["prompt"]
        assert "turn-3-" in turns[0]["prompt"]

    def test_around_phrase_not_found_returns_400(self, tmp_path):
        """anchor=around with a phrase that has no FTS match → 400, not 500.

        Pre-fix, AnchorNotFound (subclass of Exception, not ValueError) fell
        through to the generic 500 handler in _dispatch. The CLI's delegated
        user would see an opaque server error instead of the actionable
        "phrase not found" message they'd get locally.
        """
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=3)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"anchor": "around", "anchor_value": "phrase-that-isnt-in-the-corpus"},
            )
        assert resp.status_code == 400
        assert "phrase not found" in resp.text.lower() or "not found" in resp.text.lower()

    def test_around_invalid_fts_phrase_returns_400(self, tmp_path):
        """A phrase that breaks FTS5 parsing (e.g. unbalanced quote) → 400."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=3)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"anchor": "around", "anchor_value": '"unbalanced'},
            )
        # AnchorPhraseInvalid → 400.
        assert resp.status_code == 400

    def test_no_anchor_returns_all_turns(self, tmp_path):
        """When CLI omits anchor, the route must return the full conversation."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=4)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(f"/api/v1/conversations/{conv_id}")
        assert resp.status_code == 200, resp.text
        turns = resp.json()["conversation"]["turns"]
        assert len(turns) == 4


# ---------------------------------------------------------------------------
# Delegation wire format actually parses on the server
# ---------------------------------------------------------------------------


class TestDelegationWireRoundTrip:
    """Synthesize the URL the delegation client would build and confirm the
    server accepts it. This catches contract drift between the OpSpec wire
    serialization (Operation.to_wire / apply_wire) and the route's declared
    Parameter(query=...) names. (Originally written against _remap_params,
    which the ST-5 wire-form dissolution replaced with the OpSpec registry.)
    """

    def test_wire_serialization_output_is_parseable_by_route(self, tmp_path):
        """The querystring produced by apply_wire (Fidelity → axes) must
        result in a 200 against /api/v1/conversations/{id}.
        """
        from painted import Fidelity

        from siftd.api.op_spec import SPECS, apply_wire

        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=4)
        app = create_app(db_path=db, auth_config=None)

        # Mimic what cli/query.py:_query_detail puts into op.params.
        fid = Fidelity(visible=frozenset({"text", "thinking", "tools"}))
        op_params = {
            "id": "ignored-positional",  # serve route reads id from path, not query
            "fidelity": fid,
            "tool_filter": None,
            "anchor": "at_turn",
            "anchor_value": 1,           # int from CLI; urlencode → "1"
            "window_start": -1,
            "window_end": 1,
            "db_path": db,
        }

        # The production wire path: excludes (db_path/id), drop-None,
        # Fidelity → axis expansion, keyword remaps — all inside apply_wire.
        wire = apply_wire(op_params, SPECS[("/api/v1/conversations/{id}", "GET")])
        # str() coercion is what urlencode does for non-string scalars.
        querystring = urlencode({k: str(v) if not isinstance(v, str) else v for k, v in wire.items()})

        # The Fidelity must be gone, and the axes must be present.
        assert "fidelity" not in querystring
        assert "include_thinking=True" in querystring
        assert "include_tool_content=True" in querystring
        assert "anchor=at_turn" in querystring
        assert "anchor_value=1" in querystring

        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(f"/api/v1/conversations/{conv_id}?{querystring}")

        assert resp.status_code == 200, resp.text
        turns = resp.json()["conversation"]["turns"]
        # at_turn=1 with window -1..+1 → turns 0..2
        assert len(turns) == 3
        assert "turn-0-" in turns[0]["prompt"]

    def test_include_thinking_bool_true_string_is_truthy(self, tmp_path):
        """Litestar coerces include_thinking=True (str from urlencode) to bool True."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            # urlencode({"include_thinking": True}) → "include_thinking=True"
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"include_thinking": "true", "include_tool_content": "true"},
            )
        assert resp.status_code == 200, resp.text

    def test_none_valued_params_are_dropped_not_serialized_as_string(self):
        """Drop-None must happen in the wire layer, not just in tests.

        Pre-fix, urlencode({'tool_filter': None}) → 'tool_filter=None' on the
        wire. The route's Litestar Parameter declared the type as str|None and
        treated 'None' (literal four-character string) as a real filter pattern,
        which then filtered out every tool call. Build a real op.params dict
        (with explicit None values) and assert the wire dict omits those keys
        entirely. (apply_wire is the production drop-None site post-ST-5.)
        """
        from siftd.api.op_spec import SPECS, apply_wire

        op_params = {
            "id": "abc",
            "fidelity": None,            # absent fidelity
            "tool_filter": None,         # the bug-trigger
            "anchor": None,
            "anchor_value": None,
            "window_start": None,
            "window_end": None,
            "tag": None,                 # list[str] | None
        }
        wired = apply_wire(op_params, SPECS[("/api/v1/conversations/{id}", "GET")])

        # All None values dropped — the route sees absent params and uses defaults.
        assert "tool_filter" not in wired
        assert "anchor" not in wired
        assert "anchor_value" not in wired
        assert "window_start" not in wired
        assert "window_end" not in wired
        assert "tag" not in wired
        # fidelity also dropped (was None, not a Fidelity object)
        assert "fidelity" not in wired
        assert "include_thinking" not in wired
        assert "include_tool_content" not in wired

    def test_include_thinking_bool_false_string_is_falsy(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={"include_thinking": "false", "include_tool_content": "false"},
            )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Auth middleware integration over real HTTP
# ---------------------------------------------------------------------------


class TestAuthOverHttp:
    """Confirm bearer-token enforcement runs in the real ASGI pipeline."""

    def test_static_token_path_rejects_missing_header(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"static_token": "the-secret"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/stats")
        assert resp.status_code == 401

    def test_static_token_path_rejects_wrong_token(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"static_token": "the-secret"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/api/v1/stats",
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401

    def test_static_token_path_accepts_correct_token(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"static_token": "the-secret"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/stats",
                headers={"Authorization": "Bearer the-secret"},
            )
        assert resp.status_code == 200, resp.text

    def test_health_bypasses_auth_under_real_pipeline(self, tmp_path):
        """The opt={'no_auth': True} on /api/v1/health is honored end-to-end."""
        db = tmp_path / "team.db"
        create_database(db)
        auth_config = {"static_token": "the-secret"}
        app = create_app(db_path=db, auth_config=auth_config)
        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Export route — Phase C format-aware path
# ---------------------------------------------------------------------------


class TestExportArtifactRoute:
    """The format-aware path on /api/v1/export added in Phase C.

    These tests pin the wire shape that the CLI's `siftd export` delegation
    relies on (added in Phase D).
    """

    def test_format_md_returns_export_artifact_shape(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/export", params={"format": "md", "last": 1})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) >= {"content", "media_type", "filename", "count"}
        assert body["media_type"] == "text/markdown"
        assert body["count"] >= 1
        assert isinstance(body["content"], str)

    def test_format_json_returns_artifact_with_json_content(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/export", params={"format": "json", "last": 1})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["media_type"] == "application/json"
        assert body["filename"].endswith(".json")

    def test_format_invalid_returns_400(self, tmp_path):
        db = tmp_path / "team.db"
        create_database(db)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/export", params={"format": "xml"})
        assert resp.status_code == 400

    def test_no_format_returns_legacy_conversation_list_shape(self, tmp_path):
        """Backward compat: absent `format` keeps the original payload shape."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/export", params={"n": 1})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "conversations" in body
        assert "content" not in body  # not the artifact shape

    def test_include_thinking_axis_threads_through_to_fidelity(self, tmp_path):
        """include_thinking=true / include_tool_content=true must reach
        export_document's fidelity (the same wire contract the CLI relies on)."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/export", params={
                "format": "md", "last": 1,
                "include_thinking": "true", "include_tool_content": "true",
            })
        assert resp.status_code == 200, resp.text
        assert "content" in resp.json()


# ---------------------------------------------------------------------------
# Full delegation loop: route → from_wire → typed object → render
# ---------------------------------------------------------------------------


class TestFullDelegationLoop:
    """Phase D wires the CLI through ``try_serve → from_wire → render``. These
    tests close that loop end-to-end at the wire-deserialize-render seam.
    """

    def test_conversation_detail_route_to_typed_detail(self, tmp_path):
        """Hit /api/v1/conversations/{id} via HTTP, deserialize the response,
        confirm we get a ConversationDetail the renderer would accept."""
        from siftd.api.conversations import ConversationDetail
        from siftd.api.deserialize import deserialize_conversation_detail

        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=4)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(f"/api/v1/conversations/{conv_id}")
        assert resp.status_code == 200, resp.text

        detail = deserialize_conversation_detail(resp.json())
        assert isinstance(detail, ConversationDetail)
        assert detail.id == conv_id
        assert len(detail.turns) == 4
        # Token splits reconstructed from per-turn data:
        assert detail.total_input_tokens > 0
        assert detail.total_output_tokens > 0
        # The renderer below would consume detail.turns directly.
        assert all(t.prompt_text is not None for t in detail.turns)

    def test_anchored_detail_round_trips_via_wire(self, tmp_path):
        """anchor=at_turn + window over HTTP → deserialized ConversationDetail
        with exactly the requested slice."""
        from siftd.api.deserialize import deserialize_conversation_detail

        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=6)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={
                    "anchor": "at_turn", "anchor_value": "3",
                    "window_start": "-1", "window_end": "1",
                },
            )
        detail = deserialize_conversation_detail(resp.json())
        assert detail is not None
        # at_turn=3 with window -1..+1 → turns 2..4 (three turns).
        assert len(detail.turns) == 3
        assert "turn-2-" in detail.turns[0].prompt_text
        assert "turn-3-" in detail.turns[1].prompt_text
        assert "turn-4-" in detail.turns[2].prompt_text

    def test_export_artifact_route_to_typed_artifact(self, tmp_path):
        """Hit /api/v1/export?format=md → deserialized ExportArtifact."""
        from siftd.api.deserialize import deserialize_export_artifact
        from siftd.api.export import ExportArtifact

        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/export", params={"format": "md", "last": 1})
        artifact = deserialize_export_artifact(resp.json())
        assert isinstance(artifact, ExportArtifact)
        assert artifact.count >= 1
        assert artifact.media_type == "text/markdown"
        assert "turn-" in artifact.content  # markdown body contains the conversation

    def test_conversation_list_route_to_typed_list(self, tmp_path):
        """Hit /api/v1/conversations → deserialized list[ConversationSummary]."""
        from siftd.api.conversations import ConversationSummary
        from siftd.api.deserialize import deserialize_conversation_list

        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/conversations")
        summaries = deserialize_conversation_list(resp.json())
        assert len(summaries) == 1
        assert isinstance(summaries[0], ConversationSummary)
        assert summaries[0].prompt_count == 2


def _stamp_owner(db: Path, user_id: str) -> None:
    """Stamp every conversation in the DB as owned by user_id.

    The auth tests use static_token mode which yields sub="local" by default;
    owner-scoped routes then filter to conversations owned by "local". Our
    fixtures insert conversations without populating conversation_owners
    (that's done by `siftd db push`, not direct inserts), so we have to
    stamp explicitly to match.
    """
    import sqlite3
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT id FROM conversations").fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO conversation_owners "
            "(conversation_id, user_id, push_id, assigned_at) VALUES (?, ?, ?, ?)",
            [(cid, user_id, "test-push", now) for (cid,) in rows],
        )
        conn.commit()
    finally:
        conn.close()


class TestAuthenticatedDelegationLoop:
    """Auth + delegation combined: confirm the full wire-form loop works when
    the route enforces a bearer token (the actual homelab topology).
    """

    def test_authenticated_conversation_detail_round_trip(self, tmp_path):
        from siftd.api.conversations import ConversationDetail
        from siftd.api.deserialize import deserialize_conversation_detail

        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=3)
        _stamp_owner(db, "local")  # static_token mode's default identity
        app = create_app(db_path=db, auth_config={"static_token": "secret"})

        with TestClient(app) as client:
            # First: list (also under auth) to discover an id.
            list_resp = client.get(
                "/api/v1/conversations",
                headers={"Authorization": "Bearer secret"},
            )
            assert list_resp.status_code == 200, list_resp.text
            conv_id = list_resp.json()["conversations"][0]["id"]

            # Detail with anchor + window, under auth, with thinking visible.
            detail_resp = client.get(
                f"/api/v1/conversations/{conv_id}",
                params={
                    "anchor": "at_turn", "anchor_value": "1",
                    "window_start": "-1", "window_end": "1",
                    "include_thinking": "true",
                },
                headers={"Authorization": "Bearer secret"},
            )
            assert detail_resp.status_code == 200, detail_resp.text

        detail = deserialize_conversation_detail(detail_resp.json())
        assert isinstance(detail, ConversationDetail)
        assert len(detail.turns) == 3  # at_turn=1 ± 1 → turns 0,1,2

    def test_authenticated_export_artifact_round_trip(self, tmp_path):
        from siftd.api.deserialize import deserialize_export_artifact
        from siftd.api.export import ExportArtifact

        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=2)
        _stamp_owner(db, "local")
        app = create_app(db_path=db, auth_config={"static_token": "secret"})

        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/export",
                params={"format": "md", "last": 1},
                headers={"Authorization": "Bearer secret"},
            )
            assert resp.status_code == 200, resp.text

        artifact = deserialize_export_artifact(resp.json())
        assert isinstance(artifact, ExportArtifact)
        assert artifact.count >= 1

    def test_unauthenticated_request_does_not_leak_typed_data(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db", n_turns=1)
        app = create_app(db_path=db, auth_config={"static_token": "secret"})
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/conversations")
        assert resp.status_code == 401
        # Body should NOT be parseable as a conversation list — None signals
        # the CLI to fall back to local execute rather than treat the auth
        # error body as an empty conversation list.
        from siftd.api.deserialize import deserialize_conversation_list
        summaries = deserialize_conversation_list(resp.json())
        assert summaries is None


# ---------------------------------------------------------------------------
# request_max_body_size enforcement
# ---------------------------------------------------------------------------


class TestRequestBodySizeLimit:
    """Verify that request_max_body_size is wired through to Litestar.

    The push route reads via request.stream(); Litestar enforces the limit
    before the handler runs, so a too-large body gets 413 without touching
    application code.
    """

    def test_oversized_body_returns_413(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None, request_max_body_size=100)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/push", content=b"x" * 200)
        assert resp.status_code == 413

    def test_within_limit_body_is_accepted(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        # 1 MiB limit; 200-byte payload is well within it.
        # The body is junk (not a valid SQLite DB), so the route will reject
        # it after accepting the body — that's fine: the discriminator here
        # is only "not 413".
        app = create_app(db_path=db, auth_config=None, request_max_body_size=1 << 20)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/push", content=b"x" * 200)
        assert resp.status_code != 413

    def test_large_payload_within_limit_is_accepted(self, tmp_path):
        """A 12 MB payload passes when the limit is above it."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None, request_max_body_size=50_000_000)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/push", content=b"\x00" * 12_000_000)
        assert resp.status_code != 413

    def test_payload_at_exact_limit_is_accepted(self, tmp_path):
        """A payload of exactly limit bytes must not return 413."""
        limit = 1000
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None, request_max_body_size=limit)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/push", content=b"x" * limit)
        assert resp.status_code != 413

    def test_payload_one_byte_over_limit_returns_413(self, tmp_path):
        """A payload of limit+1 bytes must return 413."""
        limit = 1000
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None, request_max_body_size=limit)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/push", content=b"x" * (limit + 1))
        assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Search mode wire contract (ST-4a)
# ---------------------------------------------------------------------------


def _init_empty_embed_db():
    """Create an initialized, empty embeddings DB at the sandboxed default path.

    The search route resolves the *server's* default embeddings DB (embed_db is
    wire-excluded), which `_sandbox_db_home` redirects into tmp. Hybrid/semantic
    searches raise FileNotFoundError (-> 404) when that DB is absent; these
    wire-contract tests assert the route accepts the mode, so they need the DB
    to exist — empty is enough (missing index metadata degrades with a warning).

    No-op without the [embed] extra (storage.embeddings imports numpy at module
    level): the route 501s before resolving the DB path, so it isn't needed.
    """
    from siftd.paths import embeddings_db_path

    try:
        from siftd.storage.embeddings import open_embeddings_db
    except ImportError:
        return

    open_embeddings_db(embeddings_db_path()).close()


class TestSearchModeWireContract:
    """Verify the `mode` query param travels on the wire and is honoured by the route.

    ST-4a: `mode` was previously in `_WIRE_EXCLUDE` and the search route had no
    matching Parameter, making FTS-only mode unreachable via delegation. These
    tests confirm that all three modes reach the route and that an invalid mode
    returns 400 rather than silently being ignored.

    FTS mode does not require the [embed] extra — `search_chunks` lazy-imports
    embeddings only for semantic/hybrid paths. hybrid and semantic tests are
    marked `embeddings` so they only run with `./dev test-all`.
    """

    def test_search_mode_fts_returns_results(self, tmp_path):
        """mode=fts reaches the route, executes FTS-only search, returns results."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/search",
                params={"q": "alpha", "mode": "fts", "n": "5"},
            )
        # search_chunks is importable without embed — no 501.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, dict)
        results = body.get("results", body) if isinstance(body, dict) else body
        # The multi-turn fixture includes "turn-0-unique-marker-alpha" in turn 0.
        assert len(results) > 0, f"Expected FTS results for 'alpha'; got empty: {body}"

    def test_search_facet_only_enumerates_tagged_elements(self, tmp_path):
        """No query + a tag facet → element hits, no 400 for the missing q."""
        from siftd.api.tags import apply_tags
        from siftd.storage.sqlite import open_database

        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        conn = open_database(db, read_only=True)
        try:
            rid = conn.execute("SELECT id FROM events WHERE kind='response' LIMIT 1").fetchone()["id"]
        finally:
            conn.close()
        apply_tags(db_path=db, tags=["docs:thing"], entity_type="response", entity_id=rid)

        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get("/api/v1/search", params={"tag": "docs:thing"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        results = body.get("results", body) if isinstance(body, dict) else body
        assert len(results) == 1
        assert results[0]["event_id"] == rid
        assert results[0]["tags"] == ["docs:thing"]

    def test_event_detail_route_surfaces_element_tags(self, tmp_path):
        """GET /api/v1/events/{id} carries the element's tags (WS7 read-back)."""
        from siftd.api.tags import apply_tags
        from siftd.storage.sqlite import open_database

        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        conn = open_database(db, read_only=True)
        try:
            rid = conn.execute("SELECT id FROM events WHERE kind='response' LIMIT 1").fetchone()["id"]
        finally:
            conn.close()
        apply_tags(db_path=db, tags=["docs:thing"], entity_type="response", entity_id=rid)

        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get(f"/api/v1/events/{rid}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tags"] == ["docs:thing"]

    def test_search_bad_mode_returns_400(self, tmp_path):
        """An unrecognised mode value must return HTTP 400, not silently fall through."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get(
                "/api/v1/search",
                params={"q": "alpha", "mode": "bogus"},
            )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert "error" in body, f"400 response must include an 'error' key; got: {body}"

    @pytest.mark.embeddings
    def test_search_mode_hybrid_accepted(self, tmp_path):
        """mode=hybrid is accepted by the route (backward-compat check)."""
        _init_empty_embed_db()
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/search",
                params={"q": "alpha", "mode": "hybrid", "n": "5"},
            )
        assert resp.status_code == 200, resp.text

    @pytest.mark.embeddings
    def test_search_mode_semantic_accepted(self, tmp_path):
        """mode=semantic is accepted by the route (backward-compat check)."""
        _init_empty_embed_db()
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/search",
                params={"q": "alpha", "mode": "semantic", "n": "5"},
            )
        assert resp.status_code == 200, resp.text

    def test_mode_fts_returns_200_without_embeddings(self, tmp_path):
        """mode=fts executes keyword search and returns 200 in a no-embed env."""
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/search",
                params={"q": "alpha", "mode": "fts"},
            )
        assert resp.status_code == 200, (
            f"mode=fts must return 200 without embeddings; got {resp.status_code}: {resp.text}"
        )


class TestSearchLogWebClickLinkage:
    """The Find fragment's rendered result link carries search_event_id through
    to /folio, which records the precise web-click open-signal (see
    docs/dev/search-log-design-2026-07-07.md). ``HX-Request: true`` is sent so
    the fragment routes serve their swap content instead of 303-redirecting to
    the full shell (see html_routes._is_htmx)."""

    def test_query_fragment_result_link_carries_search_event_id(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            resp = client.get(
                "/query",
                params={"search": "unique-marker"},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200, resp.text
        assert "search_event_id=" in resp.text

        from siftd.storage.sqlite import open_database

        conn = open_database(db, read_only=True)
        try:
            rows = conn.execute("SELECT id, query, issuer FROM search_events").fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["query"] == "unique-marker"
        assert rows[0]["issuer"] == "web"

    def test_folio_click_with_search_event_id_records_open(self, tmp_path):
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            query_resp = client.get(
                "/query",
                params={"search": "unique-marker"},
                headers={"HX-Request": "true"},
            )
            assert query_resp.status_code == 200, query_resp.text

            from siftd.storage.sqlite import open_database

            conn = open_database(db, read_only=True)
            try:
                sid = conn.execute("SELECT id FROM search_events").fetchone()["id"]
            finally:
                conn.close()

            # Simulate the browser following the rendered hx-get link: /folio
            # receives the same search_event_id the fragment embedded.
            folio_resp = client.get(
                "/folio",
                params={"id": conv_id, "mode": "trace", "search_event_id": sid},
                headers={"HX-Request": "true"},
            )
            assert folio_resp.status_code == 200, folio_resp.text

        conn = open_database(db, read_only=True)
        try:
            opens = conn.execute("SELECT * FROM search_opens").fetchall()
        finally:
            conn.close()
        assert len(opens) == 1
        assert opens[0]["search_event_id"] == sid
        assert opens[0]["conversation_id"] == conv_id
        assert opens[0]["surface"] == "web-click"

    def test_folio_click_without_search_event_id_records_nothing(self, tmp_path):
        """A direct (non-search-originated) folio open — no prior search and no
        search_event_id on the request — must not fabricate a link. (A prior
        search containing this conversation would legitimately bind via the
        CLI-style heuristic even without an explicit search_event_id — that's
        covered by the linkage tests above, not this one.)"""
        db, _ = _make_multi_turn_db(tmp_path / "team.db")
        app = create_app(db_path=db, auth_config=None)
        with TestClient(app) as client:
            conv_id = _resolve_conv_id(client)
            resp = client.get(
                "/folio", params={"id": conv_id}, headers={"HX-Request": "true"},
            )
            assert resp.status_code == 200, resp.text

        from siftd.storage.sqlite import open_database

        conn = open_database(db, read_only=True)
        try:
            opens = conn.execute("SELECT * FROM search_opens").fetchall()
        finally:
            conn.close()
        assert opens == []
