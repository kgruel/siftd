"""Serve-lane tests for the Swiss shell + folio/stub routes (Phase B slice 1).

Fires real requests through TestClient so the IA flip is exercised end-to-end:
the single-surface left-rail shell, the live Transcript folio, the .stub
placeholders for the not-yet-authored views, and the deep-link remap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.testing import TestClient

from siftd.serve.app import create_app
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)
from siftd.storage.usage_rollup import rebuild_rollups


def _make_db(path: Path) -> tuple[Path, str]:
    """Returns (db_path, conversation ULID). The ULID is what deep links carry —
    get_conversation resolves conversations.id, not the external_id."""
    conn = create_database(path)
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2026-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-opus")
    p = get_or_create_provider(conn, "anthropic")
    cid = insert_conversation(
        conn, external_id="c-folio", harness_id=h, workspace_id=w,
        started_at="2026-01-15T10:00:00Z",
    )
    for i in range(2):
        ts = f"2026-01-15T10:0{i}:00Z"
        pid = insert_prompt(conn, cid, f"p-{i}", ts)
        insert_prompt_content(conn, pid, 0, "text", f'{{"text": "ask number {i}"}}')
        rid = insert_response(conn, cid, pid, m, p, f"r-{i}", ts, input_tokens=10, output_tokens=5)
        insert_response_content(conn, rid, 0, "text", f'{{"text": "reply number {i}"}}')
    conn.commit()
    conn.close()
    return path, cid


@pytest.fixture
def ctx(tmp_path):
    db, cid = _make_db(tmp_path / "team.db")
    with TestClient(app=create_app(db_path=db, auth_config=None)) as c:
        yield c, cid


def test_shell_is_swiss_single_surface(ctx):
    client, _cid = ctx
    body = client.get("/").text
    # Swiss chrome, not the old two-pane instrument.
    assert 'class="chrome chrome--swiss"' in body
    assert 'class="sw-rail"' in body
    assert 'data-theme="swiss"' in body
    assert 'id="divider"' not in body          # drag-divider gone
    assert "id=\"list-pane\"" not in body       # two-pane scaffolding gone
    assert "prismjs" not in body                # prism dropped
    # All six rail views present; Transcript mounts the live folio.
    for vid in ("sessions", "search", "transcript", "tags", "workspaces", "stats"):
        assert f'data-view="{vid}"' in body
    assert 'id="main"' in body and 'hx-get="/folio"' in body
    # Scripts present in the new shell.
    assert "/static/enhance.js" in body
    assert "/static/auth.js" in body


def test_shell_exposes_auth_dom_hooks(ctx):
    """auth.js (rebound this slice) targets .sw-foot for sign-out and #main for
    the login overlay — if these hooks vanish, a 401 leaves a dead login box."""
    client, _cid = ctx
    body = client.get("/").text
    assert 'class="sw-foot"' in body
    assert 'id="main"' in body


def test_folio_renders_latest_conversation(ctx):
    client, _cid = ctx
    body = client.get("/folio").text
    assert 'class="folio"' in body
    assert 'data-view="transcript"' in body
    assert 'class="folio__nav"' in body
    assert 'class="folio__body"' in body
    assert 'class="folio__ledger"' in body
    assert "reply number 0" in body  # real conversation content


def test_folio_by_id(ctx):
    client, cid = ctx
    body = client.get("/folio", params={"id": cid}).text
    assert 'class="folio"' in body
    assert "ask number 1" in body


def test_stub_view_carries_head_metadata(ctx):
    client, _cid = ctx
    body = client.get("/view/sessions").text
    assert 'class="stub"' in body
    assert 'data-view="sessions"' in body
    assert 'data-title="Sessions"' in body


def test_find_view_is_live_unified_surface(ctx):
    """Search is no longer a stub: /find is the live unified find view — a host
    that composes the control strip (/meta) and the conversation list (/query)."""
    client, _cid = ctx
    body = client.get("/find").text
    assert 'class="find"' in body
    assert 'data-view="search"' in body
    # Composes the two existing reads, not a new renderer.
    assert 'id="filters"' in body and 'hx-get="/meta"' in body
    assert 'id="list"' in body and 'hx-get="/query"' in body


def test_search_nav_links_live_find_not_stub(ctx):
    """The Search rail item points at the live /find now, not the /view/search
    stub — clicking it must mount the find view, never the placeholder."""
    client, _cid = ctx
    body = client.get("/").text
    assert 'data-view="search"' in body and 'hx-get="/find"' in body
    assert 'hx-get="/view/search"' not in body


def test_find_deep_link_propagates_query(ctx):
    """?q= deep-links through the shell to /find, which seeds both the control
    strip (prefill) and the initial list with the term."""
    client, _cid = ctx
    shell = client.get("/", params={"q": "needle"}).text
    assert "/find?q=needle" in shell
    find = client.get("/find", params={"q": "needle"}).text
    assert "/meta?search=needle" in find
    assert "/query?search=needle" in find


def test_meta_has_content_search_box(ctx):
    """The control strip leads with a content-search box that targets the list."""
    client, _cid = ctx
    body = client.get("/meta").text
    assert 'name="search"' in body and 'filter-input--q' in body
    # Deep-link prefill fills the box value.
    assert 'value="needle"' in client.get("/meta", params={"search": "needle"}).text


def test_find_box_punctuation_does_not_500(ctx):
    """The find box is untrusted text fed into FTS5 MATCH. Bare punctuation
    (", :, *, (, AND) must sanitize to a clean list, never an fts5 syntax 500."""
    client, _cid = ctx
    for raw in ('"', "a:b", "*", "foo AND", "(", '")'):
        resp = client.get("/query", params={"search": raw})
        assert resp.status_code == 200, f"{raw!r} -> {resp.status_code}"


def test_deep_link_id_mounts_folio(ctx):
    client, cid = ctx
    body = client.get("/", params={"id": cid}).text
    assert f'hx-get="/folio?id={cid}"' in body
    assert 'data-view="transcript"' in body and 'aria-current="page"' in body


# --- Stats dashboard (Phase B slice 2) -------------------------------------


def test_dashboard_route_renders_swiss_dashboard(ctx):
    client, _cid = ctx
    body = client.get("/dashboard").text
    assert 'class="dash"' in body
    assert 'data-view="stats"' in body
    assert "Model mix" in body and "Workspace mix" in body


def test_stats_nav_links_live_dashboard_not_stub(ctx):
    """The Stats rail item points at the live /dashboard now, not /view/stats —
    clicking it must mount the dashboard, never the placeholder."""
    client, _cid = ctx
    body = client.get("/").text
    assert 'hx-get="/dashboard"' in body
    assert 'hx-get="/view/stats"' not in body


# NOTE: the pre-rollup (v8) degrade-to-stub test was removed in the 0.9.0
# integration. Its premise — "the live DB is intentionally v8 until the 0.9.0
# rollup migration" — is exactly what this branch eliminates: every served DB is
# opened via open_database(), which auto-migrates a stale-but-writable DB to the
# current schema (rollup present) or raises SchemaUpgradeRequiredError on a
# read-only stale mount. A v11 DB always has usage_by_conv_model, so the dashboard
# can no longer encounter a missing-rollup state through normal operation, and the
# storage-direct existence check it required violated the serve→API boundary.


def _make_owned_db(path: Path) -> Path:
    """Two tenants, priced: alice owns /alice-proj ($3), bob owns /bob-proj ($6).

    The global (owner=None) total is $9 — so a dashboard that shows $9 or leaks
    /bob-proj to alice is the cross-tenant IDOR. Pricing is seeded so cost is a
    real number (not the unpriced None), making the leak visible as a value.
    """
    conn = create_database(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    m = get_or_create_model(conn, "claude-x")
    p = get_or_create_provider(conn, "anthropic")
    conn.execute(
        "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok)"
        " VALUES ('pr', ?, ?, 3.0, 15.0)",
        (m, p),
    )

    def _owned(ext, ws_path, owner, mtok):
        ws = get_or_create_workspace(conn, ws_path, "2026-01-01T00:00:00Z")
        cid = insert_conversation(
            conn, external_id=ext, harness_id=h, workspace_id=ws,
            started_at="2026-01-15T10:00:00Z",
        )
        pid = insert_prompt(conn, cid, ext + "p", "2026-01-15T10:00:00Z")
        insert_response(
            conn, cid, pid, m, p, ext + "r", "2026-01-15T10:00:00Z",
            input_tokens=mtok * 1_000_000, output_tokens=0,
        )
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, owner, None, "2026-01-15T10:00:00Z"),
        )

    _owned("cA", "/alice-proj", "alice", 1)  # $3
    _owned("cB", "/bob-proj", "bob", 2)      # $6
    rebuild_rollups(conn)
    conn.commit()
    conn.close()
    return path


def test_dashboard_scopes_to_authenticated_owner(tmp_path):
    """End-to-end IDOR guard: the dashboard sums cost/usage across conversations,
    so the owner= on every read is the only thing scoping it. An alice-authed
    request must show alice's workspace + cost, never bob's or the global $9.

    A single-owner fixture would pass even if the route leaked — the two-owner
    assertion is what discriminates. Static-token auth maps identity→request.user.sub,
    which _effective_owner threads into the reads.
    """
    db = _make_owned_db(tmp_path / "owned.db")
    auth = {"static_token": "s3cret", "identity": "alice"}
    with TestClient(app=create_app(db_path=db, auth_config=auth)) as client:
        body = client.get("/dashboard", headers={"Authorization": "Bearer s3cret"}).text

    # Alice sees her own workspace, never bob's (fmt_workspace strips the slash).
    assert "alice-proj" in body
    assert "bob-proj" not in body
    # Scoped to alice's single conversation, not the global two.
    assert 'data-count="1"' in body
    # Headline cost is alice's $3.00, never the cross-tenant $9.00.
    assert "$3.00" in body
    assert "$9.00" not in body
