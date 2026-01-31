"""Tests for conversation log adapters.

Each test uses a minimal fixture file to verify:
- can_handle() recognizes the file format
- parse() yields Conversation with expected structure
- Prompts, responses, and tool calls are extracted correctly
"""

from pathlib import Path
from types import ModuleType

import pytest
from conftest import FIXTURES_DIR

from siftd.adapters import aider, claude_code, codex_cli, gemini_cli
from siftd.adapters.validation import ADAPTER_INTERFACE_VERSION, validate_adapter
from siftd.domain.source import Source


class TestValidateAdapter:
    """Tests for adapter validation logic."""

    def _make_valid_adapter(self, version: int = ADAPTER_INTERFACE_VERSION) -> ModuleType:
        """Create a mock adapter module with all required attributes."""
        module = ModuleType("test_adapter")
        module.ADAPTER_INTERFACE_VERSION = version
        module.NAME = "test"
        module.DEFAULT_LOCATIONS = []
        module.DEDUP_STRATEGY = "file"
        module.HARNESS_SOURCE = "test"
        module.discover = lambda locations=None: []
        module.can_handle = lambda source: False
        module.parse = lambda source: iter([])
        return module

    def test_valid_adapter_passes(self):
        """Adapter with correct version passes validation."""
        module = self._make_valid_adapter(ADAPTER_INTERFACE_VERSION)
        assert validate_adapter(module, "test") is None

    def test_version_mismatch_returns_error(self):
        """Adapter with wrong version returns error."""
        module = self._make_valid_adapter(version=999)
        error = validate_adapter(module, "test-adapter")
        assert error is not None
        assert "incompatible interface version 999" in error
        assert f"expected {ADAPTER_INTERFACE_VERSION}" in error

    def test_version_zero_returns_error(self):
        """Adapter with version 0 returns error."""
        module = self._make_valid_adapter(version=0)
        error = validate_adapter(module, "old-adapter")
        assert error is not None
        assert "incompatible interface version 0" in error

    def test_future_version_returns_error(self):
        """Adapter with future version returns error."""
        future_version = ADAPTER_INTERFACE_VERSION + 1
        module = self._make_valid_adapter(version=future_version)
        error = validate_adapter(module, "future-adapter")
        assert error is not None
        assert f"incompatible interface version {future_version}" in error


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

    @pytest.fixture
    def codex_source(self, tmp_path):
        """Copy codex fixture to a path with 'sessions' in it (required by adapter)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        dest = sessions_dir / "test.jsonl"
        dest.write_text((FIXTURES_DIR / "codex_cli_minimal.jsonl").read_text())
        return Source(kind="file", location=dest)

    def test_can_handle_jsonl_in_sessions(self):
        """Adapter handles .jsonl files in sessions path."""
        source = Source(kind="file", location=Path("/mock/sessions/test.jsonl"))
        assert codex_cli.can_handle(source)

    def test_can_handle_rejects_non_sessions(self):
        """Adapter rejects jsonl not in sessions path."""
        source = Source(kind="file", location=FIXTURES_DIR / "codex_cli_minimal.jsonl")
        assert not codex_cli.can_handle(source)

    def test_parse_extracts_conversation(self, codex_source):
        """Parse yields a conversation with correct metadata."""
        convos = list(codex_cli.parse(codex_source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "codex_cli::codex-session-1"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "codex_cli"
        assert conv.harness.source == "openai"

    def test_parse_extracts_prompts_and_responses(self, codex_source):
        """Parse extracts prompts with their responses."""
        conv = list(codex_cli.parse(codex_source))[0]

        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert "Run ls" in prompt.content[0].content.get("text", "")

        assert len(prompt.responses) == 1

    def test_parse_extracts_tool_calls(self, codex_source):
        """Parse extracts tool calls with results."""
        conv = list(codex_cli.parse(codex_source))[0]

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
        assert not gemini_cli.can_handle(source)

    @pytest.fixture
    def gemini_source(self, tmp_path):
        """Copy gemini fixture to a path with 'chats' in it (required by adapter)."""
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        dest = chats_dir / "test.json"
        dest.write_text((FIXTURES_DIR / "gemini_cli_minimal.json").read_text())
        return Source(kind="file", location=dest)

    def test_parse_extracts_conversation(self, gemini_source):
        """Parse yields a conversation with correct metadata."""
        convos = list(gemini_cli.parse(gemini_source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "gemini_cli::gemini-session-1"
        assert conv.harness.name == "gemini_cli"
        assert conv.harness.source == "google"

    def test_parse_extracts_prompts_and_responses(self, gemini_source):
        """Parse extracts prompts with their responses."""
        conv = list(gemini_cli.parse(gemini_source))[0]

        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert "List the files" in prompt.content[0].content.get("text", "")

        assert len(prompt.responses) == 1
        response = prompt.responses[0]
        assert response.model == "gemini-2.0-flash"

    def test_parse_extracts_tool_calls(self, gemini_source):
        """Parse extracts tool calls with results."""
        conv = list(gemini_cli.parse(gemini_source))[0]

        response = conv.prompts[0].responses[0]
        assert len(response.tool_calls) == 1

        tool_call = response.tool_calls[0]
        assert tool_call.tool_name == "list_files"
        assert tool_call.input.get("path") == "."
        assert tool_call.status == "success"

    def test_parse_extracts_usage(self, gemini_source):
        """Parse extracts token usage."""
        conv = list(gemini_cli.parse(gemini_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.usage is not None
        assert response.usage.input_tokens == 50
        assert response.usage.output_tokens == 30

    def test_parse_extracts_thinking(self, gemini_source):
        """Parse extracts thinking/thoughts blocks."""
        conv = list(gemini_cli.parse(gemini_source))[0]

        response = conv.prompts[0].responses[0]
        thinking_blocks = [b for b in response.content if b.block_type == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0].content.get("subject") == "Planning"


class TestAiderAdapter:
    """Tests for the Aider adapter."""

    def test_can_handle_chat_history(self):
        """Adapter handles .aider.chat.history.md files."""
        source = Source(kind="file", location=Path("/project/.aider.chat.history.md"))
        assert aider.can_handle(source)

    def test_can_handle_rejects_other_md(self):
        """Adapter rejects non-aider markdown files."""
        source = Source(kind="file", location=Path("/project/README.md"))
        assert not aider.can_handle(source)

    def test_can_handle_rejects_non_file(self):
        """Adapter rejects non-file sources."""
        source = Source(kind="directory", location=Path("/project"))
        assert not aider.can_handle(source)

    def test_parse_yields_multiple_sessions(self):
        """Parse yields one conversation per session header."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        convos = list(aider.parse(source))

        assert len(convos) == 2

    def test_parse_first_session_metadata(self):
        """First session has correct metadata."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        conv = list(aider.parse(source))[0]

        assert conv.external_id.startswith("aider::")
        assert "2025-07-15 14:32:01" in conv.external_id
        assert conv.started_at == "2025-07-15T14:32:01"
        assert conv.harness.name == "aider"
        assert conv.harness.source == "multi"
        # workspace_path is the fixture directory
        assert conv.workspace_path == str(FIXTURES_DIR)

    def test_parse_extracts_prompts(self):
        """Parse extracts user prompts from #### lines."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        conv = list(aider.parse(source))[0]

        assert len(conv.prompts) == 2

        # First prompt: single line
        p0 = conv.prompts[0]
        assert len(p0.content) == 1
        assert "write a hello world script" in p0.content[0].content["text"]

        # Second prompt: multi-line (joined from two #### lines)
        p1 = conv.prompts[1]
        assert "now add a greeting function" in p1.content[0].content["text"]
        assert "that takes a name parameter" in p1.content[0].content["text"]

    def test_parse_extracts_responses(self):
        """Parse extracts assistant responses."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        conv = list(aider.parse(source))[0]

        # First prompt should have a response
        p0 = conv.prompts[0]
        assert len(p0.responses) >= 1
        resp = p0.responses[0]
        text_blocks = [b for b in resp.content if b.block_type == "text"]
        assert len(text_blocks) >= 1
        assert "hello world" in text_blocks[0].content["text"].lower()

    def test_parse_extracts_tool_output(self):
        """Parse extracts tool output from > lines."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        conv = list(aider.parse(source))[0]

        # First prompt's response chain should have tool_output blocks
        p0 = conv.prompts[0]
        all_blocks = []
        for resp in p0.responses:
            all_blocks.extend(resp.content)
        tool_blocks = [b for b in all_blocks if b.block_type == "tool_output"]
        assert len(tool_blocks) >= 1
        tool_text = tool_blocks[0].content["text"]
        assert "Applied edit to hello.py" in tool_text

    def test_parse_extracts_cost_attributes(self):
        """Parse extracts approximate cost from token/cost lines."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        conv = list(aider.parse(source))[0]

        # Find a response with cost attributes
        p0 = conv.prompts[0]
        resp_with_cost = None
        for resp in p0.responses:
            if resp.attributes.get("approx_cost"):
                resp_with_cost = resp
                break

        assert resp_with_cost is not None
        assert resp_with_cost.attributes["approx_cost"] == "0.01"
        assert resp_with_cost.attributes["approx_input_tokens"] == "2100"
        assert resp_with_cost.attributes["approx_output_tokens"] == "256"

    def test_parse_second_session(self):
        """Second session is parsed independently."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        convos = list(aider.parse(source))
        conv2 = convos[1]

        assert "2025-07-15 15:10:00" in conv2.external_id
        assert conv2.started_at == "2025-07-15T15:10:00"
        assert len(conv2.prompts) == 1
        assert "fix the bug in auth.py" in conv2.prompts[0].content[0].content["text"]

    def test_parse_empty_file(self, tmp_path):
        """Parse yields nothing for an empty file."""
        empty = tmp_path / ".aider.chat.history.md"
        empty.write_text("")
        source = Source(kind="file", location=empty)
        assert list(aider.parse(source)) == []

    def test_parse_session_with_no_messages(self, tmp_path):
        """Parse skips sessions that have only a header and no messages."""
        f = tmp_path / ".aider.chat.history.md"
        f.write_text("\n# aider chat started at 2025-01-01 00:00:00\n\n")
        source = Source(kind="file", location=f)
        assert list(aider.parse(source)) == []

    def test_external_id_stable_across_calls(self):
        """External IDs are deterministic for the same file."""
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        ids1 = [c.external_id for c in aider.parse(source)]
        ids2 = [c.external_id for c in aider.parse(source)]
        assert ids1 == ids2

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("4.5k", 4500),
            ("1.2k", 1200),
            ("256", 256),
            ("1.5M", 1_500_000),
            ("bad", None),
        ],
        ids=["4.5k", "1.2k", "plain-256", "1.5M", "bad-input"],
    )
    def test_parse_token_count_helper(self, raw, expected):
        """Token count parser handles k/m suffixes."""
        assert aider._parse_token_count(raw) == expected
