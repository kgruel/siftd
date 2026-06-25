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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from painted import LIGHT, Palette, Style, Theme, current_palette

if TYPE_CHECKING:
    from painted import Fidelity


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
    code: Style          # inline/fenced code in narrative bodies — readable, not
                         # dimmed (the web keeps code at full fg + a bg chip; the
                         # terminal can't chip, so it recedes by weight, not by
                         # going unreadable). Distinct from tool_input (tool I/O).

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
    faint: Style         # tertiary chrome BELOW the muted floor — help defaults
                         # ((default 10)), example comments, the breadcrumb chevron
                         # and the version dash. A second, dimmer grey that buys the
                         # help surface the design concept's tonal depth (muted for
                         # connective tissue, faint for the things that recede
                         # further). Deliberately recessive: on a near-black
                         # terminal it sits close to the substrate (the point — it's
                         # tertiary), which is why muted, not this, stays the floor
                         # for anything that must stay legible.


# --- Identity roles ---------------------------------------------------------
# painted's Palette names five UNIVERSAL semantic roles (success/warning/error/
# accent/muted) plus the text/surface substrate. siftd's identity needs a handful
# that vocabulary can't name. They extend the Palette by SUBCLASSING it, so they
# ride the SAME ambient channel: ``use_theme`` installs the whole SiftdPalette
# into painted's palette contextvar, ``current_palette()`` returns it, and
# ``domain_styles()`` reads the roles off it — one value, one setter, swapped as a
# unit. (Cream isn't a new role: the body foreground is what painted already names
# ``text``.) Naming is by MEANING, not hue, so a variant in another key — nord, or
# a future mono — assigns its own values to the same roles; "terracotta"/"amber"/
# "teal" would have been lies the moment the warm theme stopped being the only one.

# Warm body foreground — the palette's ``text`` substrate. Kept as a named constant
# because it is also the defensive fallback when a bare (non-identity) painted
# palette is active and ``text`` is unset.
_CREAM = "#e4d9bf"


@dataclass(frozen=True)
class SiftdPalette(Palette):
    """painted's Palette carrying siftd's identity — the warm-obsidian theme.

    Re-declares painted's five universal roles with siftd's warm values and adds
    the identity roles that vocabulary can't name, so a default-constructed
    ``SiftdPalette()`` IS siftd's theme and the nord preset just overrides the
    fields. ``domain_styles()`` maps these roles onto the domain vocabulary.
    Frozen, like the base — a swap installs a whole new value, never mutates one.
    (Re-declaring the base roles also keeps each one a field the subclass owns, so
    the presets can set them by keyword. The hexes render truecolor and downsample
    on lesser terminals; they assume a dark background — on a light terminal they
    lose contrast, notably the bright amber.)
    """

    # --- painted's universal roles, defaulted to the warm-obsidian values ---
    # Backgrounds are deliberately omitted: the terminal owns its substrate, so only
    # foregrounds cross over from the served Swiss UI (serve/static/siftd.css).
    success: Style = field(default_factory=lambda: Style(fg="#5ba8a0"))   # teal
    warning: Style = field(default_factory=lambda: Style(fg="#d4a843"))   # gold
    error: Style = field(default_factory=lambda: Style(fg="#c85050"))     # soft red
    # accent carries NO hue, only bold — the one cool note (a blue) read as an
    # intruder in an otherwise-warm identity, so structure pops by WEIGHT (see
    # structure_style). Gold/amber (the `metric` thread) is the only structural hue.
    accent: Style = field(default_factory=lambda: Style(bold=True))
    # The dim chrome floor — timestamps, tool-status, arrows, separators. Warm
    # grey-gold; single point of control (every muted consumer lifts at once).
    muted: Style = field(default_factory=lambda: Style(fg="#8f836a"))
    # The body substrate — default fg for every role-less cell (search snippets,
    # plain table cells, tool output), layered UNDER each cell's explicit style.
    text: Style | None = field(default_factory=lambda: Style(fg=_CREAM))
    # Categorical ramp in the identity hues (error/warning/success + bright amber);
    # unused today but kept blue-free so a future chart inherits the warm identity.
    series: tuple[Style, ...] = field(
        default_factory=lambda: (
            Style(fg="#c85050"),
            Style(fg="#d4a843"),
            Style(fg="#5ba8a0"),
            Style(fg="#c9a84c"),
        )
    )

    # --- the siftd-identity roles painted's vocabulary can't name ---
    # The warm literal hue — code + copy-paste identifiers, distinct from prose.
    # (accent is hue-less, so the literal hue can't ride it; it is a role of its own.)
    literal: Style = field(default_factory=lambda: Style(fg="#d69a58"))
    # The amber "sifting for gold" metric thread at two weights: inline (quiet) and
    # headline (loud). Distinct from `warning` (a status colour, a different gold).
    metric: Style = field(default_factory=lambda: Style(fg="#a8884a"))
    metric_strong: Style = field(default_factory=lambda: Style(fg="#c9a84c"))
    # The marker hue — tags and agent badges. A CATEGORICAL accent, not a status
    # signal; equals `success` in the warm theme by designed coincidence, but it is
    # its own role so a variant can part them (nord does: Frost teal vs Aurora green)
    # without one swap dragging the other.
    marker: Style = field(default_factory=lambda: Style(fg="#5ba8a0"))
    # The third text weight, between `text` and the `muted` floor — field labels,
    # model names, summary hints recede to it.
    secondary: Style = field(default_factory=lambda: Style(fg="#a89a82"))
    # Tertiary chrome BELOW the muted floor — help defaults, example comments, the
    # breadcrumb chevron, the version dash. Deliberately sub-floor (see DomainStyles).
    faint: Style = field(default_factory=lambda: Style(fg="#56564e"))


# The warm-obsidian identity is the default-constructed palette (one home for the
# warm values: the field defaults above).
SIFTD_PALETTE = SiftdPalette()

# The "nord" variant — the same identity story transposed into the official Nord
# palette. Aurora carries the meaning-colours, Frost the accent, Snow Storm the
# body, Polar Night the chrome floor. The one principled divergence from the warm
# law: nord RE-ADMITS blue as accent (Frost #81a1c1 + weight). The warm theme's
# no-blue rule was a temperature-clash mitigation, and a cool palette's native
# Frost blue is no intruder — so structure_style() pops blue-and-bold here and
# bold-only in warm from ONE unchanged definition. (Giving accent a hue recolours
# EVERY accent consumer as a unit — the search rank rail, match highlight, query
# echo, markdown-strong, progress fill, spinners — not just structure_style; that
# is the point of routing them all through the one role.) Two tones are DERIVED midtones
# — Nord's 16 colours leave gaps the warm ramp filled by darkening/midpointing
# (metric inline #d0a85f, secondary #9aa3b4) — the spots to eyeball on a real
# terminal. (The dark-substrate/light-terminal caveat above applies here too.)
SIFTD_NORD_PALETTE = SiftdPalette(
    success=Style(fg="#a3be8c"),            # Aurora green
    warning=Style(fg="#ebcb8b"),            # Aurora yellow
    error=Style(fg="#bf616a"),              # Aurora red
    accent=Style(fg="#81a1c1", bold=True),  # Frost blue + weight — nord is cool
    muted=Style(fg="#4c566a"),              # Polar Night 3 — the chrome floor
    text=Style(fg="#d8dee9"),               # Snow Storm 0 — body substrate
    series=(
        Style(fg="#bf616a"),
        Style(fg="#ebcb8b"),
        Style(fg="#a3be8c"),
        Style(fg="#81a1c1"),
    ),
    literal=Style(fg="#d08770"),            # Aurora orange — the literal hue
    metric=Style(fg="#d0a85f"),             # derived dim Aurora yellow — inline metric
    metric_strong=Style(fg="#ebcb8b"),      # Aurora yellow — headline metric
    marker=Style(fg="#8fbcbb"),             # Frost teal — Nord's real teal
    secondary=Style(fg="#9aa3b4"),          # derived Snow→Polar midtone
    faint=Style(fg="#434c5e"),              # Polar Night 2 — sub-muted floor
)

# Themes bundle a palette with border chrome. Borders/icons are siftd's STRUCTURAL
# identity (LIGHT, chosen over painted's ROUNDED default), held constant across
# variants — a theme swap is a pure RECOLOUR, one axis of change.
SIFTD_THEME: Theme = Theme(palette=SIFTD_PALETTE, borders=LIGHT)
SIFTD_NORD_THEME: Theme = Theme(palette=SIFTD_NORD_PALETTE, borders=LIGHT)

# The default identity. Importers (cli, doctor, tests) bind this by name; keeping
# the alias avoids a rename sweep across the ~18 lazy importers.
siftd_theme: Theme = SIFTD_THEME

# The config-selectable variants. THEME_NAMES is the single source the `ui.theme`
# help text and the doctor `config-valid` check validate against.
_THEMES: dict[str, Theme] = {"siftd": SIFTD_THEME, "nord": SIFTD_NORD_THEME}
THEME_NAMES: tuple[str, ...] = tuple(_THEMES)


def theme_for_name(name: str | None) -> Theme:
    """Resolve a ``ui.theme`` config value to a Theme.

    Unknown or unset names fall back to siftd's default — graceful by design; the
    doctor ``config-valid`` check is what surfaces a bad name (mirroring how
    ``search.formatter`` is validated), not this lookup.
    """
    return _THEMES.get((name or "siftd").strip().lower(), SIFTD_THEME)


def structure_style() -> Style:
    """The structure role — bold body text (``palette.text`` merged with ``accent``).

    Deliberately *not* a ``DomainStyles`` field: it composes two roles the theme
    already owns — the body substrate and the accent — enacting the "structure pops
    by weight (and, where the palette gives accent a hue, by colour)" law rather
    than naming a new role. In the warm theme accent is weight-only, so structure
    pops by weight; in nord accent carries Frost blue, so it pops blue-and-bold —
    the temperature-appropriate behaviour falls out of the palette swap, no branch.
    Named here so the one concept (group labels, the breadcrumb's command, the
    wordmark's letters, sub-command names) has a single definition the help
    renderer, the mark, and any role-fidelity test can anchor on.
    """
    p = current_palette()
    # Same bare-palette guard as domain_styles(): a palette with text=None (a bare
    # painted palette) would otherwise crash this merge. Both ambient-palette readers
    # fall back to the warm cream substrate.
    text = p.text or Style(fg=_CREAM)
    return text.merge(p.accent)


def domain_styles(fidelity: Fidelity | None = None) -> DomainStyles:
    """Build domain styles from the ambient palette.

    Reads the identity roles off the active SiftdPalette (installed by
    ``use_theme``), so a palette swap (``ui.theme``) recolours the whole vocabulary
    at once: ``code``/``identifier`` ride ``literal``; the metric thread rides
    ``metric``/``metric_strong``; ``tag``/``agent`` ride ``marker``; ``label``/
    ``model``/``summary`` ride ``secondary``; the body (``cream``) is the palette's
    ``text`` substrate. The five universal roles (muted/error/...) are read straight
    off the palette as before.

    When fidelity is provided, depth-gated styles adjust:
        - thinking: secondary italic at default, body (brighter) when shown
    """
    p = current_palette()
    # Fall back to the warm preset when a bare (non-identity) painted palette is
    # active — e.g. a test installing MONO_PALETTE: such palettes lack the identity
    # roles, and those roles aren't theirs to carry, so a bare-palette swap doesn't
    # recolour them. This keeps domain_styles() from ever crashing on a missing role.
    sp = p if isinstance(p, SiftdPalette) else SIFTD_PALETTE

    cream = sp.text or Style(fg=_CREAM)  # body text — warm + bright; the substrate

    # Thinking prominence: secondary italic by default, body when explicitly shown.
    thinking_visible = fidelity is not None and fidelity.shows("thinking")
    if thinking_visible:
        thinking = cream.merge(Style(italic=True))
    else:
        thinking = sp.secondary.merge(Style(italic=True))

    return DomainStyles(
        # Identifiers & labels — ids get the literal hue + bold (copy-paste targets
        # keep their weight). The palette `accent` role stays hue-less, so
        # pure-structural accents (rank rail, match highlight) stay weight-only.
        identifier=sp.literal.merge(Style(bold=True)),
        tag=sp.marker,

        # Narrative roles — body cream; prompt/role labels cream + weight.
        prompt=cream.merge(Style(bold=True)),
        assistant=cream,
        thinking=thinking,
        code=sp.literal,  # the literal hue — readable, distinct from prose

        # Tool rendering
        tool_name=cream.merge(Style(bold=True)),
        tool_input=p.muted,
        tool_result=Style(),
        tool_error=p.error,

        # Table / list data
        temporal=p.muted,
        metric=sp.metric,
        metric_strong=sp.metric_strong,
        workspace=Style(),
        model=sp.secondary,
        adapter=p.muted,
        agent=sp.marker,

        # Structural — labels recede to secondary (the structural accent is weight,
        # carried by identifier/prompt/tool_name above, not a colour on the labels).
        label=sp.secondary,
        summary=sp.secondary,
        separator=p.muted,
        faint=sp.faint,
    )
