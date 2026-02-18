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

from siftd.adapters import aider, claude_code, codex_cli, copilot_cli, gemini_cli, opencode, pi_agent, vscode
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

    def test_parse_extracts_usage(self, codex_source):
        """Parse extracts token usage when token_count events are present."""
        conv = list(codex_cli.parse(codex_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.usage is not None
        assert response.usage.input_tokens == 120
        assert response.usage.output_tokens == 45
        assert response.attributes.get("cache_read_input_tokens") == "10"


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


class TestVscodeAdapter:
    """Tests for the VSCode adapter."""

    @pytest.fixture
    def vscode_dir(self, tmp_path):
        """Set up VSCode workspace directory structure with fixture."""
        import json
        import shutil

        hash_dir = tmp_path / "abc123hash"
        chat_dir = hash_dir / "chatSessions"
        chat_dir.mkdir(parents=True)

        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", chat_dir / "a1b2c3d4.json")

        workspace_json = hash_dir / "workspace.json"
        workspace_json.write_text(json.dumps({"folder": "file:///test/workspace"}))

        return Source(kind="file", location=chat_dir / "a1b2c3d4.json")

    def test_can_handle_json_in_chat_sessions(self):
        """Adapter handles .json files in chatSessions directory."""
        source = Source(kind="file", location=Path("/mock/chatSessions/test.json"))
        assert vscode.can_handle(source)

    def test_can_handle_rejects_non_chat_sessions(self):
        """Adapter rejects json not in chatSessions directory."""
        source = Source(kind="file", location=FIXTURES_DIR / "vscode_minimal.json")
        assert not vscode.can_handle(source)

    def test_can_handle_rejects_non_json(self):
        """Adapter rejects non-json files."""
        source = Source(kind="file", location=Path("/mock/chatSessions/test.txt"))
        assert not vscode.can_handle(source)

    def test_can_handle_rejects_non_file(self):
        """Adapter rejects non-file sources."""
        source = Source(kind="directory", location=Path("/mock/chatSessions"))
        assert not vscode.can_handle(source)

    def test_parse_extracts_conversation(self, vscode_dir):
        """Parse yields a conversation with correct metadata."""
        convos = list(vscode.parse(vscode_dir))
        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "vscode::a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "vscode"
        assert conv.harness.source == "multi"

    def test_parse_extracts_timestamps(self, vscode_dir):
        """Parse converts unix ms timestamps to ISO."""
        conv = list(vscode.parse(vscode_dir))[0]
        assert conv.started_at is not None
        assert "2024-02-15" in conv.started_at
        assert conv.ended_at is not None

    def test_parse_extracts_prompts_and_responses(self, vscode_dir):
        """Parse extracts prompts with their responses."""
        conv = list(vscode.parse(vscode_dir))[0]
        assert len(conv.prompts) == 2

        p0 = conv.prompts[0]
        assert "read a file" in p0.content[0].content["text"]
        assert len(p0.responses) == 1
        assert p0.responses[0].model == "gpt-4o"

        p1 = conv.prompts[1]
        assert "List the files" in p1.content[0].content["text"]
        assert len(p1.responses) == 1
        assert p1.responses[0].model == "claude-3.5-sonnet"

    def test_parse_extracts_markdown_content(self, vscode_dir):
        """Parse converts markdownContent parts to text blocks."""
        conv = list(vscode.parse(vscode_dir))[0]
        response = conv.prompts[0].responses[0]
        text_blocks = [b for b in response.content if b.block_type == "text"]
        assert len(text_blocks) == 1
        assert "open()" in text_blocks[0].content["text"]

    def test_parse_extracts_tool_calls(self, vscode_dir):
        """Parse extracts tool calls from toolInvocationSerialized parts."""
        conv = list(vscode.parse(vscode_dir))[0]
        response = conv.prompts[1].responses[0]
        assert len(response.tool_calls) == 1

        tc = response.tool_calls[0]
        assert tc.tool_name == "listFiles"
        assert tc.input == {"path": "."}
        assert tc.result == {"files": ["README.md", "src/", "tests/"]}
        assert tc.status == "success"

    def test_parse_extracts_text_edit_blocks(self, vscode_dir):
        """Parse preserves textEditGroup as content blocks."""
        conv = list(vscode.parse(vscode_dir))[0]
        response = conv.prompts[1].responses[0]
        edit_blocks = [b for b in response.content if b.block_type == "text_edit"]
        assert len(edit_blocks) == 1

    def test_parse_no_usage(self, vscode_dir):
        """Parse sets usage to None (not available in VSCode format)."""
        conv = list(vscode.parse(vscode_dir))[0]
        for prompt in conv.prompts:
            for response in prompt.responses:
                assert response.usage is None

    def test_parse_workspace_missing_workspace_json(self, tmp_path):
        """Parse sets workspace_path=None when workspace.json missing."""
        import shutil

        hash_dir = tmp_path / "nohash"
        chat_dir = hash_dir / "chatSessions"
        chat_dir.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", chat_dir / "test.json")

        source = Source(kind="file", location=chat_dir / "test.json")
        conv = list(vscode.parse(source))[0]
        assert conv.workspace_path is None

    def test_parse_empty_requests(self, tmp_path):
        """Parse yields nothing for session with no requests."""
        import json

        hash_dir = tmp_path / "empty"
        chat_dir = hash_dir / "chatSessions"
        chat_dir.mkdir(parents=True)

        session = {"version": 3, "sessionId": "empty-1", "creationDate": 1708012345678, "requests": []}
        (chat_dir / "empty.json").write_text(json.dumps(session))

        source = Source(kind="file", location=chat_dir / "empty.json")
        assert list(vscode.parse(source)) == []

    def test_parse_structured_message(self, tmp_path):
        """Parse handles structured message objects (IParsedChatRequest)."""
        import json

        hash_dir = tmp_path / "structured"
        chat_dir = hash_dir / "chatSessions"
        chat_dir.mkdir(parents=True)

        session = {
            "version": 3,
            "sessionId": "struct-1",
            "creationDate": 1708012345678,
            "requests": [{
                "requestId": "r1",
                "message": {"text": "Hello from structured"},
                "variableData": {},
                "timestamp": 1708012345678,
                "modelId": "gpt-4o",
                "response": [{"kind": "markdownContent", "content": {"value": "Hi"}}],
                "responseId": "resp-1",
            }],
        }
        (chat_dir / "struct.json").write_text(json.dumps(session))

        source = Source(kind="file", location=chat_dir / "struct.json")
        conv = list(vscode.parse(source))[0]
        assert "Hello from structured" in conv.prompts[0].content[0].content["text"]


class TestVscodeAdapterJsonl:
    """Tests for VSCode adapter JSONL (patch-based) format."""

    @pytest.fixture
    def vscode_jsonl_dir(self, tmp_path):
        """Set up VSCode workspace directory structure with JSONL fixture."""
        import json
        import shutil

        hash_dir = tmp_path / "abc123hash"
        chat_dir = hash_dir / "chatSessions"
        chat_dir.mkdir(parents=True)

        shutil.copy(FIXTURES_DIR / "vscode_minimal.jsonl", chat_dir / "session.jsonl")

        workspace_json = hash_dir / "workspace.json"
        workspace_json.write_text(json.dumps({"folder": "file:///test/workspace"}))

        return Source(kind="file", location=chat_dir / "session.jsonl")

    def test_can_handle_jsonl_in_chat_sessions(self):
        """Adapter handles .jsonl files in chatSessions directory."""
        source = Source(kind="file", location=Path("/mock/chatSessions/test.jsonl"))
        assert vscode.can_handle(source)

    def test_can_handle_rejects_jsonl_outside_chat_sessions(self):
        """Adapter rejects .jsonl not in chatSessions directory."""
        source = Source(kind="file", location=FIXTURES_DIR / "vscode_minimal.jsonl")
        assert not vscode.can_handle(source)

    def test_parse_jsonl_extracts_conversation(self, vscode_jsonl_dir):
        """Parse reconstructs conversation from JSONL patches."""
        convos = list(vscode.parse(vscode_jsonl_dir))
        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "vscode::jsonl-session-1234"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "vscode"

    def test_parse_jsonl_extracts_timestamps(self, vscode_jsonl_dir):
        """JSONL parse converts unix ms timestamps to ISO."""
        conv = list(vscode.parse(vscode_jsonl_dir))[0]
        assert conv.started_at is not None
        assert "2024-02-15" in conv.started_at
        assert conv.ended_at is not None

    def test_parse_jsonl_extracts_prompts(self, vscode_jsonl_dir):
        """JSONL parse reconstructs prompts from appended requests."""
        conv = list(vscode.parse(vscode_jsonl_dir))[0]
        assert len(conv.prompts) == 2

        p0 = conv.prompts[0]
        assert "read a file" in p0.content[0].content["text"]
        assert p0.responses[0].model == "gpt-4o"

        p1 = conv.prompts[1]
        assert "List the files" in p1.content[0].content["text"]
        assert p1.responses[0].model == "claude-3.5-sonnet"

    def test_parse_jsonl_appends_response_parts(self, vscode_jsonl_dir):
        """JSONL parse accumulates response parts from multiple patches."""
        conv = list(vscode.parse(vscode_jsonl_dir))[0]

        # First request: initial mcpServersStarting + appended markdownContent
        r0 = conv.prompts[0].responses[0]
        text_blocks = [b for b in r0.content if b.block_type == "text"]
        assert len(text_blocks) == 1
        assert "open()" in text_blocks[0].content["text"]

    def test_parse_jsonl_extracts_tool_calls(self, vscode_jsonl_dir):
        """JSONL parse extracts tool calls from appended response parts."""
        conv = list(vscode.parse(vscode_jsonl_dir))[0]
        r1 = conv.prompts[1].responses[0]
        assert len(r1.tool_calls) == 1

        tc = r1.tool_calls[0]
        assert tc.tool_name == "listFiles"
        assert tc.input == {"path": "."}
        assert tc.result == {"files": ["README.md", "src/"]}

    def test_parse_jsonl_empty_session(self, tmp_path):
        """JSONL with no requests yields nothing."""
        hash_dir = tmp_path / "empty"
        chat_dir = hash_dir / "chatSessions"
        chat_dir.mkdir(parents=True)

        content = '{"kind":0,"v":{"version":3,"sessionId":"empty","creationDate":1708012345678,"requests":[]}}\n'
        (chat_dir / "empty.jsonl").write_text(content)

        source = Source(kind="file", location=chat_dir / "empty.jsonl")
        assert list(vscode.parse(source)) == []

    def test_replay_set_at_path(self):
        """_set_at_path sets nested values correctly."""
        obj = {"requests": [{"response": [], "result": None}]}
        vscode._set_at_path(obj, ["requests", 0, "result"], {"ok": True})
        assert obj["requests"][0]["result"] == {"ok": True}

    def test_replay_append_at_path(self):
        """_append_at_path extends arrays correctly."""
        obj = {"requests": [{"response": []}]}
        vscode._append_at_path(obj, ["requests", 0, "response"], [{"kind": "text"}])
        assert len(obj["requests"][0]["response"]) == 1

    def test_replay_append_to_root_array(self):
        """_append_at_path works for top-level arrays like requests."""
        obj = {"requests": []}
        vscode._append_at_path(obj, ["requests"], [{"requestId": "r1"}])
        assert len(obj["requests"]) == 1
        assert obj["requests"][0]["requestId"] == "r1"

    def test_replay_set_invalid_path_is_noop(self):
        """_set_at_path silently ignores invalid paths."""
        obj = {"requests": []}
        vscode._set_at_path(obj, ["requests", 99, "result"], "value")
        assert obj == {"requests": []}


class TestPiAgentAdapter:
    """Tests for the Pi Coding Agent adapter."""

    @pytest.fixture
    def pi_source(self, tmp_path):
        """Copy Pi Agent fixture to a path with .pi/agent/sessions in it."""
        sessions_dir = tmp_path / ".pi" / "agent" / "sessions" / "--test--"
        sessions_dir.mkdir(parents=True)
        dest = sessions_dir / "20240310_test.jsonl"
        dest.write_text((FIXTURES_DIR / "pi_agent_minimal.jsonl").read_text())
        return Source(kind="file", location=dest)

    def test_can_handle_jsonl_in_pi_sessions(self):
        """Adapter handles .jsonl files in .pi/agent/sessions path."""
        source = Source(kind="file", location=Path("/mock/.pi/agent/sessions/test.jsonl"))
        assert pi_agent.can_handle(source)

    def test_can_handle_rejects_non_pi_path(self):
        """Adapter rejects jsonl not in .pi/agent/sessions path."""
        source = Source(kind="file", location=FIXTURES_DIR / "pi_agent_minimal.jsonl")
        assert not pi_agent.can_handle(source)

    def test_can_handle_rejects_non_jsonl(self):
        """Adapter rejects non-jsonl files."""
        source = Source(kind="file", location=Path("/mock/.pi/agent/sessions/test.json"))
        assert not pi_agent.can_handle(source)

    def test_can_handle_rejects_non_file(self):
        """Adapter rejects non-file sources."""
        source = Source(kind="directory", location=Path("/mock/.pi/agent/sessions"))
        assert not pi_agent.can_handle(source)

    def test_parse_extracts_conversation(self, pi_source):
        """Parse yields a conversation with correct metadata."""
        convos = list(pi_agent.parse(pi_source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "pi_agent::pi-session-001"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "pi_agent"
        assert conv.harness.source == "multi"

    def test_parse_extracts_prompts_and_responses(self, pi_source):
        """Parse extracts prompts with their responses."""
        conv = list(pi_agent.parse(pi_source))[0]

        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert "Read the README" in prompt.content[0].content.get("text", "")

        # Should have 2 responses (one with tool call, one final)
        assert len(prompt.responses) == 2

    def test_parse_extracts_tool_calls(self, pi_source):
        """Parse extracts tool calls with results."""
        conv = list(pi_agent.parse(pi_source))[0]

        response = conv.prompts[0].responses[0]
        assert len(response.tool_calls) == 1

        tool_call = response.tool_calls[0]
        assert tool_call.tool_name == "Read"
        assert tool_call.input.get("file_path") == "/test/workspace/README.md"
        assert tool_call.status == "success"
        assert "Test Project" in str(tool_call.result)

    def test_parse_extracts_usage(self, pi_source):
        """Parse extracts token usage."""
        conv = list(pi_agent.parse(pi_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.usage is not None
        assert response.usage.input_tokens == 500
        assert response.usage.output_tokens == 120

    def test_parse_extracts_cache_tokens(self, pi_source):
        """Parse extracts cache token attributes."""
        conv = list(pi_agent.parse(pi_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.attributes.get("cache_read_input_tokens") == "50"
        assert response.attributes.get("cache_creation_input_tokens") == "10"

    def test_parse_extracts_cost(self, pi_source):
        """Parse extracts cost as attribute."""
        conv = list(pi_agent.parse(pi_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.attributes.get("cost") == "0.0111"

    def test_parse_extracts_thinking(self, pi_source):
        """Parse extracts thinking blocks."""
        conv = list(pi_agent.parse(pi_source))[0]

        response = conv.prompts[0].responses[0]
        thinking_blocks = [b for b in response.content if b.block_type == "thinking"]
        assert len(thinking_blocks) == 1
        assert "README" in thinking_blocks[0].content.get("text", "")

    def test_parse_extracts_model(self, pi_source):
        """Parse extracts model from model_change event."""
        conv = list(pi_agent.parse(pi_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.model == "claude-opus-4-6"

    def test_parse_empty_file(self, tmp_path):
        """Parse yields nothing for an empty file."""
        sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
        sessions_dir.mkdir(parents=True)
        empty = sessions_dir / "empty.jsonl"
        empty.write_text("")
        source = Source(kind="file", location=empty)
        assert list(pi_agent.parse(source)) == []


class TestOpenCodeAdapter:
    """Tests for the OpenCode adapter."""

    @pytest.fixture
    def opencode_source(self, tmp_path):
        """Create a minimal OpenCode SQLite database for testing."""
        import json
        import sqlite3

        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))

        conn.execute("""CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            directory TEXT,
            title TEXT,
            version INTEGER,
            time_created INTEGER,
            time_updated INTEGER
        )""")
        conn.execute("""CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )""")
        conn.execute("""CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )""")

        # Insert session
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ses_001", "proj_001", "/test/workspace", "Test Session", 1, 1710079200000, 1710079260000),
        )

        # Insert user message
        user_data = json.dumps({
            "role": "user",
            "time": {"created": 1710079210000},
            "summary": {"title": "Run the tests"},
        })
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("msg_001", "ses_001", 1710079210000, 1710079210000, user_data),
        )

        # Insert user part (text)
        user_part_data = json.dumps({"type": "text", "text": "Run the tests please"})
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part_001", "msg_001", "ses_001", 1710079210000, 1710079210000, user_part_data),
        )

        # Insert assistant message
        assistant_data = json.dumps({
            "role": "assistant",
            "time": {"created": 1710079220000, "completed": 1710079230000},
            "modelID": "claude-3-opus-20240229",
            "providerID": "anthropic",
            "cost": 0.025,
            "tokens": {"total": 680, "input": 500, "output": 120, "reasoning": 60,
                       "cache": {"read": 50, "write": 10}},
            "finish": "tool-calls",
        })
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("msg_002", "ses_001", 1710079220000, 1710079230000, assistant_data),
        )

        # Insert assistant text part
        text_part_data = json.dumps({"type": "text", "text": "I'll run the tests for you."})
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part_002", "msg_002", "ses_001", 1710079220000, 1710079220000, text_part_data),
        )

        # Insert assistant tool part
        tool_part_data = json.dumps({
            "type": "tool",
            "callID": "call-001",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "pytest", "description": "Run tests"},
                "output": "5 passed, 0 failed",
                "title": "Run tests",
                "time": {"start": 1710079225000, "end": 1710079228000},
            },
        })
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            ("part_003", "msg_002", "ses_001", 1710079225000, 1710079228000, tool_part_data),
        )

        conn.commit()
        conn.close()

        return Source(kind="sqlite", location=db_path)

    def test_can_handle_sqlite(self):
        """Adapter handles opencode.db SQLite sources."""
        source = Source(kind="sqlite", location=Path("/mock/opencode.db"))
        assert opencode.can_handle(source)

    def test_can_handle_rejects_other_db(self):
        """Adapter rejects non-opencode SQLite databases."""
        source = Source(kind="sqlite", location=Path("/mock/other.db"))
        assert not opencode.can_handle(source)

    def test_can_handle_rejects_non_sqlite(self):
        """Adapter rejects non-sqlite sources."""
        source = Source(kind="file", location=Path("/mock/opencode.db"))
        assert not opencode.can_handle(source)

    def test_parse_extracts_conversation(self, opencode_source):
        """Parse yields a conversation with correct metadata."""
        convos = list(opencode.parse(opencode_source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "opencode::ses_001"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "opencode"
        assert conv.harness.source == "multi"

    def test_parse_extracts_timestamps(self, opencode_source):
        """Parse converts epoch ms to ISO timestamps."""
        conv = list(opencode.parse(opencode_source))[0]
        assert conv.started_at is not None
        assert "2024-03-10" in conv.started_at

    def test_parse_extracts_prompts_and_responses(self, opencode_source):
        """Parse extracts prompts with their responses."""
        conv = list(opencode.parse(opencode_source))[0]

        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert "Run the tests" in prompt.content[0].content.get("text", "")

        assert len(prompt.responses) == 1

    def test_parse_extracts_tool_calls(self, opencode_source):
        """Parse extracts tool calls from tool parts."""
        conv = list(opencode.parse(opencode_source))[0]

        response = conv.prompts[0].responses[0]
        assert len(response.tool_calls) == 1

        tool_call = response.tool_calls[0]
        assert tool_call.tool_name == "bash"
        assert tool_call.input.get("command") == "pytest"
        assert tool_call.status == "success"
        assert "5 passed" in str(tool_call.result)

    def test_parse_extracts_usage(self, opencode_source):
        """Parse extracts token usage."""
        conv = list(opencode.parse(opencode_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.usage is not None
        assert response.usage.input_tokens == 500
        assert response.usage.output_tokens == 120

    def test_parse_extracts_cache_tokens(self, opencode_source):
        """Parse extracts cache token attributes."""
        conv = list(opencode.parse(opencode_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.attributes.get("cache_read_input_tokens") == "50"
        assert response.attributes.get("cache_creation_input_tokens") == "10"

    def test_parse_extracts_cost(self, opencode_source):
        """Parse extracts cost as attribute."""
        conv = list(opencode.parse(opencode_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.attributes.get("cost") == "0.025"

    def test_parse_extracts_model(self, opencode_source):
        """Parse extracts model from message data."""
        conv = list(opencode.parse(opencode_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.model == "claude-3-opus-20240229"

    def test_parse_empty_db(self, tmp_path):
        """Parse yields nothing for an empty database."""
        import sqlite3

        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT, version INTEGER, time_created INTEGER, time_updated INTEGER)")
        conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
        conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
        conn.commit()
        conn.close()

        source = Source(kind="sqlite", location=db_path)
        assert list(opencode.parse(source)) == []


class TestCopilotCliAdapter:
    """Tests for the Copilot CLI adapter."""

    @pytest.fixture
    def copilot_source(self, tmp_path):
        """Copy Copilot CLI fixture to a path with .copilot/session-state in it."""
        session_dir = tmp_path / ".copilot" / "session-state" / "test-uuid"
        session_dir.mkdir(parents=True)
        dest = session_dir / "events.jsonl"
        dest.write_text((FIXTURES_DIR / "copilot_cli_minimal.jsonl").read_text())
        return Source(kind="file", location=dest)

    def test_can_handle_events_jsonl_in_session_state(self):
        """Adapter handles events.jsonl files in .copilot/session-state path."""
        source = Source(kind="file", location=Path("/mock/.copilot/session-state/uuid/events.jsonl"))
        assert copilot_cli.can_handle(source)

    def test_can_handle_rejects_non_copilot_path(self):
        """Adapter rejects events.jsonl not in .copilot/session-state path."""
        source = Source(kind="file", location=FIXTURES_DIR / "copilot_cli_minimal.jsonl")
        assert not copilot_cli.can_handle(source)

    def test_can_handle_rejects_non_events_file(self):
        """Adapter rejects non-events.jsonl files even in correct path."""
        source = Source(kind="file", location=Path("/mock/.copilot/session-state/uuid/other.jsonl"))
        assert not copilot_cli.can_handle(source)

    def test_can_handle_rejects_non_file(self):
        """Adapter rejects non-file sources."""
        source = Source(kind="directory", location=Path("/mock/.copilot/session-state"))
        assert not copilot_cli.can_handle(source)

    def test_parse_extracts_conversation(self, copilot_source):
        """Parse yields a conversation with correct metadata."""
        convos = list(copilot_cli.parse(copilot_source))

        assert len(convos) == 1
        conv = convos[0]

        assert conv.external_id == "copilot_cli::copilot-session-001"
        assert conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "copilot_cli"
        assert conv.harness.source == "multi"

    def test_parse_extracts_branch(self, copilot_source):
        """Parse extracts branch from session.start context."""
        conv = list(copilot_cli.parse(copilot_source))[0]
        assert conv.branch == "main"

    def test_parse_extracts_prompts_and_responses(self, copilot_source):
        """Parse extracts prompts with their responses."""
        conv = list(copilot_cli.parse(copilot_source))[0]

        assert len(conv.prompts) == 1

        prompt = conv.prompts[0]
        assert len(prompt.content) == 1
        assert "List the files" in prompt.content[0].content.get("text", "")

        # Should have 2 responses (one with tool call, one final)
        assert len(prompt.responses) == 2

    def test_parse_extracts_tool_calls(self, copilot_source):
        """Parse extracts tool calls with results."""
        conv = list(copilot_cli.parse(copilot_source))[0]

        response = conv.prompts[0].responses[0]
        assert len(response.tool_calls) == 1

        tool_call = response.tool_calls[0]
        assert tool_call.tool_name == "bash"
        assert tool_call.input.get("command") == "ls -la"
        assert tool_call.status == "success"
        assert "README.md" in str(tool_call.result)

    def test_parse_extracts_reasoning(self, copilot_source):
        """Parse extracts reasoning text as thinking blocks."""
        conv = list(copilot_cli.parse(copilot_source))[0]

        response = conv.prompts[0].responses[0]
        thinking_blocks = [b for b in response.content if b.block_type == "thinking"]
        assert len(thinking_blocks) == 1
        assert "files" in thinking_blocks[0].content.get("text", "").lower()

    def test_parse_extracts_model(self, copilot_source):
        """Parse extracts model from session.model_change event."""
        conv = list(copilot_cli.parse(copilot_source))[0]

        response = conv.prompts[0].responses[0]
        assert response.model == "claude-haiku-4.5"

    def test_parse_no_usage(self, copilot_source):
        """Parse sets usage to None (not available in Copilot CLI format)."""
        conv = list(copilot_cli.parse(copilot_source))[0]
        for prompt in conv.prompts:
            for response in prompt.responses:
                assert response.usage is None

    def test_parse_empty_file(self, tmp_path):
        """Parse yields nothing for an empty file."""
        session_dir = tmp_path / ".copilot" / "session-state" / "uuid"
        session_dir.mkdir(parents=True)
        empty = session_dir / "events.jsonl"
        empty.write_text("")
        source = Source(kind="file", location=empty)
        assert list(copilot_cli.parse(source)) == []
