"""Unit tests for the Swiss Tags renderer (output/html_fmt.render_tags).

Base lane (no litestar): renders real TagInfo dataclasses. Focus is the
curation contract — the "Most used" headline demotes auto-applied vocabulary
(``auto=True``: shell:* categories + siftd:derivative) so it can't swamp
hand-applied tags by tool-call grain count, while the namespace tree below
still lists every tag.
"""

from __future__ import annotations

from siftd.api.tags import TagInfo
from siftd.output.html_fmt import render_tags

_CTX = dict(list_base="/find", shell_base="/", pin_action_url="/tags/pin")


def _tag(name, *, convs=0, calls=0, pinned=False, auto=False):
    return TagInfo(
        name=name,
        description=None,
        created_at="2026-06-01T00:00:00Z",
        conversation_count=convs,
        workspace_count=0,
        tool_call_count=calls,
        exchange_count=0,
        prompt_count=0,
        response_count=0,
        pinned=pinned,
        auto=auto,
    )


def _zone_body(html: str, label: str) -> str:
    """Return the HTML of the zone whose head micro-label is ``label``.

    Zones are ``<section class="zone ...">`` siblings; slice from this zone's
    head marker to the next section boundary.
    """
    marker = f">{label}</span>"
    start = html.index(marker)
    rest = html[start:]
    nxt = rest.find('<section class="zone', 1)
    return rest if nxt == -1 else rest[:nxt]


def test_most_used_demotes_auto_vocabulary():
    # An auto shell tag dwarfs a hand tag by raw count, yet must not appear in
    # the headline; the hand tag (far smaller) does.
    shell = _tag("shell:vcs", calls=5000, auto=True)
    hand = _tag("topic:refactor", convs=12)
    html = render_tags([shell, hand], **_CTX)

    most_used = _zone_body(html, "Most used")
    assert "topic:refactor" in most_used
    assert "shell:vcs" not in most_used

    # But the auto tag is still present below, in its namespace tree zone.
    shell_zone = _zone_body(html, "shell:")
    assert "vcs" in shell_zone


def test_most_used_absent_when_only_auto_tags():
    # If every unpinned tag is auto vocabulary, there is no curation headline.
    html = render_tags([_tag("shell:test", calls=900, auto=True)], **_CTX)
    assert ">Most used</span>" not in html
    # ...but the tree still renders it.
    assert "test" in _zone_body(html, "shell:")


def test_pinned_auto_tag_still_pins():
    # auto only governs the "Most used" demotion; a user can still pin one.
    html = render_tags([_tag("shell:vcs", calls=5000, pinned=True, auto=True)], **_CTX)
    pinned = _zone_body(html, "Pinned")
    assert "shell:vcs" in pinned
