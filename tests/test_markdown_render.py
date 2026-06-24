"""Tests for terminal markdown rendering of transcript bodies."""

from __future__ import annotations

import io

import pytest

from siftd.output.markdown_render import render_markdown


@pytest.fixture
def ds():
    from painted import use_theme

    from siftd.output.theme import domain_styles, siftd_theme

    with use_theme(siftd_theme):
        yield domain_styles()


def _text(items) -> str:
    """Render the Line/Block items to plain (no-ANSI) text for assertions."""
    from painted import Line, join_vertical, print_block

    from siftd.output.painted_bridge import _lines_to_block

    parts: list = []
    run: list = []
    for it in items:
        if isinstance(it, Line):
            run.append(it)
        else:
            if run:
                parts.append(_lines_to_block(run))
                run = []
            parts.append(it)
    if run:
        parts.append(_lines_to_block(run))
    if not parts:
        return ""
    block = join_vertical(*parts) if len(parts) > 1 else parts[0]
    buf = io.StringIO()
    print_block(block, buf, use_ansi=False)
    return buf.getvalue()


def _render(src, ds, width=100, **kw) -> str:
    from painted import use_theme

    from siftd.output.theme import siftd_theme

    with use_theme(siftd_theme):
        return _text(render_markdown(src, ds, width, **kw))


def test_empty_input_returns_nothing(ds):
    assert render_markdown("", ds, 80) == []
    assert render_markdown("   \n  ", ds, 80) == []


def test_paragraph_strips_inline_markdown(ds):
    out = _render("A **bold** and `code` and *em* word.", ds)
    assert "bold" in out and "code" in out and "em" in out
    # The markdown syntax itself is gone — rendered as style, not source.
    assert "**" not in out
    assert "`code`" not in out


def test_paragraph_reflows_and_wraps(ds):
    # softbreaks (single newlines) collapse, then the paragraph word-wraps to width.
    src = "one two three four five six seven eight nine ten eleven twelve"
    out = _render(src, ds, width=30)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) >= 2  # wrapped across multiple lines
    assert all(len(ln) <= 30 for ln in lines)
    assert "one" in out and "twelve" in out


def test_heading_strips_hashes(ds):
    out = _render("## Overview\n\nbody", ds)
    assert "Overview" in out
    assert "##" not in out


def test_table_renders_without_pipe_source(ds):
    src = "| Slice | What |\n|---|---|\n| 1 | a bump |\n| 2 | a rail |"
    out = _render(src, ds)
    assert "Slice" in out and "What" in out
    assert "bump" in out and "rail" in out
    # The markdown table delimiter row is gone; a real table rule is drawn instead.
    assert "|---|" not in out


def test_fenced_code_preserved_without_backticks(ds):
    out = _render("```python\nx = 1\nprint(x)\n```", ds)
    assert "x = 1" in out and "print(x)" in out
    assert "```" not in out


def test_bullet_list_ascii_marker(ds):
    out = _render("- first\n- second", ds, ascii_mode=True)
    assert "- first" in out
    assert "- second" in out


def test_ordered_list_numbers(ds):
    out = _render("1. alpha\n2. beta", ds)
    assert "1. alpha" in out
    assert "2. beta" in out


def test_ascii_degradation_drops_unicode_glyphs(ds):
    src = "- item\n\n> quote\n\n---\n"
    out = _render(src, ds, ascii_mode=True)
    assert "•" not in out  # bullet degraded
    assert "─" not in out  # rule degraded
    assert "│" not in out  # quote gutter degraded
    assert "- item" in out
    assert "| quote" in out  # ascii quote gutter


def test_unicode_glyphs_when_capable(ds):
    out = _render("- item\n\n---\n", ds, ascii_mode=False)
    assert "•" in out
    assert "─" in out


def test_thematic_break_is_a_rule(ds):
    out = _render("a\n\n---\n\nb", ds, ascii_mode=True)
    assert "----" in out  # a run of dashes


def test_link_shows_text_and_url(ds):
    out = _render("see [the plan](http://x/plan)", ds)
    assert "the plan" in out
    assert "http://x/plan" in out


def test_blockquote_gutter(ds):
    out = _render("> quoted text", ds, ascii_mode=True)
    assert "| quoted text" in out


def test_parse_failure_falls_back_to_plain(ds, monkeypatch):
    import siftd.output.markdown_render as mr

    class _Boom:
        def __call__(self, _content):
            raise RuntimeError("boom")

    monkeypatch.setattr(mr, "_parser", lambda: _Boom())
    out = _render("plain content here", ds)
    assert "plain content here" in out


def test_code_block_in_list_item_not_dropped(ds):
    # Regression: a fenced code block inside a list item used to vanish.
    src = "- one\n\n  ```\n  CODE_IN_ITEM\n  ```\n\n- two"
    out = _render(src, ds, ascii_mode=True)
    assert "CODE_IN_ITEM" in out
    assert "- one" in out and "- two" in out


def test_loose_list_item_paragraphs_separated(ds):
    # Regression: two paragraphs in one item used to merge as "para Apara B".
    src = "- para A\n\n  para B"
    out = _render(src, ds, ascii_mode=True)
    assert "para Apara B" not in out
    assert "para A para B" in out


def test_multiline_html_block_keeps_rows_separate(ds):
    # Regression: a multi-line raw HTML block used to collapse into one Line
    # carrying embedded newlines. Each physical line should be its own row.
    src = "<details>\n<summary>x</summary>\n</details>"
    out = _render(src, ds)
    lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    assert "  <details>" in lines
    assert "  <summary>x</summary>" in lines
    assert "  </details>" in lines


def test_nested_list_deeper_indent(ds):
    src = "- top\n  - child"
    out = _render(src, ds, ascii_mode=True)
    lines = out.splitlines()
    top = next(ln for ln in lines if "top" in ln)
    child = next(ln for ln in lines if "child" in ln)
    assert child.index("- child") > top.index("- top")


def test_image_renders_alt_placeholder(ds):
    out = _render("![a cat](http://x/cat.png)", ds)
    assert "[image: a cat]" in out


def test_link_text_equals_url_shows_once(ds):
    out = _render("[http://x/y](http://x/y)", ds)
    assert "http://x/y" in out
    assert "(http://x/y)" not in out  # no duplicated url-in-parens suffix


def test_wide_table_under_ascii_has_no_ellipsis(ds):
    # The table-ellipsis crash-class: under ascii_mode the width budget is dropped
    # so painted never draws its hardcoded "…" on a strict-ASCII stream.
    headers = "| " + " | ".join(f"col{i}" for i in range(6)) + " |"
    rule = "|" + "|".join(["---"] * 6) + "|"
    row = "| " + " | ".join(f"value-{i}-xxxxxxxx" for i in range(6)) + " |"
    out = _render(f"{headers}\n{rule}\n{row}", ds, width=40, ascii_mode=True)
    assert "…" not in out
    assert "─" not in out
    assert "value-0-xxxxxxxx" in out
