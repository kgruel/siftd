"""Unit tests for the Swiss Tags renderer (output/html_fmt.render_tags).

Base lane (no litestar): renders real TagInfo dataclasses. Focus is the
index contract — auto-applied vocabulary (``auto=True``: shell:* categories +
siftd:derivative) lives in its own Machine vocabulary book, kept out of the
hand-applied Subject index where its tool-call grain would swamp the
conversation tags; pinned tags surface in the Marked section.
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


def _book_body(html: str, label: str) -> str:
    """Return the HTML of the index book/section whose head micro-label is
    ``label`` (Marked / Subject index / Machine vocabulary).

    Top-level sections are ``<section class="index__...">`` siblings; slice from
    this section's head marker to the next index__ section boundary (idx-group
    sections inside a book use ``class="idx-group"``, so they don't split it).
    """
    marker = f">{label}</span>"
    start = html.index(marker)
    rest = html[start:]
    nxt = rest.find('<section class="index__', 1)
    return rest if nxt == -1 else rest[:nxt]


def test_machine_vocab_kept_out_of_subject_index():
    # An auto shell tag dwarfs a hand tag by raw count, yet must not appear in
    # the Subject index; the hand tag does. The auto tag lives in its own book.
    shell = _tag("shell:vcs", calls=5000, auto=True)
    hand = _tag("topic:refactor", convs=12)
    html = render_tags([shell, hand], **_CTX)

    subject = _book_body(html, "Subject index")
    assert "refactor" in subject
    assert "shell:vcs" not in subject

    # The auto tag is present in the Machine vocabulary book.
    machine = _book_body(html, "Machine vocabulary")
    assert "vcs" in machine


def test_subject_index_absent_when_only_auto_tags():
    # If every tag is auto vocabulary, there is no Subject index book.
    html = render_tags([_tag("shell:test", calls=900, auto=True)], **_CTX)
    assert ">Subject index</span>" not in html
    # ...but the Machine vocabulary book still renders it.
    assert "test" in _book_body(html, "Machine vocabulary")


def test_pinned_auto_tag_surfaces_in_marked():
    # auto governs the Subject/Machine split; a user can still pin one, and it
    # surfaces (by full name) in the Marked section.
    html = render_tags([_tag("shell:vcs", calls=5000, pinned=True, auto=True)], **_CTX)
    marked = _book_body(html, "Marked")
    assert "shell:vcs" in marked
