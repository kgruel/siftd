"""WS5: web tags index surfaces element-kind breakdown, quiet for conv-only tags."""

from siftd.api.tags import TagInfo
from siftd.output.html_fmt import _tag_kind_breakdown, render_tags


def _tag(name, **counts):
    base = dict(
        conversation_count=0, workspace_count=0, tool_call_count=0,
        exchange_count=0, prompt_count=0, response_count=0,
    )
    base.update(counts)
    return TagInfo(name=name, description=None, created_at="2024-01-01", **base)


def test_breakdown_lists_element_kinds():
    t = _tag("docs:thing", response_count=3, conversation_count=1)
    assert _tag_kind_breakdown(t) == "3 responses, 1 conversation"


def test_breakdown_lists_block_kind():
    t = _tag("docs:thing", block_count=2, conversation_count=1)
    assert _tag_kind_breakdown(t) == "2 blocks, 1 conversation"


def test_breakdown_singular():
    t = _tag("docs:thing", response_count=1)
    assert _tag_kind_breakdown(t) == "1 response"


def test_breakdown_quiet_for_conversation_only():
    t = _tag("proj:x", conversation_count=5)
    assert _tag_kind_breakdown(t) == ""


def test_breakdown_quiet_for_empty():
    assert _tag_kind_breakdown(_tag("empty")) == ""


def test_render_tags_shows_breakdown_html():
    tags = [_tag("docs:thing", response_count=3, conversation_count=1)]
    html = render_tags(tags, list_base="/find", shell_base="/")
    assert 'class="idx-kinds"' in html
    assert "3 responses, 1 conversation" in html


def test_render_tags_hides_breakdown_for_conv_only():
    tags = [_tag("proj:x", conversation_count=5)]
    html = render_tags(tags, list_base="/find", shell_base="/")
    assert 'class="idx-kinds"' not in html
