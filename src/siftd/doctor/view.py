"""Painted rendering for doctor checks — progress and findings."""

from __future__ import annotations

from siftd.doctor.checks import Finding
from siftd.output.common import term_width
from siftd.output.row import row_line

# severity_glyph lives in the output layer (output.status, beside the callout
# severity vocabulary) so the CLI surfaces that lay out their own marks reach it
# without a ``cli -> doctor`` import; the doctor is just another consumer.
from siftd.output.status import severity_glyph

__all__ = ["render_findings_block", "render_progress_block", "severity_glyph"]


def _painted():
    from painted import Block, Line, Span, Style, border, join_horizontal, join_vertical

    return Block, Line, Span, Style, border, join_horizontal, join_vertical


def _line(*parts):
    return row_line(parts)


def _line_block(parts, width):
    """Build a line from (text, style) pairs and pad/truncate to exact width."""
    line = row_line(parts)
    if line.width > width:
        line = line.truncate(width)
    return line.to_block(width)


def _max_severity(findings: list[Finding]) -> str | None:
    if not findings:
        return None
    order = {"error": 0, "warning": 1, "info": 2}
    return min(findings, key=lambda f: order.get(f.severity, 3)).severity


def _style_for(key: str):
    from painted import current_palette

    p = current_palette()
    return {"error": p.error, "warning": p.warning, "success": p.success, "muted": p.muted}[key]


def render_progress_block(
    check_names: list[str],
    completed: dict[str, list[Finding]],
    spinner_frame: int,
):
    """Render two-column progress: issues left, passed+pending right in border.

    Layout (constrained to terminal width):
        ████████████████████████████████████  12/17

        ⚠ ingest-errors                   ┃ ╭─────────────────────╮
          ↳ 4 file(s) failed              ┃ │ ✓ embeddings-avail  │
        ℹ blob-migration                  ┃ │ ✓ cost-coverage     │
          ↳ siftd migrate blobs           ┃ │ ✓ freelist          │
                                          ┃ │ · fts-stale         │
                                          ┃ │ · config-valid      │
                                          ┃ ╰─────────────────────╯
        Run siftd doctor fix to apply fixes
    """
    Block, Line, Span, Style, border_fn, join_horizontal, join_vertical = _painted()
    from painted import ROUNDED
    from painted.views import ProgressState, progress_bar

    # Floor the width: the two-column layout below derives several widths by
    # subtraction (bar_width, left_width), which go negative on a degenerate
    # terminal — a pty that reports 0 columns (e.g. under `script`) crashes
    # painted with "Block row width != block width". 40 is the narrowest width
    # the layout stays coherent at; wider real terminals are unaffected.
    tw = max(term_width(), 40)
    total = len(check_names)
    done = len(completed)
    pct = done / total if total > 0 else 0.0

    # --- Column widths ---
    right_inner = max(22, tw * 3 // 10)  # ~30% for the bordered box (inner)
    right_outer = right_inner + 2  # border adds 2
    left_width = tw - right_outer - 1  # remaining space, 1 for separator

    # --- Progress bar (full width at top) ---
    bar_width = min(tw - 10, 50)
    bar = progress_bar(
        ProgressState(pct),
        width=bar_width,
        filled_style=_style_for("success"),
        empty_style=Style(dim=True),
    )
    muted = _style_for("muted")
    count_str = f" {done}/{total}"
    pad_left = _line((" ", Style())).to_block(1)
    count_block = _line((count_str, muted)).to_block(len(count_str))
    bar_block = join_horizontal(pad_left, bar, count_block)

    # --- Classify checks ---
    severity_order = {"error": 0, "warning": 1, "info": 2}
    issues = []  # (name, findings, severity)
    passed = []  # names
    pending = []  # names

    for name in check_names:
        if name in completed:
            findings = completed[name]
            if findings:
                sev = _max_severity(findings)
                issues.append((name, findings, sev))
            else:
                passed.append(name)
        else:
            pending.append(name)

    issues.sort(key=lambda x: severity_order.get(x[2] or "", 3))

    # --- Build left column (issues) ---
    left_blocks = []
    for name, findings, sev in issues:
        icon, key = severity_glyph(sev)
        style = _style_for(key)
        left_blocks.append(_line_block([(f" {icon} ", style), (name, Style(bold=True))], left_width))
        if findings:
            left_blocks.append(_line_block([("   ↳ ", muted), (findings[0].message, muted)], left_width))
            if findings[0].fix_command:
                dim = Style(dim=True)
                left_blocks.append(_line_block([("   ↳ ", dim), (findings[0].fix_command, dim)], left_width))

    # --- Build right column (passed + pending, bordered) ---
    right_lines = []
    ok_style = _style_for("success")
    pass_glyph = severity_glyph(None)[0]
    for name in passed:
        right_lines.append(_line_block([(f" {pass_glyph} ", ok_style), (name, ok_style)], right_inner))
    for name in pending:
        dim = Style(dim=True)
        right_lines.append(_line_block([(" · ", dim), (name, dim)], right_inner))

    if not right_lines:
        right_lines.append(Block.empty(right_inner, 1))

    right_content = join_vertical(*right_lines)
    right_bordered = border_fn(right_content, ROUNDED, style=muted)

    # --- Pad columns to same height ---
    left_h = sum(b.height for b in left_blocks) if left_blocks else 0
    right_h = right_bordered.height
    max_h = max(left_h, right_h, 1)

    if left_blocks:
        left_col = join_vertical(*left_blocks)
    else:
        left_col = Block.empty(left_width, 0)

    if left_col.height < max_h:
        left_col = join_vertical(left_col, Block.empty(left_width, max_h - left_col.height))

    if right_bordered.height < max_h:
        right_bordered = join_vertical(right_bordered, Block.empty(right_outer, max_h - right_bordered.height))

    columns = join_horizontal(left_col, right_bordered)

    parts = [bar_block, Block.empty(tw, 1), columns]

    # Hint at bottom when done
    has_fixes = any(f.fix_available for name in completed for f in completed[name])
    if has_fixes and done == total:
        parts.append(Block.empty(tw, 1))
        parts.append(_line_block(
            [(" Run ", Style(dim=True)), ("siftd doctor fix", muted), (" to apply fixes", Style(dim=True))],
            tw,
        ))

    return join_vertical(*parts)


# ---------------------------------------------------------------------------
# Final findings block (for non-TTY / show_fixes mode)
# ---------------------------------------------------------------------------


def render_findings_block(
    findings: list[Finding],
    show_fixes: bool = False,
    total_checks: int = 0,
):
    """Render final findings list (used for plain painted output without progress)."""
    Block, Line, Span, Style, _, _, join_vertical = _painted()
    muted = _style_for("muted")

    if not findings:
        ok_style = _style_for("success")
        line = _line((f" {severity_glyph(None)[0]} ", ok_style), ("All checks passed.", ok_style))
        summary = _render_summary(findings, total_checks)
        return join_vertical(line.to_block(line.width), summary)

    severity_order = {"error": 0, "warning": 1, "info": 2}
    sorted_findings = sorted(findings, key=lambda f: (severity_order.get(f.severity, 3), f.check))

    lines = []
    for f in sorted_findings:
        icon, key = severity_glyph(f.severity)
        icon_style = _style_for(key)
        lines.append(_line(
            (f" {icon} ", icon_style),
            (f"{f.check}: ", Style(bold=True)),
            (f.message, Style()),
        ))
        if f.fix_command and not show_fixes:
            lines.append(_line(("     ↳ ", muted), (f.fix_command, muted)))

    blocks = [ln.to_block(ln.width) for ln in lines]
    blocks.append(Block.empty(0, 1))
    blocks.append(_render_summary(findings, total_checks))

    if show_fixes:
        fixable = [f for f in sorted_findings if f.fix_available and f.fix_command]
        if fixable:
            fix_lines = [_line((" To fix:", Style(bold=True)))]
            seen = set()
            for f in fixable:
                if f.fix_command not in seen:
                    fix_lines.append(_line(("   ", Style()), (f.fix_command, muted)))
                    seen.add(f.fix_command)
            blocks.append(Block.empty(0, 1))
            blocks.extend(ln.to_block(ln.width) for ln in fix_lines)

    return join_vertical(*blocks)


def _render_summary(findings: list[Finding], total_checks: int):
    _, _, Span, Style, _, _, _ = _painted()
    from painted import Line

    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")
    passed = total_checks - len({f.check for f in findings})

    parts = [Span(" ", Style())]
    if error_count > 0:
        parts.append(Span(f"{severity_glyph('error')[0]} {error_count} error  ", _style_for("error")))
    if warning_count > 0:
        parts.append(Span(f"{severity_glyph('warning')[0]} {warning_count} warning  ", _style_for("warning")))
    if info_count > 0:
        parts.append(Span(f"{severity_glyph('info')[0]} {info_count} info  ", _style_for("muted")))
    if passed > 0:
        parts.append(Span(f"{severity_glyph(None)[0]} {passed} passed", _style_for("success")))

    line = Line(spans=tuple(parts))
    return line.to_block(line.width)
