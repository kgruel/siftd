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

from painted import Style, current_palette, use_theme

from siftd.output.theme import domain_styles, siftd_theme

CREAM = "#e4d9bf"       # body text — warm + bright
TERRACOTTA = "#d69a58"  # code + identifiers — the warm literal hue
SECONDARY = "#a89a82"   # third weight (warmed from the old cool #8a8a9a)
WARM_MUTED = "#8f836a"  # the dim floor (warmed/raised from the old cool slate #505862)


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
