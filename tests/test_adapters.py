"""Tests for conversation log adapters.

Each test uses a minimal fixture file to verify:
- can_handle() recognizes the file format
- parse() yields Conversation with expected structure
- Prompts, responses, and tool calls are extracted correctly
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.domain.source import Source
from strata.adapters import claude_code, codex_cli, gemini_cli

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestClaudeCodeAdapter:
    """Tests for the Claude Code adapter."""

    def test_can_handle_jsonl(self):
        """Adapter handles .jsonl files."""
        source = Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")
        assert claude_code.can_handle(source)

    def test_can_handle_rejects_json(self):
        """Adapter rejects non-jsonl files."""
        source = Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json")
        assert not claude_code.can_handle(source)

    def test_parse_extracts_conversation(self):
        """Parse yields a conversation with correct metadata."""
        source = Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")
        convos = list(claude_code.parse(source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "claude_code::test-session-1"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "claude_code"
        assert conv.harness.source == "anthropic"

    def test_parse_extracts_prompts_and_responses(self):
        """Parse extracts prompts with their responses."""
        source = Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")
        conv = list(claude_code.parse(source))[0]

        # Should have 1 user prompt (tool_result is not a separate prompt)
        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert prompt.content[0].block_type == "text"
        assert "Hello" in prompt.content[0].content.get("text", "")

        # Prompt should have 2 responses
        assert len(prompt.responses) == 2

    def test_parse_extracts_tool_calls(self):
        """Parse extracts tool calls with results."""
        source = Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")
        conv = list(claude_code.parse(source))[0]

        response = conv.prompts[0].responses[0]
        assert len(response.tool_calls) == 1

        tool_call = response.tool_calls[0]
        assert tool_call.tool_name == "Read"
        assert tool_call.input.get("file_path") == "/test/workspace/README.md"
        assert tool_call.status == "success"
        assert "Test Project" in str(tool_call.result)

    def test_parse_extracts_usage(self):
        """Parse extracts token usage."""
        source = Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")
        conv = list(claude_code.parse(source))[0]

        response = conv.prompts[0].responses[0]
        assert response.usage is not None
        assert response.usage.input_tokens == 100
        assert response.usage.output_tokens == 50

    def test_parse_extracts_cache_tokens(self):
        """Parse extracts cache token attributes."""
        source = Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")
        conv = list(claude_code.parse(source))[0]

        response = conv.prompts[0].responses[0]
        assert response.attributes.get("cache_creation_input_tokens") == "10"


class TestCodexCliAdapter:
    """Tests for the Codex CLI adapter."""

    def test_can_handle_jsonl_in_sessions(self):
        """Adapter handles .jsonl files in sessions path."""
        # Codex requires 'sessions' in path
        source = Source(kind="file", location=Path("/mock/sessions/test.jsonl"))
        assert codex_cli.can_handle(source)

    def test_can_handle_rejects_non_sessions(self):
        """Adapter rejects jsonl not in sessions path."""
        source = Source(kind="file", location=FIXTURES_DIR / "codex_cli_minimal.jsonl")
        # Fixture path doesn't have 'sessions' in it
        assert not codex_cli.can_handle(source)

    def test_parse_extracts_conversation(self, tmp_path):
        """Parse yields a conversation with correct metadata."""
        # Copy fixture to a path with 'sessions' in it
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        fixture = FIXTURES_DIR / "codex_cli_minimal.jsonl"
        dest = sessions_dir / "test.jsonl"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        convos = list(codex_cli.parse(source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "codex_cli::codex-session-1"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "codex_cli"
        assert conv.harness.source == "openai"

    def test_parse_extracts_prompts_and_responses(self, tmp_path):
        """Parse extracts prompts with their responses."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        fixture = FIXTURES_DIR / "codex_cli_minimal.jsonl"
        dest = sessions_dir / "test.jsonl"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        conv = list(codex_cli.parse(source))[0]

        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert "Run ls" in prompt.content[0].content.get("text", "")

        assert len(prompt.responses) == 1

    def test_parse_extracts_tool_calls(self, tmp_path):
        """Parse extracts tool calls with results."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        fixture = FIXTURES_DIR / "codex_cli_minimal.jsonl"
        dest = sessions_dir / "test.jsonl"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        conv = list(codex_cli.parse(source))[0]

        response = conv.prompts[0].responses[0]
        assert len(response.tool_calls) == 1

        tool_call = response.tool_calls[0]
        assert tool_call.tool_name == "shell_command"
        assert tool_call.input.get("command") == "ls -la"
        assert tool_call.status == "success"
        assert "README.md" in str(tool_call.result)


class TestGeminiCliAdapter:
    """Tests for the Gemini CLI adapter."""

    def test_can_handle_json_in_chats(self):
        """Adapter handles .json files in chats directory."""
        source = Source(kind="file", location=Path("/mock/chats/test.json"))
        assert gemini_cli.can_handle(source)

    def test_can_handle_rejects_non_chats(self):
        """Adapter rejects json not in chats directory."""
        source = Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json")
        # Fixture is not in a 'chats' directory
        assert not gemini_cli.can_handle(source)

    def test_parse_extracts_conversation(self, tmp_path):
        """Parse yields a conversation with correct metadata."""
        # Copy fixture to a path with 'chats' in it
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        fixture = FIXTURES_DIR / "gemini_cli_minimal.json"
        dest = chats_dir / "test.json"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        convos = list(gemini_cli.parse(source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "gemini_cli::gemini-session-1"
        assert conv.harness.name == "gemini_cli"
        assert conv.harness.source == "google"

    def test_parse_extracts_prompts_and_responses(self, tmp_path):
        """Parse extracts prompts with their responses."""
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        fixture = FIXTURES_DIR / "gemini_cli_minimal.json"
        dest = chats_dir / "test.json"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        conv = list(gemini_cli.parse(source))[0]

        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert "List the files" in prompt.content[0].content.get("text", "")

        assert len(prompt.responses) == 1
        response = prompt.responses[0]
        assert response.model == "gemini-2.0-flash"

    def test_parse_extracts_tool_calls(self, tmp_path):
        """Parse extracts tool calls with results."""
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        fixture = FIXTURES_DIR / "gemini_cli_minimal.json"
        dest = chats_dir / "test.json"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        conv = list(gemini_cli.parse(source))[0]

        response = conv.prompts[0].responses[0]
        assert len(response.tool_calls) == 1

        tool_call = response.tool_calls[0]
        assert tool_call.tool_name == "list_files"
        assert tool_call.input.get("path") == "."
        assert tool_call.status == "success"

    def test_parse_extracts_usage(self, tmp_path):
        """Parse extracts token usage."""
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        fixture = FIXTURES_DIR / "gemini_cli_minimal.json"
        dest = chats_dir / "test.json"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        conv = list(gemini_cli.parse(source))[0]

        response = conv.prompts[0].responses[0]
        assert response.usage is not None
        assert response.usage.input_tokens == 50
        assert response.usage.output_tokens == 30

    def test_parse_extracts_thinking(self, tmp_path):
        """Parse extracts thinking/thoughts blocks."""
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        fixture = FIXTURES_DIR / "gemini_cli_minimal.json"
        dest = chats_dir / "test.json"
        dest.write_text(fixture.read_text())

        source = Source(kind="file", location=dest)
        conv = list(gemini_cli.parse(source))[0]

        response = conv.prompts[0].responses[0]
        thinking_blocks = [b for b in response.content if b.block_type == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0].content.get("subject") == "Planning"
