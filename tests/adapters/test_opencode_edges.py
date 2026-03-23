from siftd.adapters import opencode


def test_part_to_content_block_tool_type_returns_none():
    assert opencode._part_to_content_block({"type": "tool"}) is None
