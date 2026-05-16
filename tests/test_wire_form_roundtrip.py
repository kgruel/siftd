"""Round-trip tests for the wire-form deserializers.

For each typed result that flows over the delegation wire, fetch locally →
serialize → deserialize → assert structural fidelity. This pins the
response-side half of the operation-has-local-form-and-wire-form pattern.

See ``docs/guides/delegation-contract.md`` and
``siftd/serialization/deserialize.py`` for the contract.
"""

from __future__ import annotations

from pathlib import Path

from siftd.api.conversations import (
    ConversationDetail,
    ConversationSummary,
    NarrativeBlock,
    ToolCallDetail,
    Turn,
)
from siftd.serialization.conversations import (
    serialize_conversation_detail,
    serialize_conversation_list,
    serialize_conversation_summary,
)
from siftd.api.deserialize import (
    deserialize_conversation_detail,
    deserialize_conversation_list,
    deserialize_conversation_summary,
    deserialize_export_artifact,
    deserialize_narrative_block,
    from_wire,
)


# ---------------------------------------------------------------------------
# Conversation summary round-trip
# ---------------------------------------------------------------------------


class TestConversationSummaryRoundTrip:
    def test_basic_summary_roundtrips(self):
        original = ConversationSummary(
            id="01HX...",
            workspace_path="/home/proj",
            model="gpt-4",
            started_at="2024-01-15T10:00:00Z",
            prompt_count=5,
            response_count=5,
            total_tokens=1234,
            cost=0.05,
            tags=["work", "research"],
            owner="kyle",
        )
        wire = serialize_conversation_summary(original)
        recovered = deserialize_conversation_summary(wire)
        assert recovered == original

    def test_minimal_summary_with_optional_fields_absent(self):
        original = ConversationSummary(
            id="01HX...",
            workspace_path=None,
            model=None,
            started_at=None,
            prompt_count=0,
            response_count=0,
            total_tokens=0,
            cost=None,
            tags=[],
            owner=None,
        )
        wire = serialize_conversation_summary(original)
        recovered = deserialize_conversation_summary(wire)
        assert recovered == original


class TestConversationListRoundTrip:
    def test_list_roundtrips(self):
        items = [
            ConversationSummary(
                id=f"c{i}", workspace_path=None, model=None, started_at=None,
                prompt_count=i, response_count=i, total_tokens=i * 100, cost=None,
            )
            for i in range(3)
        ]
        wire = {"conversations": serialize_conversation_list(items)}
        recovered = deserialize_conversation_list(wire)
        assert recovered == items

    def test_empty_list_roundtrips(self):
        wire = {"conversations": []}
        recovered = deserialize_conversation_list(wire)
        assert recovered == []


# ---------------------------------------------------------------------------
# Narrative block round-trip
# ---------------------------------------------------------------------------


class TestNarrativeBlockDeserialization:
    """The serializer emits via JsonEmitter; here we exercise the inverse
    against the wire shapes that JsonEmitter produces.
    """

    def test_text_block(self):
        d = {"type": "text", "content": "hello world", "event_id": "ev1"}
        block = deserialize_narrative_block(d)
        assert block == NarrativeBlock(
            block_type="text", content="hello world", event_id="ev1",
        )

    def test_thinking_block(self):
        d = {"type": "thinking", "content": "reasoning...", "event_id": "ev2"}
        block = deserialize_narrative_block(d)
        assert block.block_type == "thinking"
        assert block.content == "reasoning..."

    def test_thinking_placeholder_no_content(self):
        # JsonEmitter.thinking_placeholder emits no "content" field.
        d = {"type": "thinking", "event_id": "ev3"}
        block = deserialize_narrative_block(d)
        assert block.block_type == "thinking"
        assert block.content is None

    def test_tool_calls_aggregated(self):
        # JsonEmitter.tool_summary emits {"type": "tool_calls", "tools": [...]}
        d = {
            "type": "tool_calls",
            "tools": [
                {"name": "shell.run", "count": 2, "status": "ok"},
                {"name": "file.read", "count": 1},
            ],
        }
        block = deserialize_narrative_block(d)
        assert block.block_type == "tool_calls"
        assert len(block.tool_calls) == 2
        assert block.tool_calls[0].tool_name == "shell.run"
        assert block.tool_calls[0].count == 2
        assert block.tool_calls[0].status == "ok"
        assert block.tool_calls[1].tool_name == "file.read"

    def test_tool_call_singular_expanded(self):
        # JsonEmitter.tool_content emits {"type": "tool_call", ...}
        d = {
            "type": "tool_call",
            "name": "shell.run",
            "count": 1,
            "status": "ok",
            "input": "ls -la",
            "result": "file1\nfile2",
            "tool_call_id": "tc-1",
            "event_id": "ev4",
        }
        block = deserialize_narrative_block(d)
        # Singular tool_call wraps into a tool_calls NarrativeBlock with one entry.
        assert block.block_type == "tool_calls"
        assert len(block.tool_calls) == 1
        tc = block.tool_calls[0]
        assert tc.tool_name == "shell.run"
        assert tc.input == "ls -la"
        assert tc.result == "file1\nfile2"
        assert tc.tool_call_id == "tc-1"
        assert block.event_id == "ev4"

    def test_tool_output_block_roundtrips(self):
        """JsonEmitter.tool_output emits {"type": "tool_output"|"tool_result", ...}.

        Round-tripped to NarrativeBlock preserving block_type, content, event_id.
        """
        for kind in ("tool_output", "tool_result"):
            d = {"type": kind, "content": "stdout text", "event_id": f"ev-{kind}"}
            block = deserialize_narrative_block(d)
            assert block.block_type == kind
            assert block.content == "stdout text"
            assert block.event_id == f"ev-{kind}"

    def test_unknown_block_type_returns_none(self):
        """Forward-compat: deserializer returns None for unrecognized types
        so older CLI clients can talk to newer servers without crashing."""
        d = {"type": "future_block_type", "content": "..."}
        assert deserialize_narrative_block(d) is None


# ---------------------------------------------------------------------------
# Conversation detail round-trip (the substantial case)
# ---------------------------------------------------------------------------


class TestConversationDetailRoundTrip:
    def _make_detail(self, n_turns: int = 3) -> ConversationDetail:
        turns = [
            Turn(
                timestamp=f"2024-01-15T10:{i:02d}:00Z",
                prompt_text=f"prompt-{i}",
                total_input_tokens=10 * (i + 1),
                total_output_tokens=5 * (i + 1),
                narrative=[
                    NarrativeBlock(block_type="text", content=f"response-{i}", event_id=f"r-{i}"),
                ],
                prompt_id=f"p-{i}",
                response_ids=[f"r-{i}"],
                tool_call_ids=[],
            )
            for i in range(n_turns)
        ]
        return ConversationDetail(
            id="01HX...",
            workspace_path="/proj",
            model="gpt-4",
            started_at="2024-01-15T10:00:00Z",
            total_input_tokens=sum(t.total_input_tokens for t in turns),
            total_output_tokens=sum(t.total_output_tokens for t in turns),
            turns=turns,
            tags=["work"],
        )

    def test_minimal_detail_roundtrips_with_token_splits_reconstructed(self):
        original = self._make_detail(n_turns=3)
        wire = {"conversation": serialize_conversation_detail(original)}
        recovered = deserialize_conversation_detail(wire)
        assert recovered is not None
        assert recovered.id == original.id
        assert recovered.workspace_path == original.workspace_path
        assert recovered.model == original.model
        assert recovered.started_at == original.started_at
        assert recovered.tags == original.tags
        assert len(recovered.turns) == len(original.turns)
        # Token splits are reconstructed by summing per-turn (the wire form
        # only carries the combined sum at conversation level).
        assert recovered.total_input_tokens == original.total_input_tokens
        assert recovered.total_output_tokens == original.total_output_tokens

    def test_detail_with_tool_calls_block_roundtrips(self):
        original = ConversationDetail(
            id="01HX...",
            workspace_path="/proj",
            model="gpt-4",
            started_at="2024-01-15T10:00:00Z",
            total_input_tokens=10,
            total_output_tokens=5,
            turns=[
                Turn(
                    timestamp="2024-01-15T10:00:00Z",
                    prompt_text="run shell",
                    total_input_tokens=10,
                    total_output_tokens=5,
                    narrative=[
                        NarrativeBlock(
                            block_type="tool_calls",
                            tool_calls=[
                                ToolCallDetail(tool_name="shell.run", status="ok", count=2),
                            ],
                            event_id="r-1",
                        ),
                    ],
                ),
            ],
        )
        wire = {"conversation": serialize_conversation_detail(original)}
        recovered = deserialize_conversation_detail(wire)
        assert recovered is not None
        turn = recovered.turns[0]
        # Renderer-facing shape: one tool_calls block with the right tools.
        tool_blocks = [b for b in turn.narrative if b.block_type == "tool_calls"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_calls[0].tool_name == "shell.run"

    def test_detail_with_empty_body_returns_none(self):
        recovered = deserialize_conversation_detail({"error": "not found"})
        assert recovered is None


# ---------------------------------------------------------------------------
# Export artifact round-trip
# ---------------------------------------------------------------------------


class TestExportArtifactRoundTrip:
    def test_basic_artifact_roundtrip(self):
        from siftd.api.export import ExportArtifact

        original = ExportArtifact(
            content="# heading\n\ntext",
            media_type="text/markdown",
            filename="siftd-export.md",
            count=1,
        )
        # Phase C will add the matching serializer; for now, hand-build the wire dict.
        wire = {
            "content": original.content,
            "media_type": original.media_type,
            "filename": original.filename,
            "count": original.count,
        }
        recovered = deserialize_export_artifact(wire)
        assert recovered == original


# ---------------------------------------------------------------------------
# from_wire dispatcher
# ---------------------------------------------------------------------------


class TestMalformedInputReturnsNone:
    """Round-4 review hardening: deserializers MUST return None on schema
    mismatch rather than raising. The CLI fallback path treats None as
    "fall back to local execute"; an exception would crash the user instead.
    """

    def test_detail_with_non_dict_conversation_returns_none(self):
        """The reviewer's example: ``{"conversation": []}`` — used to crash
        with AttributeError on ``.get()``, now returns None."""
        assert deserialize_conversation_detail({"conversation": []}) is None
        assert deserialize_conversation_detail({"conversation": "not a dict"}) is None
        assert deserialize_conversation_detail({"conversation": 42}) is None
        assert deserialize_conversation_detail({"conversation": None}) is None

    def test_detail_with_no_id_returns_none(self):
        """A conversation dict missing the id field can't be reconstructed."""
        assert deserialize_conversation_detail({"conversation": {"workspace": "/p"}}) is None

    def test_detail_with_malformed_turns_returns_none_for_bad_turns(self):
        """Non-list turns field is treated as empty; malformed turn entries
        are skipped without crashing the whole conversation."""
        from siftd.api.deserialize import deserialize_turn

        # Non-list turns → treated as empty (and total_tokens falls back to body-level).
        out = deserialize_conversation_detail({"conversation": {
            "id": "c1", "turns": "not a list", "total_tokens": 42,
        }})
        assert out is not None
        assert out.turns == []

        # Individual non-dict turn entries → skipped.
        out = deserialize_conversation_detail({"conversation": {
            "id": "c1",
            "turns": [None, "string", 42, {"timestamp": "2024-01-15T10:00:00Z"}],
        }})
        assert out is not None
        assert len(out.turns) == 1  # only the dict survived

        # deserialize_turn directly returns None for non-dict.
        assert deserialize_turn(None) is None
        assert deserialize_turn("oops") is None
        assert deserialize_turn(42) is None

    def test_detail_with_non_dict_body_returns_none(self):
        from siftd.api.deserialize import deserialize_conversation_detail
        # The body itself is malformed.
        # NOTE: typing says dict; we still guard at runtime.
        assert deserialize_conversation_detail("not a dict") is None  # type: ignore[arg-type]

    def test_list_returns_none_on_schema_mismatch(self):
        """Missing/wrong-shaped conversations key → None (fallback signal).
        Legitimate empty list → [] (a valid result, not a fallback)."""
        assert deserialize_conversation_list({}) is None
        assert deserialize_conversation_list({"error": "auth failed"}) is None
        assert deserialize_conversation_list({"conversations": "not a list"}) is None
        # Legitimate empty:
        assert deserialize_conversation_list({"conversations": []}) == []

    def test_list_skips_malformed_entries(self):
        """Mixed list with some malformed entries: skip bad, keep good."""
        wire = {"conversations": [
            {"id": "c1", "prompts": 1, "responses": 1, "tokens": 10},
            "not a dict",
            42,
            None,
            {"missing": "id field"},
            {"id": "c2", "prompts": 2, "responses": 2, "tokens": 20},
        ]}
        out = deserialize_conversation_list(wire)
        assert out is not None
        assert len(out) == 2
        assert out[0].id == "c1"
        assert out[1].id == "c2"

    def test_summary_with_no_id_returns_none(self):
        from siftd.api.deserialize import deserialize_conversation_summary
        assert deserialize_conversation_summary({"workspace": "/p"}) is None
        assert deserialize_conversation_summary("not a dict") is None  # type: ignore[arg-type]

    def test_detail_with_malformed_field_types_does_not_crash(self):
        """Round-5 finding: field-level type mismatches (non-numeric where int
        expected, scalar where list expected, etc.) must not raise — they
        absorb via _coerce_int / _coerce_list with sensible defaults.
        """
        # total_tokens carrying a string instead of an int.
        out = deserialize_conversation_detail({"conversation": {
            "id": "x", "total_tokens": "bad",
        }})
        assert out is not None
        assert out.total_input_tokens == 0

        # narrative as a scalar instead of a list.
        out = deserialize_conversation_detail({"conversation": {
            "id": "x",
            "turns": [{"narrative": 1, "tokens": {"input": 5, "output": 3}}],
        }})
        assert out is not None
        assert len(out.turns) == 1
        assert out.turns[0].narrative == []

        # tokens as a string instead of a dict.
        out = deserialize_conversation_detail({"conversation": {
            "id": "x",
            "turns": [{"tokens": "not a dict"}],
        }})
        assert out is not None
        assert out.turns[0].total_input_tokens == 0

        # tags as a non-list.
        out = deserialize_conversation_detail({"conversation": {
            "id": "x", "tags": "work",
        }})
        assert out is not None
        assert out.tags == []

    def test_summary_with_malformed_field_types_does_not_crash(self):
        """ConversationSummary with non-numeric prompts/responses/tokens — coerced to 0."""
        from siftd.api.deserialize import deserialize_conversation_summary
        out = deserialize_conversation_summary({"id": "x", "prompts": "bad", "responses": [], "tokens": None})
        assert out is not None
        assert out.prompt_count == 0
        assert out.response_count == 0
        assert out.total_tokens == 0

    def test_list_with_malformed_field_types_does_not_crash(self):
        """ConversationSummary inside a list with malformed field types — entry kept with defaults."""
        wire = {"conversations": [
            {"id": "c1", "prompts": "not-int", "tokens": {"nested": "obj"}, "tags": 42},
        ]}
        out = deserialize_conversation_list(wire)
        assert out is not None
        assert len(out) == 1
        assert out[0].prompt_count == 0
        assert out[0].total_tokens == 0
        assert out[0].tags == []

    def test_export_artifact_with_malformed_count_does_not_crash(self):
        """ExportArtifact with non-numeric count — coerced to 0."""
        out = deserialize_export_artifact({"content": "x", "count": "bad"})
        assert out is not None
        assert out.count == 0

    def test_narrative_tool_calls_with_malformed_entries(self):
        """tool_calls block where tools field is not a list, or contains non-dicts."""
        from siftd.api.deserialize import deserialize_narrative_block

        # tools as a scalar.
        block = deserialize_narrative_block({"type": "tool_calls", "tools": 42})
        assert block is not None
        assert block.tool_calls == []

        # tools as a list with mixed valid/invalid entries.
        block = deserialize_narrative_block({"type": "tool_calls", "tools": [
            "not a dict", None, 42,
            {"name": "shell.run", "count": "bad"},  # malformed count → default 1
            {"name": "file.read", "count": 3},
        ]})
        assert block is not None
        assert len(block.tool_calls) == 2  # only dicts kept
        assert block.tool_calls[0].count == 1  # malformed coerced
        assert block.tool_calls[1].count == 3

    def test_export_artifact_returns_none_on_schema_mismatch(self):
        """The reviewer's specific case: old server returning the legacy
        conversation-list shape on /api/v1/export when CLI expects an
        artifact dict. Now caught at the deserializer rather than the
        CLI catch ladder."""
        # Legacy shape from pre-Phase-C servers:
        assert deserialize_export_artifact({"conversations": [{"id": "c1"}]}) is None
        # Empty body:
        assert deserialize_export_artifact({}) is None
        # Wrong type:
        assert deserialize_export_artifact([]) is None  # type: ignore[arg-type]
        # Legitimate artifact:
        out = deserialize_export_artifact({"content": "# hello", "count": 1})
        assert out is not None and out.content == "# hello"


class TestFromWireDispatcher:
    """The api.dispatch.from_wire indirection picks the right deserializer
    based on op.render_method.
    """

    def test_list_render_method_returns_list_of_summaries(self):
        wire = {"conversations": [
            {"id": "c1", "prompts": 1, "responses": 1, "tokens": 10},
        ]}
        out = from_wire("list", wire)
        assert len(out) == 1
        assert out[0].id == "c1"

    def test_detail_render_method_returns_detail(self):
        wire = {"conversation": {
            "id": "c1", "workspace": None, "model": None, "started_at": None,
            "tags": [], "total_tokens": 0, "turns": [],
        }}
        out = from_wire("detail", wire)
        assert isinstance(out, ConversationDetail)
        assert out.id == "c1"

    def test_unknown_render_method_returns_body_unchanged(self):
        """search/stats/tags don't have registered deserializers — pass through."""
        wire = {"results": [{"id": "x", "score": 0.9}]}
        out = from_wire("search", wire)
        assert out is wire

    def test_dispatch_module_from_wire_delegates(self):
        """api.dispatch.from_wire(op, body) → serialization.deserialize.from_wire."""
        from painted import Fidelity

        from siftd.api.dispatch import Operation
        from siftd.api.dispatch import from_wire as dispatch_from_wire

        op = Operation(
            path="/api/v1/conversations",
            method="GET",
            fn=lambda **kwargs: None,
            params={},
            render_method="list",
            fidelity=Fidelity(),
            db=Path("/tmp/x"),
        )
        wire = {"conversations": []}
        assert dispatch_from_wire(op, wire) == []
