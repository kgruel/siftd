"""Painted rendering for doctor checks — the live activity bar and the report.

Two surfaces, both single-column and typographic (no borders):

  - ``render_progress_block`` — the **live panel**: one determinate activity bar
    whose label narrates the check that most recently resolved, with a spinner
    glyph that advances per resolved check (settling to ✓), over an issue feed
    (warnings / info / errors) that builds up beneath as checks land. No
    passed/pending lists and no running tally — those belong to the report. The
    bar is replaced by the report on finalize, so it "disappears when finished".
  - ``render_findings_block`` — the **settled report**, composed as a
    :class:`~siftd.output.listing.StatusReport` and deposited as the final frame,
    so doctor's verdict reads as one family with ``cmd_status`` et al.

The bar is the shared live primitive (``output.live.bar_row``, the same one
push/pull and ingest use); the severity vocabulary (glyph + style + order) is the
shared one in ``output.status``. Doctor is just a consumer of both.

Dropping the old two-column ROUNDED-bordered layout also dissolved a crash class:
that layout derived column widths by subtraction (``left_width = tw - …``), which
went negative on a degenerate pty (0 reported columns) and crashed painted. A
single-column feed has no subtraction-derived widths, so the crash cannot recur,
and the non-Unicode border-garbling disappears with the border.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from siftd.output.common import term_width
from siftd.output.row import row_line

# The severity vocabulary lives in the output layer (output.status, beside the
# callout severity map) so every surface that lays out its own marks reaches it
# without a ``cli -> doctor`` import; doctor is just another consumer.
from siftd.output.status import severity_glyph, severity_mark, severity_rank

if TYPE_CHECKING:
    from painted import Block, Style

    from siftd.doctor.checks import Finding

__all__ = ["render_findings_block", "render_progress_block", "severity_glyph"]


def _fit(segments: list[tuple[str, Style | None]], width: int) -> Block:
    """One row of ``(text, style)`` segments, truncated to ``width`` columns.

    Keeps a long message from overflowing the terminal and tearing the in-place
    frame; the settled report (which the terminal soft-wraps) does not truncate.
    painted's block ``truncate`` appends the ambient ``IconSet.ellipsis`` (an
    honest, ASCII-degrading cut) and is a no-op when the row already fits.
    """
    from painted import truncate

    line = row_line(segments)
    return truncate(line.to_block(line.width), width)


def _clip(text: str, width: int) -> str:
    """Truncate a bar label to ``width`` display columns with the IconSet ellipsis.

    ``bar_row`` left-pads the label but does not truncate it, so a long check name
    would push the bar off the right edge; this clips it first (display-width
    correct, not ``len()``).
    """
    from painted import current_icons
    from painted.core._text_width import display_width

    if display_width(text) <= width:
        return text
    ellipsis = current_icons().ellipsis
    budget = width - display_width(ellipsis)
    if budget <= 0:
        return ellipsis if width >= display_width(ellipsis) else ""
    out, used = "", 0
    for ch in text:
        cw = display_width(ch)
        if used + cw > budget:
            break
        out += ch
        used += cw
    return out + ellipsis


def _issue_segments(finding: Finding) -> list[tuple[str, Style | None]]:
    """The shared issue line — ``<glyph> check: message`` — one definition for the
    live feed and the settled report (so the two read identically)."""
    from painted import Style

    glyph, style = severity_mark(finding.severity)
    return [(f"{glyph} ", style), (f"{finding.check}: ", Style(bold=True)), (finding.message, None)]


def _summary_segments(
    findings: list[Finding], total_checks: int
) -> list[tuple[str, Style | None]]:
    """The severity tally as ``(text, style)`` segments — ``⚠ 1 warning  ✓ 13 passed``.

    The report's verdict line. ``passed`` is ``total_checks`` minus the distinct
    checks with findings.
    """
    parts: list[tuple[str, Style | None]] = []
    for sev in ("error", "warning", "info"):
        count = sum(1 for f in findings if f.severity == sev)
        if count:
            glyph, style = severity_mark(sev)
            parts.append((f"{glyph} {count} {sev}", style))
    passed = total_checks - len({f.check for f in findings})
    if passed > 0:
        glyph, style = severity_mark(None)
        parts.append((f"{glyph} {passed} passed", style))

    segments: list[tuple[str, Style | None]] = []
    for i, part in enumerate(parts):
        if i:
            segments.append(("  ", None))
        segments.append(part)
    return segments


def render_progress_block(
    check_names: list[str],
    completed: dict[str, list[Finding]],
    *,
    current: str | None = None,
):
    """The live progress panel — a determinate activity bar over an issue feed.

    Layout (constrained to terminal width, no borders)::

        cost-coverage  ━━━━━━━━━━━━━─────────  11/17  ⠋

        ⚠ ingest-errors: 8 file(s) failed
        ℹ blob-migration: Legacy blob layout detected; migrate when convenient

    ``current`` is the check that most recently resolved (checks run concurrently,
    so this is the latest activity, not a single in-flight check). The bar fills to
    the fraction done; the trailing glyph is a spinner (advancing once per resolved
    check) until the run settles to ✓. Warnings/info/errors accumulate beneath it.
    Repainted per check completion; ``finalize`` then replaces the whole panel with
    the settled report, so the bar disappears when finished.
    """
    from painted import Block, current_icons, current_palette, join_vertical
    from painted.core._text_width import display_width

    from siftd.output.live import bar_row, spinner_glyph
    from siftd.output.theme import domain_styles

    pal = current_palette()
    ic = current_icons()
    ds = domain_styles()

    tw = max(term_width(), 20)
    total = len(check_names)
    done = len(completed)
    fraction = done / total if total > 0 else 0.0
    settled = total > 0 and done >= total

    # Widths: the label column is the widest check name (capped) so the bar never
    # jumps as the current check changes; the bar takes the rest. The label is
    # stolen from first when the terminal is too narrow for a >=6-wide bar — so no
    # width is ever derived negative (the crash class stays dissolved).
    if settled:
        glyph, glyph_style, frac = ic.ok, pal.success, 1.0
    else:
        # The spinner advances once per resolved check (done) — discrete activity
        # tied to real progress, not a faked continuous animation.
        glyph, glyph_style, frac = spinner_glyph(done), pal.accent, fraction
    count = f"{done}/{total}"
    # leading gap + two trailing gaps around segments + the trailing glyph.
    reserve = display_width(count) + display_width(glyph) + 6
    max_name = max((display_width(n) for n in check_names), default=8)
    label_width = min(max_name, 24)
    bar_width = tw - label_width - reserve
    if bar_width < 6:
        label_width = max(1, tw - reserve - 6)
        bar_width = max(1, tw - label_width - reserve)

    label = _clip(current if current else "checking…", label_width)
    bar = bar_row(
        label, frac, label_width=label_width, bar_width=bar_width,
        segments=[(count, pal.muted)], glyph=glyph, glyph_style=glyph_style,
        label_style=pal.muted, fill_style=ds.metric, empty_style=pal.muted,
        filled_char="━", empty_char="─",
    )

    parts: list[Block] = [bar]

    issues = sorted(
        (f for fs in completed.values() for f in fs),
        key=lambda f: (severity_rank(f.severity), f.check),
    )
    if issues:
        parts.append(Block.empty(0, 1))
        parts.extend(_fit(_issue_segments(f), tw) for f in issues)

    return join_vertical(*parts)


def render_findings_block(
    findings: list[Finding],
    show_fixes: bool = False,
    total_checks: int = 0,
):
    """The settled findings report as a ``StatusReport`` block — the final frame.

    The issue feed (the same ``<glyph> check: message`` line the live panel uses,
    with a ``↳`` fix-command continuation), the severity tally as a note, and —
    when ``show_fixes`` — a "To fix" section. Composed from the same
    report-structure atoms ``cmd_status`` uses, so doctor's verdict reads as one
    family.
    """
    from painted import current_palette

    from siftd.output.listing import StatusReport

    muted = current_palette().muted
    report = StatusReport()

    if not findings:
        glyph, style = severity_mark(None)
        message = f"All {total_checks} checks passed." if total_checks else "All checks passed."
        report.note([(f"{glyph} ", style), (message, style)])
        return report.to_block()

    ordered = sorted(findings, key=lambda f: (severity_rank(f.severity), f.check))
    items: list[list[tuple[str, Style | None]]] = []
    for f in ordered:
        items.append(_issue_segments(f))
        if f.fix_command and not show_fixes:
            items.append([("  ↳ ", muted), (f.fix_command, muted)])
    report.note(*items)

    report.note(_summary_segments(findings, total_checks))

    if show_fixes:
        seen: set[str] = set()
        commands: list[str] = []
        for f in ordered:
            if f.fix_available and f.fix_command and f.fix_command not in seen:
                commands.append(f.fix_command)
                seen.add(f.fix_command)
        if commands:
            report.lines_section("To fix", commands)

    return report.to_block()
