"""Tests for the export API and CLI."""

import json

import pytest

from siftd.api import (
    ExportedConversation,
    ExportOptions,
    export_conversations,
    format_export,
    format_json,
    format_markdown,
    list_conversations,
)
from siftd.api.conversations import NarrativeBlock, ToolCallDetail, Turn


def _make_turn(
    *,
    prompt="Test prompt",
    text="Test response",
    thinking=None,
    tool_calls=None,
    timestamp="2024-01-01T10:00:00Z",
):
    """Build a Turn with narrative blocks for testing."""
    narrative = []
    if thinking:
        narrative.append(NarrativeBlock(block_type="thinking", content=thinking))
    if tool_calls:
        narrative.append(NarrativeBlock(block_type="tool_calls", tool_calls=tool_calls))
    if text:
        narrative.append(NarrativeBlock(block_type="text", content=text))
    return Turn(
        timestamp=timestamp,
        prompt_text=prompt,
        total_input_tokens=100,
        total_output_tokens=50,
        narrative=narrative,
    )


def _make_conv(turns=None, **kwargs):
    """Build an ExportedConversation for testing."""
    defaults = dict(
        id="test123456789",
        workspace_path="/home/user/project",
        workspace_name="project",
        model="claude-opus-4-5",
        started_at="2024-01-15T10:00:00Z",
        turns=turns or [_make_turn()],
        tags=["review"],
        total_tokens=150,
    )
    defaults.update(kwargs)
    return ExportedConversation(**defaults)


class TestExportConversations:
    def test_export_by_last(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        assert len(conversations) == 1
        assert isinstance(conversations[0], ExportedConversation)

    def test_export_by_id(self, test_db):
        summaries = list_conversations(db_path=test_db, limit=1)
        conv_id = summaries[0].id
        conversations = export_conversations(
            conversation_ids=[conv_id], db_path=test_db
        )
        assert len(conversations) == 1
        assert conversations[0].id == conv_id

    def test_export_by_id_prefix(self, test_db):
        summaries = list_conversations(db_path=test_db, limit=1)
        prefix = summaries[0].id[:8]
        conversations = export_conversations(
            conversation_ids=[prefix], db_path=test_db
        )
        assert len(conversations) == 1

    def test_export_has_turns(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        assert len(conversations[0].turns) > 0
        assert conversations[0].turns[0].prompt_text is not None

    def test_export_workspace_filter(self, test_db):
        conversations = export_conversations(
            last=10, workspace="project", db_path=test_db
        )
        assert len(conversations) == 2

        conversations = export_conversations(
            last=10, workspace="nonexistent", db_path=test_db
        )
        assert len(conversations) == 0

    def test_export_workspace_name_populated(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        assert conversations[0].workspace_name == "project"

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            export_conversations(last=1, db_path=tmp_path / "nonexistent.db")


class TestFormatMarkdown:
    def test_default_includes_both_sides(self):
        conv = _make_conv()
        options = ExportOptions()
        output = format_markdown([conv], options)

        assert "### " in output
        assert "User" in output
        assert "Assistant" in output
        assert "Test prompt" in output
        assert "Test response" in output

    def test_session_header(self):
        conv = _make_conv()
        options = ExportOptions()
        output = format_markdown([conv], options)

        assert "# Session test1234" in output
        assert "project" in output
        assert "claude-opus-4-5" in output
        assert "150 tokens" in output

    def test_no_header(self):
        conv = _make_conv()
        options = ExportOptions(no_header=True)
        output = format_markdown([conv], options)

        assert "# Session" not in output
        assert "User" in output

    def test_thinking_placeholder_by_default(self):
        conv = _make_conv(turns=[_make_turn(thinking="Deep analysis...")])
        options = ExportOptions()
        output = format_markdown([conv], options)

        assert "*[thinking]*" in output
        assert "Deep analysis" not in output

    def test_thinking_expanded(self):
        conv = _make_conv(turns=[_make_turn(thinking="Deep analysis here")])
        options = ExportOptions(include_thinking=True)
        output = format_markdown([conv], options)

        assert "Deep analysis here" in output
        assert "*[thinking]*" not in output

    def test_tool_summary_by_default(self):
        tools = [
            ToolCallDetail(tool_name="file.read", status="success", count=3),
            ToolCallDetail(tool_name="shell.execute", status="success", count=1),
        ]
        conv = _make_conv(turns=[_make_turn(tool_calls=tools)])
        options = ExportOptions()
        output = format_markdown([conv], options)

        assert "*[file.read ×3, shell.execute]*" in output

    def test_tool_detail_expanded(self):
        tools = [
            ToolCallDetail(
                tool_name="file.read", status="success", count=1,
                input="src/auth.py", result="def validate(): ...",
            ),
        ]
        conv = _make_conv(turns=[_make_turn(tool_calls=tools)])
        options = ExportOptions(include_tools=True)
        output = format_markdown([conv], options)

        assert "**file.read**" in output
        assert "`src/auth.py`" in output

    def test_brief_truncates(self):
        long_text = "x" * 500
        conv = _make_conv(turns=[_make_turn(text=long_text)])
        options = ExportOptions(brief=True)
        output = format_markdown([conv], options)

        assert "..." in output
        assert len(output) < 500

    def test_timestamps_in_headings(self):
        conv = _make_conv(turns=[_make_turn(timestamp="2024-01-15T10:30:00Z")])
        options = ExportOptions()
        output = format_markdown([conv], options)

        # Timestamp is converted to local time; just verify some HH:MM appears
        assert "— User" in output
        # The heading should contain a time like "HH:MM — User"
        import re
        assert re.search(r"\d{2}:\d{2} — User", output)

    def test_tags_in_header(self):
        conv = _make_conv(tags=["review", "important"])
        options = ExportOptions()
        output = format_markdown([conv], options)

        assert "tags: review, important" in output

    def test_multiple_sessions(self):
        convs = [_make_conv(id="aaa111222333"), _make_conv(id="bbb444555666")]
        options = ExportOptions()
        output = format_markdown(convs, options)

        assert output.count("# Session") == 2

    def test_from_db(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions()
        output = format_markdown(conversations, options)

        assert "# Session" in output
        assert "User" in output


class TestFormatJson:
    def test_valid_json(self):
        conv = _make_conv()
        options = ExportOptions(json_mode=True)
        output = format_json([conv], options)

        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_structure(self):
        conv = _make_conv()
        options = ExportOptions(json_mode=True)
        output = format_json([conv], options)

        data = json.loads(output)
        c = data[0]
        assert "id" in c
        assert "workspace" in c
        assert "model" in c
        assert "turns" in c
        assert "tags" in c

    def test_turns_have_narrative(self):
        conv = _make_conv()
        options = ExportOptions(json_mode=True)
        output = format_json([conv], options)

        data = json.loads(output)
        turn = data[0]["turns"][0]
        assert "prompt" in turn
        assert "narrative" in turn
        assert "tokens" in turn

    def test_thinking_excluded_by_default(self):
        conv = _make_conv(turns=[_make_turn(thinking="Secret thoughts")])
        options = ExportOptions(json_mode=True)
        output = format_json([conv], options)

        data = json.loads(output)
        thinking_blocks = [
            b for b in data[0]["turns"][0]["narrative"]
            if b["type"] == "thinking"
        ]
        assert len(thinking_blocks) == 1
        assert "content" not in thinking_blocks[0]  # block present, content omitted

    def test_thinking_included(self):
        conv = _make_conv(turns=[_make_turn(thinking="Secret thoughts")])
        options = ExportOptions(json_mode=True, include_thinking=True)
        output = format_json([conv], options)

        data = json.loads(output)
        thinking_blocks = [
            b for b in data[0]["turns"][0]["narrative"]
            if b["type"] == "thinking"
        ]
        assert thinking_blocks[0]["content"] == "Secret thoughts"

    def test_tool_detail_included(self):
        tools = [
            ToolCallDetail(
                tool_name="file.read", status="success",
                input="foo.py", result="contents",
            ),
        ]
        conv = _make_conv(turns=[_make_turn(tool_calls=tools)])
        options = ExportOptions(json_mode=True, include_tools=True)
        output = format_json([conv], options)

        data = json.loads(output)
        tool_block = next(
            b for b in data[0]["turns"][0]["narrative"]
            if b["type"] == "tool_call"
        )
        assert tool_block["input"] == "foo.py"

    def test_from_db(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions(json_mode=True)
        output = format_json(conversations, options)

        data = json.loads(output)
        assert len(data) == 1
        assert "turns" in data[0]


class TestFormatExport:
    def test_default_is_markdown(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions()
        output = format_export(conversations, options)

        assert "# Session" in output

    def test_json_mode(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions(json_mode=True)
        output = format_export(conversations, options)

        data = json.loads(output)
        assert isinstance(data, list)


class TestExportCLI:
    def test_export_default(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export"])
        assert result == 0

    def test_export_last_n(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--last", "2"])
        assert result == 0

    def test_export_json(self, test_db, capsys):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_export_thinking(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--thinking"])
        assert result == 0

    def test_export_tools(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--tools"])
        assert result == 0

    def test_export_full(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--full"])
        assert result == 0

    def test_export_brief(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--brief"])
        assert result == 0

    def test_export_to_file(self, test_db, tmp_path):
        from siftd.cli import main

        output_file = tmp_path / "export.md"
        result = main(["--db", str(test_db), "export", "-o", str(output_file)])
        assert result == 0

        assert output_file.exists()
        content = output_file.read_text()
        assert "# Session" in content

    def test_export_no_header(self, test_db, capsys):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--no-header"])
        assert result == 0

        captured = capsys.readouterr()
        assert "# Session" not in captured.out

    def test_export_workspace_filter(self, test_db):
        from siftd.cli import main

        result = main([
            "--db", str(test_db),
            "export", "-w", "project", "--last", "10"
        ])
        assert result == 0

    def test_export_missing_db(self, tmp_path):
        from siftd.cli import main

        result = main(["--db", str(tmp_path / "nope.db"), "export"])
        assert result == 1

    def test_export_no_matches(self, test_db, capsys):
        from siftd.cli import main

        result = main([
            "--db", str(test_db),
            "export", "-w", "nonexistent_workspace"
        ])
        assert result == 1

        captured = capsys.readouterr()
        assert "No conversations found" in captured.out
