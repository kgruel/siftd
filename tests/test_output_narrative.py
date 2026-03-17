from dataclasses import dataclass, field

from siftd.output.narrative import render_narrative_blocks


@dataclass
class _ToolCall:
    tool_name: str
    count: int = 1
    status: str = "success"
    input: str | None = None
    result: str | None = None


@dataclass
class _Block:
    block_type: str
    content: str | None = None
    tool_calls: list[_ToolCall] = field(default_factory=list)


def test_render_narrative_blocks_hides_tool_payloads_by_default():
    lines = render_narrative_blocks(
        [
            _Block(
                block_type="tool_calls",
                tool_calls=[_ToolCall("shell.execute", input="git status", result="clean")],
            )
        ],
        chars_limit=200,
        tool_chars=120,
    )

    assert lines == ["    → shell.execute"]


def test_render_narrative_blocks_shows_tool_payloads_when_requested():
    lines = render_narrative_blocks(
        [
            _Block(
                block_type="tool_calls",
                tool_calls=[_ToolCall("shell.execute", input="git status", result="clean")],
            )
        ],
        chars_limit=200,
        tool_chars=120,
        show_tool_content=True,
    )

    assert lines == [
        "    → shell.execute",
        "      input: git status",
        "      ← clean",
    ]
