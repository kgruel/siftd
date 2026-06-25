"""Tests for siftd.output.theme — the bespoke "warm obsidian" palette + domain styles.

These pin the load-bearing decisions so a future edit can't silently undo them:

* the two-tier amber thread — inline ``metric`` is DISTINCT from the headline
  ``metric_strong``; collapsing them erases the slice's point;
* the WARM REBALANCE (eyeball-driven): body text is an explicit cream, the literal
  family (code + identifiers) is a warm terracotta hue (not grey), and the dim floor
  is a warm grey-gold — dense surfaces no longer read as a cool low-contrast wash;
* the palette ``accent`` role stays weight-only (hue-less) so pure-structural accents
  — the search rank rail, match highlight — pop by bold, not colour.
"""

from painted import MONO_PALETTE, Style, current_palette, use_palette, use_theme

from siftd.output.theme import (
    SIFTD_NORD_THEME,
    SIFTD_THEME,
    domain_styles,
    siftd_theme,
    structure_style,
    theme_for_name,
)

CREAM = "#e4d9bf"       # body text — warm + bright
TERRACOTTA = "#d69a58"  # code + identifiers — the warm literal hue
SECONDARY = "#a89a82"   # third weight (warmed from the old cool #8a8a9a)
WARM_MUTED = "#8f836a"  # the dim floor (warmed/raised from the old cool slate #505862)

# Nord variant — the same roles transposed into the official Nord palette.
NORD_BODY = "#d8dee9"     # Snow Storm 0 — body substrate
NORD_LITERAL = "#d08770"  # Aurora orange — code + identifiers
NORD_MARKER = "#8fbcbb"   # Frost teal — tags/agents
NORD_SUCCESS = "#a3be8c"  # Aurora green — the success role (distinct from marker)
NORD_ACCENT = "#81a1c1"   # Frost blue — nord re-admits blue as accent


def test_amber_tiers_are_distinct():
    # The load-bearing invariant: the inline thread and the headline tier must not
    # collapse to one colour.
    with use_theme(siftd_theme):
        ds = domain_styles()
    assert ds.metric != ds.metric_strong


def test_identity_tones_carry_their_hues():
    with use_theme(siftd_theme):
        ds = domain_styles()
    assert ds.metric == Style(fg="#a8884a")         # inline amber — warmed + raised
    assert ds.metric_strong == Style(fg="#c9a84c")  # bright amber — headline figures
    assert ds.model == ds.summary == Style(fg=SECONDARY)  # the warmed third tone
    assert ds.tag == ds.agent == Style(fg="#5ba8a0")      # teal


def test_body_text_is_warm_cream():
    # The theme now owns the foreground on its dark substrate: body text is an
    # explicit warm cream, not the terminal default / a cool grey.
    with use_theme(siftd_theme):
        ds = domain_styles()
    assert ds.assistant == Style(fg=CREAM)


def test_code_and_identifiers_are_the_warm_literal_hue():
    # The eyeball fix: code/identifiers were dimmed grey (unreadable wash). They now
    # carry the warm terracotta literal hue; ids add bold (copy-paste targets keep
    # their weight). Distinct from tool_input (tool I/O) and from prose.
    with use_theme(siftd_theme):
        ds = domain_styles()
    assert ds.code == Style(fg=TERRACOTTA)
    assert ds.identifier == Style(fg=TERRACOTTA, bold=True)
    assert ds.code != ds.tool_input          # decoupled from tool I/O styling
    assert ds.code != ds.assistant           # code is distinct from prose


def test_thinking_defaults_to_secondary_italic_brightens_when_shown():
    with use_theme(siftd_theme):
        ds = domain_styles()
        muted = current_palette().muted
    assert ds.thinking == Style(fg=SECONDARY, italic=True)
    assert ds.thinking != muted.merge(Style(italic=True))


def test_dim_floor_is_warm_not_cool_slate():
    # The single point of control for peripheral chrome (timestamps, tool-status,
    # arrows, separators): a warm grey-gold, raised from the old ≈2.5:1 cool slate.
    with use_theme(siftd_theme):
        p = current_palette()
    assert p.accent == Style(bold=True)      # no hue — structure pops by weight
    assert p.success == Style(fg="#5ba8a0")  # teal
    assert p.warning == Style(fg="#d4a843")  # gold
    assert p.error == Style(fg="#c85050")    # soft red
    assert p.muted == Style(fg=WARM_MUTED)   # warm dim — not the old cool slate


def test_palette_accent_stays_weight_only():
    # The palette accent role carries no hue (so the rank rail / match highlight pop
    # by bold). The warm hue lives on the DOMAIN literal roles, not the palette role.
    with use_theme(siftd_theme):
        p = current_palette()
        ds = domain_styles()
    assert p.accent == Style(bold=True)              # the structural accent: weight only
    assert ds.label == Style(fg=SECONDARY)           # labels recede to the warm secondary
    assert ds.identifier != p.accent                 # the id now carries a hue, the role doesn't


# --- Config-swappable themes -------------------------------------------------
# The identity tones are roles on a SiftdPalette subclass, not literals in
# domain_styles(), so ``ui.theme`` recolours the WHOLE vocabulary as a unit. These
# pin that the nord variant is a FULL swap (no warm tone leaks through) and that
# the load-bearing role split holds — ``marker`` is its own role, not ``success``.


def test_nord_variant_swaps_the_whole_identity():
    # use_theme(nord) must move every identity role to its Nord value — the
    # half-swap (painted roles go nord, identity tones stay warm) is the bug this
    # whole mechanism fixes.
    with use_theme(SIFTD_NORD_THEME):
        ds = domain_styles()
    assert ds.assistant == Style(fg=NORD_BODY)              # body substrate swapped
    assert ds.code == Style(fg=NORD_LITERAL)                # literal hue swapped
    assert ds.identifier == Style(fg=NORD_LITERAL, bold=True)
    assert ds.tag == ds.agent == Style(fg=NORD_MARKER)      # marker swapped
    assert ds.metric == Style(fg="#d0a85f")                 # inline metric swapped
    assert ds.metric_strong == Style(fg="#ebcb8b")          # headline metric swapped
    assert ds.model == ds.summary == Style(fg="#9aa3b4")    # secondary swapped
    assert ds.faint == Style(fg="#434c5e")                  # sub-floor swapped
    # And no warm tone leaks through.
    assert ds.assistant != Style(fg=CREAM)
    assert ds.code != Style(fg=TERRACOTTA)


def test_nord_metric_tiers_stay_distinct():
    # The two-tier amber invariant must survive the transposition into Nord.
    with use_theme(SIFTD_NORD_THEME):
        ds = domain_styles()
    assert ds.metric != ds.metric_strong


def test_marker_is_its_own_role_not_success():
    # The load-bearing decision: tag/agent ride ``marker``, a categorical accent
    # distinct from the ``success`` status role. They COINCIDE in the warm theme by
    # design, but the nord variant PARTS them (Frost teal vs Aurora green) — so a
    # success-hue edit can't silently drag tags along.
    with use_theme(siftd_theme):
        assert domain_styles().tag == current_palette().success  # warm: designed coincidence
    with use_theme(SIFTD_NORD_THEME):
        ds = domain_styles()
        success = current_palette().success
    assert ds.tag == Style(fg=NORD_MARKER)        # Frost teal
    assert success == Style(fg=NORD_SUCCESS)      # Aurora green
    assert ds.tag != success                      # the two roles are genuinely parted


def test_nord_accent_carries_frost_blue_warm_stays_weight_only():
    # The one principled divergence: nord re-admits blue as accent, so structure
    # pops blue-and-bold; warm accent is weight-only, so structure pops by weight.
    # structure_style() is UNCHANGED — the behaviour falls out of the palette swap.
    with use_theme(siftd_theme):
        assert current_palette().accent == Style(bold=True)
        assert structure_style() == Style(fg=CREAM, bold=True)   # weight only, no hue
    with use_theme(SIFTD_NORD_THEME):
        assert current_palette().accent == Style(fg=NORD_ACCENT, bold=True)
        assert structure_style() == Style(fg=NORD_ACCENT, bold=True)  # blue + weight


def test_theme_for_name_resolves_variants_and_falls_back():
    assert theme_for_name("siftd") is SIFTD_THEME
    assert theme_for_name("nord") is SIFTD_NORD_THEME
    assert theme_for_name("NORD") is SIFTD_NORD_THEME       # case-insensitive
    assert theme_for_name(None) is SIFTD_THEME              # unset → default
    assert theme_for_name("dracula") is SIFTD_THEME         # unknown → default (doctor flags it)


def test_domain_styles_falls_back_to_warm_on_bare_palette():
    # A bare painted palette (no identity roles) must not crash domain_styles() — it
    # falls back to the warm identity tones. Those aren't siftd identity, so they
    # don't follow a non-siftd swap; the five universal roles still come off the
    # active palette.
    with use_palette(MONO_PALETTE):
        ds = domain_styles()
    assert ds.code == Style(fg=TERRACOTTA)         # identity tone falls back to warm
    assert ds.tag == Style(fg="#5ba8a0")
    assert ds.tool_input == MONO_PALETTE.muted     # but universal roles track the active palette


def test_structure_style_safe_on_bare_palette():
    # structure_style() shares domain_styles()' bare-palette contract: a palette with
    # text=None (MONO_PALETTE) must not crash the text.merge(accent) — it falls back
    # to the warm cream substrate.
    with use_palette(MONO_PALETTE):
        s = structure_style()
    assert s == Style(fg=CREAM).merge(MONO_PALETTE.accent)


def test_main_selects_theme_from_config(monkeypatch, capsys):
    # The integration glue: main() must wire ui.theme through theme_for_name into
    # use_theme. Override only the ui.theme config read and capture (then install)
    # the theme main() chooses.
    import painted

    import siftd.cli as cli
    import siftd.config as cfg

    captured = {}
    real_get_config = cfg.get_config
    monkeypatch.setattr(
        cfg, "get_config",
        lambda key: "nord" if key == "ui.theme" else real_get_config(key),
    )
    # Capture-only (don't install) so the process-wide theme doesn't leak into other
    # tests; the bare-palette guards keep the help render safe without it.
    monkeypatch.setattr(painted, "use_theme", lambda theme: captured.setdefault("theme", theme))

    cli.main([])  # no subcommand → prints help and returns; theme is selected first
    assert captured["theme"] is SIFTD_NORD_THEME
