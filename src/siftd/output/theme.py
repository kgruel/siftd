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
    metric: Style        # tokens, cost, counts — the quiet amber thread (amber-dim)
    metric_strong: Style # headline totals — loud amber (the conversation grand-total
                         # today; reserved for a future stats/report surface)
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

    # The bespoke "warm obsidian" palette, ported from the served Swiss UI
    # stylesheet (serve/static/siftd.css). painted carries the five universal
    # semantic roles; the two siftd-identity tones the role vocabulary can't name
    # — the amber "sifting for gold" thread and a third (secondary) text weight —
    # live in domain_styles(), which maps roles onto the domain vocabulary.
    #
    # The hex values render truecolor and downsample on lesser terminals.
    # Backgrounds are deliberately omitted: the terminal owns its substrate, so
    # only the foreground hues cross over from the web surface. Those hues assume a
    # dark background (the Swiss "warm obsidian"); on a light terminal they lose
    # contrast — notably the bright amber, which can read fainter than its dim
    # sibling (there is no terminal-background detection to mitigate).
    #
    # The `accent` role carries NO hue, only bold: the one cool note (a blue) read
    # as an intruder in an otherwise-warm identity, so structural elements —
    # identifiers, prompt markers, tool-names — pop by WEIGHT while field labels
    # recede to `secondary`. Gold/amber stays the only structural colour ("the gold
    # is the point"); tags/agents keep their teal; success/warning/error keep their
    # meaning-colours. Because every accent consumer (the search rank rail, follow
    # headers, ingest counts) reads this one role, dropping the hue here drops it
    # everywhere at once.
    palette = Palette(
        success=Style(fg="#5ba8a0"),   # teal
        warning=Style(fg="#d4a843"),   # gold
        error=Style(fg="#c85050"),     # soft red
        accent=Style(bold=True),       # no hue — structure pops by weight, not colour
        muted=Style(fg="#505862"),     # slate
        # Categorical ramp in the identity hues (error/warning/success + the bright
        # amber). Unused today (no flame surface) but kept blue-free so a future
        # chart inherits the warm identity rather than reviving the dropped accent.
        series=(
            Style(fg="#c85050"),
            Style(fg="#d4a843"),
            Style(fg="#5ba8a0"),
            Style(fg="#c9a84c"),
        ),
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

    # siftd-identity tones the painted role vocabulary can't name (see _make_theme):
    # the amber "sifting for gold" thread at two weights, a third text weight, and
    # the teal used for tags/agents. Hex renders truecolor and downsamples.
    amber = Style(fg="#c9a84c")        # bright — headline figures (the loud tier)
    amber_dim = Style(fg="#8a7a3a")    # dim — inline metrics (tokens/cost/counts)
    secondary = Style(fg="#8a8a9a")    # third text weight, between fg and muted
    teal = Style(fg="#5ba8a0")         # tags, agents

    # Thinking prominence: secondary italic by default, brighter when explicitly shown
    thinking_visible = fidelity is not None and fidelity.shows("thinking")
    if thinking_visible:
        thinking = Style(italic=True)
    else:
        thinking = secondary.merge(Style(italic=True))

    return DomainStyles(
        # Identifiers & labels
        identifier=p.accent,
        tag=teal,

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
        metric=amber_dim,
        metric_strong=amber,
        workspace=Style(),
        model=secondary,
        adapter=p.muted,
        agent=teal,

        # Structural — labels recede to secondary (the structural accent is weight,
        # carried by identifier/prompt/tool_name above, not a colour on the labels).
        label=secondary,
        summary=secondary,
        separator=p.muted,

        # Borders — tool I/O gets LIGHT, thinking gets ROUNDED
        tool_border=LIGHT,
        thinking_border=ROUNDED,
    )
