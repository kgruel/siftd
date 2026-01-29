"""Tests for the export API and CLI."""

import json
from pathlib import Path

import pytest

from siftd.api import (
    ExportedConversation,
    ExportOptions,
    export_conversations,
    format_export,
    format_exchanges,
    format_json,
    format_prompts,
    list_conversations,
)
from siftd.api.conversations import Exchange, ToolCallSummary


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

    def test_export_includes_exchanges(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)

        assert len(conversations[0].exchanges) > 0
        assert conversations[0].exchanges[0].prompt_text is not None

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


class TestFormatPrompts:
    def test_basic_output(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_prompts(conversations)

        assert "## Session" in output
        assert "1." in output  # numbered prompt

    def test_includes_workspace_and_date(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_prompts(conversations)

        assert "project" in output
        assert "2024-01" in output

    def test_no_header_option(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_prompts(conversations, no_header=True)

        assert "## Session" not in output
        assert "1." in output  # still has numbered prompts

    def test_multiple_sessions_separated(self, test_db):
        conversations = export_conversations(last=2, db_path=test_db)
        output = format_prompts(conversations)

        # Should have two session headers
        assert output.count("## Session") == 2


class TestFormatExchanges:
    def test_includes_prompts_and_responses(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_exchanges(conversations)

        assert "**User:**" in output
        assert "**Assistant:**" in output

    def test_prompts_only_option(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_exchanges(conversations, prompts_only=True)

        assert "**User:**" in output
        assert "**Assistant:**" not in output

    def test_includes_tool_calls(self):
        # Create mock conversation with tool calls
        conv = ExportedConversation(
            id="test123",
            workspace_path="/test",
            workspace_name="test",
            model="claude-3",
            started_at="2024-01-01T10:00:00Z",
            exchanges=[
                Exchange(
                    timestamp="2024-01-01T10:00:00Z",
                    prompt_text="Test prompt",
                    response_text="Test response",
                    input_tokens=100,
                    output_tokens=50,
                    tool_calls=[
                        ToolCallSummary(tool_name="shell.execute", status="success", count=2),
                    ],
                )
            ],
            tags=[],
            total_tokens=150,
        )

        output = format_exchanges([conv])

        assert "*Tools:" in output
        assert "shell.execute ×2" in output


class TestFormatJson:
    def test_valid_json_output(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_json(conversations)

        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_includes_all_fields(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_json(conversations)

        data = json.loads(output)
        conv = data[0]

        assert "id" in conv
        assert "workspace" in conv
        assert "workspace_name" in conv
        assert "model" in conv
        assert "started_at" in conv
        assert "exchanges" in conv
        assert "tags" in conv
        assert "total_tokens" in conv

    def test_exchanges_structure(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_json(conversations)

        data = json.loads(output)
        exchange = data[0]["exchanges"][0]

        assert "timestamp" in exchange
        assert "prompt" in exchange
        assert "response" in exchange
        assert "tool_calls" in exchange
        assert "tokens" in exchange

    def test_prompts_only_option(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        output = format_json(conversations, prompts_only=True)

        data = json.loads(output)
        exchange = data[0]["exchanges"][0]

        assert "prompt" in exchange
        assert "response" not in exchange
        assert "tool_calls" not in exchange


class TestFormatExport:
    def test_prompts_format(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions(format="prompts")
        output = format_export(conversations, options)

        assert "## Session" in output
        assert "1." in output

    def test_exchanges_format(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions(format="exchanges")
        output = format_export(conversations, options)

        assert "**User:**" in output

    def test_json_format(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions(format="json")
        output = format_export(conversations, options)

        data = json.loads(output)
        assert isinstance(data, list)

    def test_prompts_only_passed_through(self, test_db):
        conversations = export_conversations(last=1, db_path=test_db)
        options = ExportOptions(format="exchanges", prompts_only=True)
        output = format_export(conversations, options)

        assert "**Assistant:**" not in output


class TestExportCLI:
    def test_export_last_default(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export"])
        assert result == 0

    def test_export_last_n(self, test_db):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--last", "2"])
        assert result == 0

    def test_export_format_json(self, test_db, capsys):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)

    def test_export_format_exchanges(self, test_db, capsys):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--format", "exchanges"])
        assert result == 0

        captured = capsys.readouterr()
        assert "**User:**" in captured.out

    def test_export_to_file(self, test_db, tmp_path):
        from siftd.cli import main

        output_file = tmp_path / "export.md"
        result = main(["--db", str(test_db), "export", "-o", str(output_file)])
        assert result == 0

        assert output_file.exists()
        content = output_file.read_text()
        assert "## Session" in content

    def test_export_prompts_only(self, test_db, capsys):
        from siftd.cli import main

        result = main([
            "--db", str(test_db),
            "export", "--format", "exchanges", "--prompts-only"
        ])
        assert result == 0

        captured = capsys.readouterr()
        assert "**User:**" in captured.out
        assert "**Assistant:**" not in captured.out

    def test_export_no_header(self, test_db, capsys):
        from siftd.cli import main

        result = main(["--db", str(test_db), "export", "--no-header"])
        assert result == 0

        captured = capsys.readouterr()
        assert "## Session" not in captured.out

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
