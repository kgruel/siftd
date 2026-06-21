"""Tests for siftd.output.theme — the bespoke "warm obsidian" palette + domain styles.

These pin the load-bearing decisions of the Swiss-UI palette port so a future edit
can't silently undo them:

* the two-tier amber thread — inline ``metric`` (amber-dim) is DISTINCT from the
  headline ``metric_strong`` (bright amber); collapsing them erases the slice's point;
* the identity tones painted's five-role palette can't name (amber ×2, secondary,
  teal) carry their Swiss hex values;
* the five painted roles take the warm-obsidian hues (the accent stays bold).

Hues trace to the served stylesheet ``src/siftd/serve/static/siftd.css`` (the design
source of truth): --amber #c9a84c, --amber-dim #8a7a3a, --fg-secondary #8a8a9a,
--teal #5ba8a0, --accent #6ba3d6, --warning #d4a843, --error #c85050, --muted #505862.
"""

from painted import Style, current_palette, use_theme

from siftd.output.theme import domain_styles, siftd_theme


def test_amber_tiers_are_distinct():
    # The load-bearing invariant: the inline thread and the headline tier must not
    # collapse to one colour (the pre-split state this slice deliberately undid).
    with use_theme(siftd_theme):
        ds = domain_styles()
    assert ds.metric != ds.metric_strong


def test_identity_tones_carry_their_swiss_hues():
    with use_theme(siftd_theme):
        ds = domain_styles()
    assert ds.metric == Style(fg="#8a7a3a")         # amber-dim — inline figures
    assert ds.metric_strong == Style(fg="#c9a84c")  # bright amber — headline figures
    assert ds.model == ds.summary == Style(fg="#8a8a9a")  # the third (secondary) tone
    assert ds.tag == ds.agent == Style(fg="#5ba8a0")      # teal


def test_thinking_defaults_to_secondary_italic_not_muted():
    # Faithful to Swiss `.thinking { color: var(--fg-secondary); font-style: italic }`
    # — brighter than the old muted default, so reasoning reads as present.
    with use_theme(siftd_theme):
        ds = domain_styles()
        muted = current_palette().muted
    assert ds.thinking == Style(fg="#8a8a9a", italic=True)
    assert ds.thinking != muted.merge(Style(italic=True))


def test_palette_roles_take_the_warm_obsidian_hues():
    with use_theme(siftd_theme):
        p = current_palette()
    assert p.accent == Style(bold=True)     # no hue — structure pops by weight
    assert p.success == Style(fg="#5ba8a0")  # teal
    assert p.warning == Style(fg="#d4a843")  # gold
    assert p.error == Style(fg="#c85050")    # soft red
    assert p.muted == Style(fg="#505862")    # slate


def test_accent_is_weight_not_hue():
    # The structural accent carries no colour: identifiers / prompt markers /
    # tool-names pop by bold weight (the blue read as a cool intruder in the warm
    # identity), and field labels recede to secondary. Gold stays the only colour pop.
    with use_theme(siftd_theme):
        ds = domain_styles()
    assert ds.identifier == Style(bold=True)             # bold, no fg
    assert ds.identifier == ds.prompt == ds.tool_name    # one weight-accent, three roles
    assert ds.label == Style(fg="#8a8a9a")               # labels recede to secondary
    assert ds.label != ds.identifier                     # a label is not the accent
