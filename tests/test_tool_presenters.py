"""Tests for painted bridge tool-specific presenters and fidelity integration."""

import json

import pytest

from painted import Fidelity

from siftd.api.conversations import NarrativeBlock, ToolCallDetail
from siftd.domain.peek import PeekNarrativeBlock, PeekToolCall
from siftd.output.painted_bridge import render_narrative_block


def _block_to_lines(block) -> list[str]:
    """Extract plain text lines from a painted Block."""
    lines = []
    for y in range(block.height):
        lines.append("".join(cell.char for cell in block.row(y)).rstrip())
    return lines


def _render(blocks, *, fidelity=None, tool_chars=0):
    """Render narrative blocks and return plain text lines."""
    fidelity = fidelity or Fidelity(depth=1, visible=frozenset({"text", "tools"}))
    block = render_narrative_block(blocks, fidelity=fidelity, tool_chars=tool_chars)
    return _block_to_lines(block)


def _tool_block(tool_name, *, input=None, result=None, status="success", count=1):
    """Create a NarrativeBlock with a single tool call."""
    return NarrativeBlock(
        block_type="tool_calls",
        tool_calls=[ToolCallDetail(
            tool_name=tool_name,
            status=status,
            count=count,
            input=input,
            result=result,
        )],
    )


def _peek_tool_block(tool_name, *, input=None, result=None, status="success"):
    """Create a PeekNarrativeBlock with a single tool call."""
    return PeekNarrativeBlock(
        block_type="tool_calls",
        tool_calls=[PeekToolCall(
            tool_name=tool_name,
            input=input,
            result=result,
            status=status,
        )],
    )


# ---------------------------------------------------------------------------
# Shell.execute presenter
# ---------------------------------------------------------------------------

class TestShellExecutePresenter:
    def test_json_input_shows_command(self):
        block = _tool_block("shell.execute", input=json.dumps({"command": "git status"}))
        lines = _render([block])
        text = "\n".join(lines)
        assert "$ git status" in text

    def test_json_input_cmd_key(self):
        block = _tool_block("shell.execute", input=json.dumps({"cmd": "npm test"}))
        lines = _render([block])
        text = "\n".join(lines)
        assert "$ npm test" in text

    def test_plain_string_input(self):
        block = _tool_block("shell.execute", input="ls -la")
        lines = _render([block])
        text = "\n".join(lines)
        assert "$ ls -la" in text

    def test_result_with_exit_code_and_wall_time(self):
        block = _tool_block(
            "shell.execute",
            input=json.dumps({"command": "make"}),
            result=json.dumps({"exit_code": 0, "wall_time_seconds": 1.5, "output": "ok"}),
        )
        lines = _render([block])
        text = "\n".join(lines)
        assert "exit: 0" in text
        assert "1.5s" in text  # format: bare seconds without label

    def test_error_result_styling(self):
        block = _tool_block(
            "shell.execute",
            input=json.dumps({"command": "false"}),
            result=json.dumps({"exit_code": 1, "output": "command failed"}),
            status="error",
        )
        lines = _render([block])
        text = "\n".join(lines)
        assert "exit: 1" in text
        assert "command failed" in text

    def test_output_preview_limits_lines(self):
        long_output = "\n".join(f"line {i}" for i in range(20))
        block = _tool_block(
            "shell.execute",
            result=json.dumps({"output": long_output}),
        )
        lines = _render([block], tool_chars=120)
        text = "\n".join(lines)
        assert "line 0" in text
        assert "line 5" in text
        assert "... +14 more lines" in text
        assert "line 19" not in text

    def test_output_preview_unlimited_for_full_depth(self):
        long_output = "\n".join(f"line {i}" for i in range(20))
        block = _tool_block(
            "shell.execute",
            result=json.dumps({"output": long_output}),
        )
        fidelity = Fidelity(depth=3, visible=frozenset({"text", "tools"}))
        lines = _render([block], fidelity=fidelity)
        text = "\n".join(lines)
        assert "line 19" in text
        assert "more lines" not in text

    def test_peek_plain_string_input(self):
        block = _peek_tool_block("shell.execute", input="git diff")
        lines = _render([block])
        text = "\n".join(lines)
        assert "$ git diff" in text


# ---------------------------------------------------------------------------
# File.read presenter
# ---------------------------------------------------------------------------

class TestFileReadPresenter:
    def test_json_input_shows_path(self):
        block = _tool_block("file.read", input=json.dumps({"file_path": "/src/main.py"}))
        lines = _render([block])
        text = "\n".join(lines)
        assert "/src/main.py" in text

    def test_json_input_with_offset_limit(self):
        block = _tool_block("file.read", input=json.dumps({
            "file_path": "/src/main.py", "offset": 10, "limit": 50,
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "/src/main.py:10-59" in text

    def test_token_count_suffix(self):
        block = _tool_block(
            "file.read",
            input=json.dumps({"file_path": "/src/main.py"}),
            result=json.dumps({"original_token_count": 250}),
        )
        lines = _render([block])
        text = "\n".join(lines)
        assert "250 tokens" in text

    def test_plain_string_input(self):
        block = _peek_tool_block("file.read", input="src/config.py")
        lines = _render([block])
        text = "\n".join(lines)
        assert "src/config.py" in text

    def test_error_shows_message(self):
        block = _tool_block(
            "file.read",
            input=json.dumps({"file_path": "/missing.py"}),
            result=json.dumps({"error": "File not found"}),
            status="error",
        )
        lines = _render([block])
        text = "\n".join(lines)
        assert "/missing.py" in text
        assert "File not found" in text


# ---------------------------------------------------------------------------
# File.edit presenter
# ---------------------------------------------------------------------------

class TestFileEditPresenter:
    def test_json_input_shows_path_and_diff(self):
        block = _tool_block("file.edit", input=json.dumps({
            "file_path": "/src/main.py",
            "old_string": "def foo():",
            "new_string": "def bar():",
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "/src/main.py" in text
        assert "def foo():" in text
        assert "def bar():" in text

    def test_plain_string_input(self):
        block = _peek_tool_block("file.edit", input="/src/main.py")
        lines = _render([block])
        text = "\n".join(lines)
        assert "/src/main.py" in text


# ---------------------------------------------------------------------------
# File.write presenter
# ---------------------------------------------------------------------------

class TestFileWritePresenter:
    def test_json_input_shows_path_and_line_count(self):
        content = "line1\nline2\nline3\n"
        block = _tool_block("file.write", input=json.dumps({
            "file_path": "/src/new.py", "content": content,
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "/src/new.py" in text
        assert "3 lines" in text

    def test_plain_string_input(self):
        block = _peek_tool_block("file.write", input="/src/new.py")
        lines = _render([block])
        text = "\n".join(lines)
        assert "/src/new.py" in text


# ---------------------------------------------------------------------------
# Search.grep presenter
# ---------------------------------------------------------------------------

class TestSearchGrepPresenter:
    def test_json_input_shows_pattern_and_path(self):
        block = _tool_block("search.grep", input=json.dumps({
            "pattern": "TODO", "path": "/src",
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "/TODO/" in text
        assert "/src" in text

    def test_json_input_with_glob(self):
        block = _tool_block("search.grep", input=json.dumps({
            "pattern": "import", "path": "/src", "glob": "*.py",
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "/import/" in text
        assert "*.py" in text

    def test_output_preview(self):
        block = _tool_block("search.grep", result=json.dumps({
            "output": "main.py:10: TODO fix\nutils.py:20: TODO add",
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "main.py:10: TODO fix" in text


# ---------------------------------------------------------------------------
# File.glob presenter
# ---------------------------------------------------------------------------

class TestFileGlobPresenter:
    def test_json_input_shows_pattern(self):
        block = _tool_block("file.glob", input=json.dumps({
            "pattern": "**/*.py", "path": "/src",
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "**/*.py" in text
        assert "/src" in text


# ---------------------------------------------------------------------------
# ui.todo presenter
# ---------------------------------------------------------------------------

class TestTodoPresenter:
    def test_title_and_tasks(self):
        block = _tool_block("ui.todo", input=json.dumps({
            "title": "Implementation plan",
            "tasks": [
                {"description": "Write tests", "status": "done"},
                {"description": "Fix bug", "status": "in_progress"},
            ],
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "Implementation plan" in text
        assert "Write tests" in text
        assert "Fix bug" in text

    def test_plan_key_fallback(self):
        block = _tool_block("ui.todo", input=json.dumps({
            "title": "Steps",
            "plan": [{"step": "Inspect", "status": "in_progress"}],
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "Steps" in text
        assert "Inspect" in text


# ---------------------------------------------------------------------------
# Generic fallback presenter
# ---------------------------------------------------------------------------

class TestGenericPresenter:
    def test_unknown_tool_uses_generic(self):
        block = _tool_block("custom.tool", input=json.dumps({
            "query": "find things", "url": "https://example.com",
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "query: find things" in text

    def test_generic_result_extracts_output(self):
        block = _tool_block("custom.tool", result=json.dumps({
            "output": "result data", "exit_code": 0,
        }))
        lines = _render([block])
        text = "\n".join(lines)
        assert "result data" in text


# ---------------------------------------------------------------------------
# Fidelity integration
# ---------------------------------------------------------------------------

class TestFidelityIntegration:
    def test_tools_hidden_when_not_visible(self):
        block = _tool_block("shell.execute", input=json.dumps({"command": "ls"}))
        fidelity = Fidelity(depth=1, visible=frozenset({"text"}))  # tools not visible
        lines = _render([block], fidelity=fidelity)
        text = "\n".join(lines)
        assert "shell.execute" in text  # header always shows
        assert "$ ls" not in text  # content hidden

    def test_tools_visible_shows_content(self):
        block = _tool_block("shell.execute", input=json.dumps({"command": "ls"}))
        fidelity = Fidelity(depth=1, visible=frozenset({"text", "tools"}))
        lines = _render([block], fidelity=fidelity)
        text = "\n".join(lines)
        assert "$ ls" in text

    def test_empty_visible_hides_tool_content(self):
        block = _tool_block("shell.execute", input=json.dumps({"command": "ls"}))
        fidelity = Fidelity(depth=1)  # empty visible = nothing extra
        lines = _render([block], fidelity=fidelity)
        text = "\n".join(lines)
        # Tool header shown, but content ($ ls) hidden
        assert "shell.execute" in text
        assert "$ ls" not in text

    def test_density_truncates_text(self):
        # Representative prose (words, not one giant token) so the content-budget
        # "..." marker lands at a word boundary and survives the body word-wrap.
        text_block = NarrativeBlock(block_type="text", content="word " * 60)
        fidelity = Fidelity(depth=1, chars=80)
        lines = _render([text_block], fidelity=fidelity)
        text = "\n".join(lines)
        assert "..." in text
        assert len(text) < 300

    def test_density_truncates_over_width_single_token(self, monkeypatch):
        # The case that forced the test above to use prose: a single unbroken token
        # wider than the wrap width. The content budget still caps it, but the "..."
        # marker straddles the hard-split boundary — so undo the wrap before checking.
        monkeypatch.setenv("COLUMNS", "80")
        text_block = NarrativeBlock(block_type="text", content="A" * 200)
        lines = _render([text_block], fidelity=Fidelity(depth=1, chars=80))
        flat = "\n".join(lines).replace("\n", "").replace(" ", "")
        assert "..." in flat  # marker present once the wrap is undone
        assert flat.count("A") == 77  # capped to chars - len(suffix), not 200

    def test_density_zero_no_truncation(self):
        text_block = NarrativeBlock(block_type="text", content="A" * 200)
        fidelity = Fidelity(depth=1, chars=0)
        lines = _render([text_block], fidelity=fidelity)
        text = "\n".join(lines)
        assert "..." not in text

    def test_tool_density_derived_from_fidelity(self):
        """Default tool density is 120 chars when fidelity.chars is 0."""
        long_output = "x" * 200
        block = _tool_block(
            "shell.execute",
            input=json.dumps({"command": "echo hi"}),
            result=json.dumps({"output": long_output}),
        )
        fidelity = Fidelity(depth=1, visible=frozenset({"text", "tools"}), chars=0)
        lines = _render([block], fidelity=fidelity)
        text = "\n".join(lines)
        # Output should be truncated at default tool density (120 chars)
        assert "..." in text

    def test_full_depth_no_tool_truncation(self):
        """Depth=3 (full) means no tool truncation."""
        long_input = json.dumps({"command": "x" * 200})
        block = _tool_block("shell.execute", input=long_input)
        fidelity = Fidelity(depth=3, visible=frozenset({"text", "tools"}))
        lines = _render([block], fidelity=fidelity)
        text = "\n".join(lines)
        assert "..." not in text

    def test_thinking_block_always_renders(self):
        """Thinking blocks render when present — visibility is data-layer concern for now."""
        block = NarrativeBlock(block_type="thinking", content="Let me think...")
        fidelity = Fidelity(depth=1, visible=frozenset({"text"}))
        lines = _render([block], fidelity=fidelity)
        text = "\n".join(lines)
        # Thinking renders if present in data (data layer controls inclusion)
        assert "thinking" in text

    def test_tool_chars_override(self):
        """Explicit tool_chars parameter overrides fidelity-derived density."""
        block = _tool_block("shell.execute", input=json.dumps({"command": "echo hi"}))
        fidelity = Fidelity(depth=1, visible=frozenset({"text", "tools"}))
        lines_default = _render([block], fidelity=fidelity)
        lines_override = _render([block], fidelity=fidelity, tool_chars=80)
        # Both should render the command (just with different truncation)
        assert "$ echo hi" in "\n".join(lines_default)
        assert "$ echo hi" in "\n".join(lines_override)
