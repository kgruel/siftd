"""Render markdown transcript bodies onto painted Lines/Blocks for the terminal.

The terminal was historically the only human-facing surface that showed
transcript bodies as raw markdown *source* — literal ``##`` headings, ``**bold**``
runs, ``| pipe | tables |``, and fenced code blocks. This module parses the same
markdown the HTML emitter parses (mistune) and projects it onto painted spans, the
shared word-wrap, and the table atom — the same author-meaning -> painted-presents
transform ``HtmlEmitter`` performs, expressed against the terminal instead of HTML.

The parser lives here, not in painted, by design: painted renders trees -> Blocks;
it does not ingest foreign text. This package owns the markdown dialect its adapters
emit, so text -> tree is the consumer's job (as ``to_html`` consumers supply trees).

``render_markdown`` returns an ordered list of painted ``Line`` (line-shaped
elements: paragraphs, headings, lists, code, quotes, rules) and ``Block`` (tables)
items; the caller groups runs of Lines into Blocks and passes table Blocks through.
A parse failure degrades to plain wrapped lines — a transcript never breaks on
malformed (e.g. mid-truncation) markdown.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Line

    from siftd.output.theme import DomainStyles

_BASE_INDENT = "  "  # the transcript body column (matches the old raw-text indent)
_CODE_STEP = "  "  # fenced code sits one step deeper than its surrounding prose
_RULE_WIDTH = 24  # thematic-break rule length (a divider, not a full-width banner)


@lru_cache(maxsize=1)
def _parser():
    """The mistune AST parser, with the GFM table + strikethrough plugins.

    ``renderer=None`` yields the token tree (not HTML); the table plugin is what
    turns ``| a | b |`` into real ``table`` tokens instead of a pipe paragraph.
    """
    import mistune

    return mistune.create_markdown(renderer=None, plugins=["table", "strikethrough"])


def _pp():
    from painted import Block, Line, Span, Style, current_palette, join_vertical

    return Block, Line, Span, Style, current_palette, join_vertical


def render_markdown(
    content: str, ds: DomainStyles, width: int | None, *, ascii_mode: bool = False
) -> list:
    """Parse ``content`` as markdown and project it onto painted Lines/Blocks.

    ``width`` is the terminal width the body wraps within (the 2-space body indent
    is applied here); ``ascii_mode`` degrades glyphs (bullets/rules/quote gutters)
    to their ASCII forms for a non-Unicode target. Returns ``[]`` for empty input
    and falls back to plain wrapped lines if the parse raises.
    """
    if not content or not content.strip():
        return []
    try:
        tokens = _parser()(content)
    except Exception:
        return _plain_lines(content.strip(), ds, width)
    out: list = []
    _render_blocks(tokens, out, ds, width, ascii_mode, _BASE_INDENT)
    return _trim_trailing_blanks(out)


# --- block dispatch --------------------------------------------------------


def _render_blocks(
    tokens: list, out: list, ds: DomainStyles, width: int | None, ascii_mode: bool, indent: str
) -> None:
    for tok in tokens or []:
        t = tok.get("type")
        if t == "blank_line":
            continue
        before = len(out)
        if t == "paragraph":
            out.extend(_paragraph(tok, ds, width, ascii_mode, indent))
        elif t == "heading":
            out.extend(_heading(tok, ds, width, ascii_mode, indent))
        elif t == "block_code":
            out.extend(_code(tok, ds, indent))
        elif t == "list":
            out.extend(_list(tok, ds, width, ascii_mode, indent))
        elif t == "table":
            out.append(_table(tok, width, ascii_mode, indent))
        elif t == "thematic_break":
            out.append(_rule(ds, ascii_mode, indent))
        elif t == "block_quote":
            out.extend(_blockquote(tok, ds, width, ascii_mode, indent))
        else:  # block_text / block_html / unknown — emit raw text, never drop it
            raw = tok.get("raw") or _flatten_text(tok.get("children"))
            if raw:
                # Split on physical newlines so a multi-line raw block (e.g. an
                # HTML <details>/<table> island) stays one Line per row, not one
                # Line carrying embedded '\n' (which breaks the row invariant).
                for ln in raw.rstrip("\n").split("\n"):
                    out.extend(_paragraph_text(ln, ds, width, indent))
        if len(out) > before:  # only separate blocks that actually emitted output
            out.append(_blank())


# --- block renderers -------------------------------------------------------


def _paragraph(tok, ds, width, ascii_mode, indent):
    _, _, _, Style, _, _ = _pp()
    segs = _inline(tok.get("children"), ds, ds.assistant, ascii_mode)
    return _wrap_segments(segs, width, [(indent, Style())], [(indent, Style())])


def _paragraph_text(text, ds, width, indent):
    _, _, _, Style, _, _ = _pp()
    return _wrap_segments([(text, ds.assistant)], width, [(indent, Style())], [(indent, Style())])


def _heading(tok, ds, width, ascii_mode, indent):
    _, _, _, Style, current_palette, _ = _pp()
    p = current_palette()
    level = tok.get("attrs", {}).get("level", 1)
    # A heading is the body tone + weight (so it tracks the cream body, not the
    # hue-less accent); the top two levels add an underline so they read as
    # headings rather than mere inline bold.
    base = ds.assistant.merge(p.accent)
    if level <= 2:
        base = base.merge(Style(underline=True))
    segs = _inline(tok.get("children"), ds, base, ascii_mode)
    return _wrap_segments(segs, width, [(indent, Style())], [(indent, Style())])


def _code(tok, ds, indent):
    """Fenced code → the narrative code role (ds.code), deeper-indented, no box.
    Code is left unwrapped (verbatim) like tool output."""
    from siftd.output.row import row_line

    raw = tok.get("raw", "")
    code_indent = indent + _CODE_STEP
    return [row_line([(ln, ds.code)], indent=code_indent) for ln in raw.rstrip("\n").split("\n")]


def _list(tok, ds, width, ascii_mode, indent, depth: int = 0):
    _, _, _, Style, _, _ = _pp()
    attrs = tok.get("attrs", {})
    ordered = attrs.get("ordered", False)
    bullet = "- " if ascii_mode else "• "
    item_indent = indent + ("  " * depth)
    idx = attrs.get("start", 1) if ordered else 1
    lines: list = []
    for item in tok.get("children", []):
        marker = f"{idx}. " if ordered else bullet
        idx += 1
        inline_children, nested, blocks = _split_list_item(item)
        segs = _inline(inline_children, ds, ds.assistant, ascii_mode)
        cont_indent = item_indent + " " * len(marker)
        first = [(item_indent, Style()), (marker, ds.separator)]
        cont = [(cont_indent, Style())]
        lines.extend(_wrap_segments(segs, width, first, cont))
        for nested_list in nested:
            lines.extend(_list(nested_list, ds, width, ascii_mode, indent, depth + 1))
        # Code fences / rules / tables / quotes inside the item render through the
        # real block renderers at the continuation indent — never dropped.
        if blocks:
            _render_blocks(blocks, lines, ds, width, ascii_mode, cont_indent)
    return lines


def _table(tok, width, ascii_mode, indent):
    from painted import pad

    from siftd.output.table import render_string_table

    head = body = None
    for child in tok.get("children", []):
        if child.get("type") == "table_head":
            head = child
        elif child.get("type") == "table_body":
            body = child
    headers = [_cell_text(c) for c in (head.get("children", []) if head else [])]
    rows = [
        [_cell_text(c) for c in row.get("children", [])]
        for row in (body.get("children", []) if body else [])
    ]
    # Drop the width budget on an incapable stream (ascii_mode): a budget makes
    # painted ellipsize overflow with its hardcoded ``…``, which would crash a
    # strict-ASCII TTY — the same sidestep ``table.print_table`` makes.
    avail = None if (ascii_mode or width is None) else max(10, width - len(indent))
    block = render_string_table(headers, rows, width=avail, as_ascii=ascii_mode)
    return pad(block, left=len(indent)) if indent else block


def _rule(ds, ascii_mode, indent):
    from siftd.output.row import row_line

    ch = "-" if ascii_mode else "─"
    return row_line([(ch * _RULE_WIDTH, ds.separator)], indent=indent)


def _blockquote(tok, ds, width, ascii_mode, indent):
    _, Line, Span, _, _, _ = _pp()
    gutter = "| " if ascii_mode else "│ "
    inner: list = []
    _render_blocks(tok.get("children", []), inner, ds, width, ascii_mode, "")
    inner = _trim_trailing_blanks(inner)
    out: list = []
    for item in inner:
        if _is_line(item):
            out.append(Line(spans=(Span(indent + gutter, ds.separator), *item.spans)))
        else:  # a table inside a quote — pad past the gutter column
            from painted import pad

            out.append(pad(item, left=len(indent + gutter)))
    return out


# --- inline -----------------------------------------------------------------


def _inline(children, ds, base_style, ascii_mode: bool) -> list[tuple[str, Any]]:
    """Walk inline tokens into ``(text, Style)`` segments, composing emphasis."""
    _, _, _, Style, current_palette, _ = _pp()
    p = current_palette()
    out: list[tuple[str, Any]] = []
    for node in children or []:
        t = node.get("type")
        if t == "text":
            out.append((node.get("raw", ""), base_style))
        elif t == "strong":
            out.extend(_inline(node.get("children"), ds, base_style.merge(p.accent), ascii_mode))
        elif t == "emphasis":
            out.extend(
                _inline(node.get("children"), ds, base_style.merge(Style(italic=True)), ascii_mode)
            )
        elif t in ("strikethrough", "del"):
            out.extend(
                _inline(node.get("children"), ds, base_style.merge(Style(dim=True)), ascii_mode)
            )
        elif t == "codespan":
            out.append((node.get("raw", ""), ds.code))
        elif t == "link":
            url = node.get("attrs", {}).get("url", "")
            txt = _inline(node.get("children"), ds, base_style.merge(p.accent), ascii_mode)
            out.extend(txt)
            label = "".join(s for s, _ in txt)
            if url and url != label:
                out.append((f" ({url})" if label else f"({url})", ds.summary))
        elif t == "image":
            alt = _flatten_text(node.get("children")) or node.get("attrs", {}).get("url", "")
            out.append((f"[image: {alt}]", ds.summary))
        elif t in ("softbreak", "linebreak"):
            out.append((" ", base_style))
        elif t in ("inline_html", "html"):
            out.append((node.get("raw", ""), base_style))
        elif node.get("children"):
            out.extend(_inline(node.get("children"), ds, base_style, ascii_mode))
        elif node.get("raw"):
            out.append((node.get("raw", ""), base_style))
    return out


# --- helpers ----------------------------------------------------------------


def _wrap_segments(segments, width, first_prefix, cont_prefix) -> list:
    """Word-wrap ``(text, Style)`` segments to ``width``, prefixing each line.

    Thin delegator to the leaf ``output.row.wrap_segments`` (the one home for the
    aligned-continuation word-wrap, shared with the help body). ``first_prefix`` /
    ``cont_prefix`` are ``[(text, Style)]`` segment lists (the body indent, plus a
    list marker on the first line of an item).
    """
    from siftd.output.row import wrap_segments

    return wrap_segments(segments, width, first_prefix, cont_prefix)


def _split_list_item(item):
    """Split a list_item into (inline content, nested lists, other block children).

    Adjacent inline-bearing children (a loose list item with two paragraphs) are
    space-separated with a synthetic softbreak so they don't run together. Other
    block children (a code fence, rule, table, or quote inside the item) are
    returned separately so they route through the real block renderers rather than
    being flattened or dropped.
    """
    inline: list = []
    nested: list = []
    blocks: list = []
    for child in item.get("children", []):
        ct = child.get("type")
        if ct in ("block_text", "paragraph"):
            if inline:
                inline.append({"type": "softbreak"})  # space between adjacent paragraphs
            inline.extend(child.get("children", []))
        elif ct == "list":
            nested.append(child)
        else:
            blocks.append(child)  # block_code / thematic_break / table / block_quote
    return inline, nested, blocks


def _cell_text(cell) -> str:
    return _flatten_text(cell.get("children"))


def _flatten_text(nodes) -> str:
    """Collapse an inline subtree to plain text (table cells, image alt, fallback)."""
    out: list[str] = []
    for n in nodes or []:
        if n.get("type") in ("softbreak", "linebreak"):
            out.append(" ")
        elif n.get("raw") is not None:
            out.append(n["raw"])
        elif n.get("children"):
            out.append(_flatten_text(n["children"]))
    return "".join(out)


def _plain_lines(content, ds, width) -> list:
    _, _, _, Style, _, _ = _pp()
    out: list = []
    for ln in content.split("\n"):
        out.extend(
            _wrap_segments([(ln, ds.assistant)], width, [(_BASE_INDENT, Style())], [(_BASE_INDENT, Style())])
        )
    return out


def _blank() -> Line:
    from siftd.output.row import row_line

    return row_line([])


def _is_line(item) -> bool:
    from painted import Line

    return isinstance(item, Line)


def _is_blank_line(item) -> bool:
    return _is_line(item) and not any(s.text.strip() for s in item.spans)


def _trim_trailing_blanks(out: list) -> list:
    while out and _is_blank_line(out[-1]):
        out.pop()
    return out
