"""Unit tests for the Swiss Workspaces renderer (output/html_fmt.render_workspaces).

Base lane (no litestar): renders dict rows shaped like list_workspaces output.
Focus is the two-tier contract — pinned + recent cards in the head as shortcuts
OVER the full body list, and the honest bar rule (the bar encodes the active
sort measure; the recency sort drops it so row order never lies).
"""

from __future__ import annotations

from siftd.output.html_fmt import render_workspaces

_CTX = dict(
    detail_base="/workspace",
    shell_base="/",
    pin_action_url="/workspace/pin",
    sort_base="/view/workspaces",
)


def _row(wid, path, *, convs=1, inp=0, out=0, cost=None, last="2026-01-01T00:00:00Z", pinned=0):
    return {
        "id": wid, "path": path, "git_remote": None, "convs": convs,
        "last_activity": last, "inp": inp, "out": out, "cost": cost, "pinned": pinned,
    }


def _zone_body(html: str, label: str) -> str:
    marker = f">{label}</span>"
    start = html.index(marker)
    rest = html[start:]
    nxt = rest.find('<section class="zone', 1)
    return rest if nxt == -1 else rest[:nxt]


def test_pinned_appears_in_head_and_body():
    rows = [
        _row("01A", "/Code/alpha", convs=3, pinned=1),
        _row("01B", "/Code/beta", convs=9),
    ]
    html = render_workspaces(rows, sort="sessions", **_CTX)

    pinned_zone = _zone_body(html, "Pinned")
    assert 'class="card card--ws"' in pinned_zone
    assert "alpha" in pinned_zone
    # Cards are shortcuts OVER the list, not a partition: the pinned ws is also a
    # body row. Body rows uniquely carry class="ledger__row".
    assert html.count('class="ledger__row"') == 2  # both alpha + beta in the body
    # The pinned ws is not duplicated into the Recent strip.
    assert "Recent" in html
    assert "alpha" not in _zone_body(html, "Recent")


def test_recent_orders_by_activity_and_excludes_pinned():
    rows = [
        _row("01A", "/Code/alpha", last="2026-01-01T00:00:00Z", pinned=1),
        _row("01B", "/Code/beta", last="2026-03-01T00:00:00Z"),
        _row("01C", "/Code/gamma", last="2026-02-01T00:00:00Z"),
    ]
    html = render_workspaces(rows, sort="sessions", **_CTX)
    recent = _zone_body(html, "Recent")
    # beta (Mar) before gamma (Feb); alpha is pinned, excluded from Recent.
    assert recent.index("beta") < recent.index("gamma")
    assert "alpha" not in recent


def test_bar_follows_active_sort():
    rows = [_row("01A", "/Code/alpha", convs=7, inp=100, out=20, cost=5.0)]

    sessions = render_workspaces(rows, sort="sessions", **_CTX)
    assert "ledger--ws is-ranked" in sessions
    assert 'class="ledger__bar" data-n="7"' in sessions  # bar = sessions count

    tokens = render_workspaces(rows, sort="tokens", **_CTX)
    assert 'class="ledger__bar" data-n="120"' in tokens  # bar = inp+out

    cost = render_workspaces(rows, sort="cost", **_CTX)
    assert 'class="ledger__bar" data-n="5.0"' in cost  # bar = cost


def test_recency_sort_drops_the_bar():
    rows = [_row("01A", "/Code/alpha", convs=7, inp=100, out=20)]
    html = render_workspaces(rows, sort="recent", **_CTX)
    assert "is-ranked" not in html
    # No magnitude bar in the body under recency (cards never carry one either).
    assert 'class="ledger__bar"' not in html


def test_pin_button_carries_ws_and_active_sort():
    rows = [_row("01A", "/Code/alpha", pinned=0)]
    html = render_workspaces(rows, sort="cost", **_CTX)
    # The pin posts the ws id + the active sort so the re-render preserves order.
    assert 'hx-post="/workspace/pin"' in html
    assert '&quot;ws&quot;: &quot;01A&quot;' in html
    assert '&quot;sort&quot;: &quot;cost&quot;' in html
    assert '&quot;action&quot;: &quot;pin&quot;' in html


def test_sort_control_marks_active():
    rows = [_row("01A", "/Code/alpha")]
    html = render_workspaces(rows, sort="tokens", **_CTX)
    assert 'hx-get="/view/workspaces?sort=tokens"' in html
    assert 'class="ws-sort__opt is-active"' in html
    assert 'data-ws-filter' in html  # the filter input is present


def test_empty_renders_stub_row():
    html = render_workspaces([], sort="sessions", **_CTX)
    assert "no workspaces yet" in html
    assert 'data-count="0"' in html
