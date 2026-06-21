"""Serve-lane smoke for the Swiss Workspaces view (master ledger + detail).

Drives the real ASGI app via TestClient (auth off → unscoped/local), asserting:
  - /view/workspaces is live (not a stub), rows drill into /workspace?ws=,
    cost is honest (— for unpriced, $ for priced — never a fabricated $0),
  - the duplicate-workspace caveat surfaces (unscoped) when twins exist,
  - /workspace?ws= renders the detail with an honest headline,
  - an unknown ws id degrades to the not-found stub (not a 500),
  - the ?ws= shell deep-link mounts the workspace detail with the rail lit.

Owner-scoping / IDOR for workspace_detail is covered at the api layer
(test_workspace_detail.py); these tests run unscoped.
"""

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.testing import TestClient

from siftd.serve.app import create_app
from test_workspaces_master import _build, _build_twin


def _client(db):
    # Fragment endpoints are htmx-only; a direct (non-htmx) GET 303s to the
    # canonical /?view=… shell. These tests exercise the fragment, so they fire
    # as htmx (the header htmx sets on every fetch).
    c = TestClient(app=create_app(db_path=db, auth_config=None))
    c.headers.update({"HX-Request": "true"})
    return c


def test_view_workspaces_is_live_with_drill_and_honest_cost(tmp_path):
    db = tmp_path / "d.db"
    _build(db, price=True)  # projA priced ($1.25), projB unpriced (—)
    with _client(db) as c:
        body = c.get("/view/workspaces").text

    assert 'class="stub"' not in body
    assert 'data-view="workspaces"' in body and 'data-title="Workspaces"' in body
    # Body list; default sort is a magnitude (sessions), so the bar column rides
    # via the is-ranked modifier.
    assert 'ledger ledger--usage ledger--ws is-ranked' in body
    # Rows drill into the per-workspace detail, keyed on ws (not the folio's id).
    assert 'hx-get="/workspace?ws=' in body and 'hx-target="#main"' in body
    # Honest cost: the priced workspace shows a dollar figure, the unpriced one
    # an em dash — and no fabricated $0.00 anywhere.
    assert "$1.25" in body
    assert "&mdash;" in body
    assert "$0.00" not in body


def test_view_workspaces_surfaces_duplicate_caveat_when_unscoped(tmp_path):
    db = tmp_path / "twin.db"
    _build_twin(db)
    with _client(db) as c:
        body = c.get("/view/workspaces").text

    assert 'class="ws-caveat"' in body
    assert "share a git remote" in body
    assert "siftd migrate --merge-workspaces" in body
    # The same-remote twins stay as two distinguishable body rows (same basename,
    # different parent), not collapsed. (The head cards drill too, so count the
    # body ledger rows, which uniquely carry class="ledger__row".)
    assert body.count('class="ledger__row"') == 2


def test_view_workspaces_no_caveat_without_duplicates(tmp_path):
    db = tmp_path / "d.db"
    _build(db)
    with _client(db) as c:
        body = c.get("/view/workspaces").text
    assert 'class="ws-caveat"' not in body


def test_workspace_detail_route_honest_headline_unpriced(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)  # no pricing
    with _client(db) as c:
        body = c.get(f"/workspace?ws={ws_a}").text

    assert 'class="dash ws-detail"' in body
    assert 'data-view="workspaces"' in body
    assert 'class="ws-detail__back"' in body and 'hx-get="/view/workspaces"' in body
    # Headline cost is an em dash, not a fabricated $0.00.
    assert "&mdash;" in body
    assert "$0.00" not in body


def test_workspace_detail_route_priced_headline(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db, price=True)
    with _client(db) as c:
        body = c.get(f"/workspace?ws={ws_a}").text
    assert "$1.25" in body


def test_workspace_detail_route_unknown_id_is_stub_not_500(tmp_path):
    db = tmp_path / "d.db"
    _build(db)
    with _client(db) as c:
        resp = c.get("/workspace?ws=01DOESNOTEXIST0000000000")
    assert resp.status_code == 200
    assert 'class="stub"' in resp.text
    assert 'data-view="workspaces"' in resp.text


def test_shell_ws_deeplink_mounts_workspace_detail(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)
    with _client(db) as c:
        body = c.get(f"/?ws={ws_a}").text
    # The shell mounts the workspace detail into #main and lights the rail item.
    assert f'hx-get="/workspace?ws={ws_a}"' in body
    assert 'data-view="workspaces" aria-current="page"' in body
