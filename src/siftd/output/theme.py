"""Visual language — domain styles composed from painted primitives.

Defines the Theme (palette + borders) and domain-specific styles
that map semantic roles onto painted Style objects. The domain styles
derive from the ambient palette so they adapt when the palette changes.

Usage:
    from siftd.output.theme import siftd_theme, domain_styles

    with use_theme(siftd_theme):
        s = domain_styles()
        # s.identifier, s.temporal, s.prompt, s.thinking, ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from painted import BorderChars, Fidelity, Style, Theme


@dataclass(frozen=True)
class DomainStyles:
    """Siftd semantic styles — the full visual vocabulary.

    Organized by attention hierarchy:
        High:   identifier, prompt, tag
        Medium: workspace, model, assistant
        Low:    temporal, metric, adapter, summary
        Accent: tool_name, agent
        Dim:    thinking, tool_input, tool_result
        Error:  tool_error
    """

    # --- Identifiers & labels ---
    identifier: Style    # session/conversation IDs — actionable, copy-paste target
    tag: Style           # user-applied tags — notable but not loud

    # --- Narrative roles ---
    prompt: Style        # user prompt label + text
    assistant: Style     # assistant response text
    thinking: Style      # model thinking/reasoning — depth-gated prominence

    # --- Tool rendering ---
    tool_name: Style     # tool call header (shell.execute, file.read, etc.)
    tool_input: Style    # tool input content (commands, paths, patterns)
    tool_result: Style   # tool output/result content
    tool_error: Style    # error status/output

    # --- Table / list data ---
    temporal: Style      # timestamps, "ago" values — recedes
    metric: Style        # tokens, cost, counts — recedes
    workspace: Style     # project context — moderate prominence
    model: Style         # AI model name — informational
    adapter: Style       # source tool adapter — supporting detail
    agent: Style         # child agent count — notable when present

    # --- Structural ---
    label: Style         # section headers, field labels
    summary: Style       # summary hints, overflow indicators
    separator: Style     # visual dividers

    # --- Borders ---
    tool_border: BorderChars   # box around tool I/O content
    thinking_border: BorderChars  # box around thinking blocks


def _make_theme():
    from painted import LIGHT, ROUNDED, Palette, Style, Theme

    palette = Palette(
        accent=Style(fg=110, bold=True),   # soft blue — identifiers, prompts
        muted=Style(fg=60),                # grey — timestamps, metrics, structure
        success=Style(fg=72),              # teal — tags, positive status
        warning=Style(fg=180),             # amber
        error=Style(fg=167),               # soft red
    )
    return Theme(palette=palette, borders=LIGHT), ROUNDED


_SIFTD_THEME, _ROUNDED = _make_theme()

siftd_theme: Theme = _SIFTD_THEME


def domain_styles(fidelity: Fidelity | None = None) -> DomainStyles:
    """Build domain styles from the ambient palette.

    When fidelity is provided, depth-gated styles adjust:
        - thinking: italic dim at default, bordered at --thinking
        - tool content: density-aware
    """
    from painted import LIGHT, ROUNDED, Style, current_palette

    p = current_palette()

    # Thinking prominence: dim italic by default, less dim when explicitly shown
    thinking_visible = fidelity is not None and fidelity.shows("thinking")
    if thinking_visible:
        thinking = Style(italic=True)
    else:
        thinking = p.muted.merge(Style(italic=True))

    return DomainStyles(
        # Identifiers & labels
        identifier=p.accent,
        tag=Style(fg=72),  # teal — distinct from accent blue

        # Narrative roles
        prompt=p.accent,
        assistant=Style(),
        thinking=thinking,

        # Tool rendering
        tool_name=p.accent,
        tool_input=p.muted,
        tool_result=Style(),
        tool_error=p.error,

        # Table / list data
        temporal=p.muted,
        metric=p.muted,
        workspace=Style(),
        model=Style(),
        adapter=p.muted,
        agent=Style(fg=72),  # teal — matches tags

        # Structural
        label=p.accent,
        summary=p.muted,
        separator=p.muted,

        # Borders — tool I/O gets LIGHT, thinking gets ROUNDED
        tool_border=LIGHT,
        thinking_border=ROUNDED,
    )
