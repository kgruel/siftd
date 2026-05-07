"""Phase 4: tests for the event detail surface."""

from __future__ import annotations

import pytest

from siftd.api import EventDetail, get_event, get_event_neighbors
from siftd.api.conversations import resolve_entity_id
from siftd.serialization.events import serialize_event_detail
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
    insert_tool_call,
    open_database,
)
from siftd.storage.tags import apply_tag, get_or_create_tag


@pytest.fixture
def db_with_events(tmp_path):
    db_path = tmp_path / "events.db"
    conn = create_database(db_path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-3-opus")
    t = get_or_create_tool(conn, "shell.execute")

    c = insert_conversation(conn, external_id="c1", harness_id=h,
                            workspace_id=ws, started_at="2024-01-15T10:00:00Z")

    # Two prompts and responses; a single tool_call under r1.
    p1 = insert_prompt(conn, c, "p1-ext", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, p1, 0, "text", '{"text": "do thing"}')
    r1 = insert_response(conn, c, p1, m, None, "r1-ext",
                         "2024-01-15T10:00:01Z", input_tokens=10, output_tokens=5)
    insert_response_content(conn, r1, 0, "text", '{"text": "result text"}')
    insert_response_content(conn, r1, 1, "tool_use",
                            '{"id": "toolu_1", "name": "shell.execute"}')
    tc1 = insert_tool_call(conn, r1, c, t, "toolu_1",
                           '{"command": "ls"}', '"ok"', "success",
                           "2024-01-15T10:00:01Z")

    p2 = insert_prompt(conn, c, "p2-ext", "2024-01-15T11:00:00Z")
    insert_prompt_content(conn, p2, 0, "text", '{"text": "next"}')
    r2 = insert_response(conn, c, p2, m, None, "r2-ext",
                         "2024-01-15T11:00:01Z", input_tokens=20, output_tokens=10)
    insert_response_content(conn, r2, 0, "text", '{"text": "another"}')

    # Tag r1 directly (response-kind tag)
    review_tag = get_or_create_tag(conn, "review-me")
    apply_tag(conn, "response", r1, review_tag)

    conn.commit()
    conn.close()
    return db_path, c, p1, p2, r1, r2, tc1


class TestGetEventByKind:
    def test_get_response(self, db_with_events):
        db, c, _p1, _p2, r1, _r2, _tc1 = db_with_events
        detail = get_event(r1, db_path=db)
        assert detail is not None
        assert detail.id == r1
        assert detail.kind == "response"
        assert detail.conversation_id == c
        assert detail.external_id == "r1-ext"
        assert detail.kind_specific["model"] == "claude-3-opus"
        assert detail.kind_specific["input_tokens"] == 10
        assert detail.kind_specific["output_tokens"] == 5
        # Tagged in fixture
        assert "review-me" in detail.tags

    def test_get_prompt(self, db_with_events):
        db, c, p1, *_ = db_with_events
        detail = get_event(p1, db_path=db)
        assert detail is not None
        assert detail.kind == "prompt"
        assert detail.conversation_id == c
        # Prompt has no kind_specific
        assert detail.kind_specific == {}

    def test_get_tool_call(self, db_with_events):
        db, _c, _p1, _p2, r1, _r2, tc1 = db_with_events
        detail = get_event(tc1, db_path=db)
        assert detail is not None
        assert detail.kind == "tool_call"
        assert detail.parent_id == r1
        assert detail.kind_specific["tool_name"] == "shell.execute"
        assert detail.kind_specific["status"] == "success"
        # Result is the content_blob payload
        assert detail.kind_specific["result"] == '"ok"'

    def test_response_lists_child_tool_calls(self, db_with_events):
        db, _c, _p1, _p2, r1, _r2, tc1 = db_with_events
        detail = get_event(r1, db_path=db)
        children = detail.kind_specific.get("tool_calls") or []
        ids = [child["id"] for child in children]
        assert tc1 in ids

    def test_unknown_id_returns_none(self, db_with_events):
        db, *_ = db_with_events
        assert get_event("ABCDEFGHIJ123456", db_path=db) is None


class TestGetEventPrefixMatch:
    def test_prefix_resolves(self, db_with_events):
        db, _c, _p1, _p2, r1, _r2, _tc1 = db_with_events
        prefix = r1[:12]
        detail = get_event(prefix, db_path=db)
        assert detail is not None
        assert detail.id == r1

    def test_resolve_entity_id_prompt_prefix(self, db_with_events):
        db, _c, p1, *_ = db_with_events
        conn = open_database(db, read_only=True)
        try:
            resolved = resolve_entity_id(conn, "prompt", p1[:12])
            assert resolved == p1
        finally:
            conn.close()

    def test_resolve_entity_id_tool_call_prefix(self, db_with_events):
        db, *_, tc1 = db_with_events
        conn = open_database(db, read_only=True)
        try:
            resolved = resolve_entity_id(conn, "tool_call", tc1[:12])
            assert resolved == tc1
        finally:
            conn.close()


class TestEventDetailContent:
    def test_includes_content_blocks_by_default(self, db_with_events):
        db, _c, _p1, _p2, r1, *_ = db_with_events
        detail = get_event(r1, db_path=db)
        assert detail.content_blocks
        types = [b["block_type"] for b in detail.content_blocks]
        assert "text" in types and "tool_use" in types

    def test_include_content_false_omits_blocks(self, db_with_events):
        db, _c, _p1, _p2, r1, *_ = db_with_events
        detail = get_event(r1, db_path=db, include_content=False)
        assert detail.content_blocks == []


class TestNeighbors:
    def test_neighbors_default_off(self, db_with_events):
        db, _c, _p1, _p2, r1, *_ = db_with_events
        detail = get_event(r1, db_path=db)
        assert detail.neighbors is None

    def test_neighbors_chain(self, db_with_events):
        db, _c, _p1, _p2, r1, r2, _tc1 = db_with_events
        detail_r1 = get_event(r1, db_path=db, include_neighbors=True)
        assert detail_r1.neighbors == {"prev_event_id": None, "next_event_id": r2}

        detail_r2 = get_event(r2, db_path=db, include_neighbors=True)
        assert detail_r2.neighbors == {"prev_event_id": r1, "next_event_id": None}

    def test_get_event_neighbors_helper(self, db_with_events):
        db, _c, _p1, _p2, r1, r2, _tc1 = db_with_events
        nb = get_event_neighbors(r1, db_path=db)
        assert nb == {"prev_event_id": None, "next_event_id": r2}


class TestSerializeEventDetail:
    def test_response_shape(self, db_with_events):
        db, c, _p1, _p2, r1, _r2, tc1 = db_with_events
        detail = get_event(r1, db_path=db)
        d = serialize_event_detail(detail)
        assert d["id"] == r1
        assert d["kind"] == "response"
        assert d["conversation_id"] == c
        assert d["tags"] == ["review-me"]
        assert d["conversation"]["id"] == c
        # response kind splits kind_specific into "response" + "tool_calls"
        assert "response" in d
        assert d["response"]["model"] == "claude-3-opus"
        assert d["response"]["input_tokens"] == 10
        assert "tool_calls" in d
        assert any(child["id"] == tc1 for child in d["tool_calls"])
        assert "neighbors" not in d  # default off

    def test_prompt_shape_omits_kind_specific(self, db_with_events):
        db, _c, p1, *_ = db_with_events
        detail = get_event(p1, db_path=db)
        d = serialize_event_detail(detail)
        # Prompt has no kind_specific top-level wrapper
        assert "response" not in d
        assert "tool_call" not in d

    def test_tool_call_shape(self, db_with_events):
        db, *_, tc1 = db_with_events
        detail = get_event(tc1, db_path=db)
        d = serialize_event_detail(detail)
        assert d["kind"] == "tool_call"
        assert "tool_call" in d
        assert d["tool_call"]["tool_name"] == "shell.execute"

    def test_neighbors_emitted_when_present(self, db_with_events):
        db, _c, _p1, _p2, r1, r2, _tc1 = db_with_events
        detail = get_event(r1, db_path=db, include_neighbors=True)
        d = serialize_event_detail(detail)
        assert d["neighbors"]["next_event_id"] == r2


class TestPromptTagsIncludeExchangeTags:
    def test_exchange_tag_surfaces_on_prompt(self, db_with_events):
        db, _c, p1, *_ = db_with_events
        # Apply an exchange tag (target_kind='exchange', anchor on prompt)
        conn = open_database(db, read_only=False)
        try:
            tag_id = get_or_create_tag(conn, "exchange-tag")
            apply_tag(conn, "exchange", p1, tag_id, commit=True)
        finally:
            conn.close()

        detail = get_event(p1, db_path=db)
        assert "exchange-tag" in detail.tags
