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


def test_query_rows_mount_folio_in_main(ctx):
    """Rows are the click path from Find to a conversation. They must target
    #main (the only swap target in the Swiss shell) and mount the folio —
    the old #detail target matched nothing, so clicks were silent no-ops."""
    client, cid = ctx
    body = client.get("/query").text
    assert 'hx-get="/folio?id=' in body
    assert 'hx-target="#main"' in body
    assert 'hx-push-url="/?id=' in body
    assert "#detail" not in body


def test_folio_hosts_tag_and_export_curation(ctx):
    """The folio is the single detail surface, so it owns the tag/export
    affordances the deleted /query?id= detail mode used to carry."""
    client, cid = ctx
    body = client.get("/folio", params={"id": cid}).text
    assert 'class="ledger__curation"' in body
    assert 'hx-post="/tag"' in body          # interactive tag add/remove
    assert 'id="tags-' in body               # stable swap target for /tag
    assert "format=md" in body and "format=json" in body  # export links


def test_stub_view_carries_head_metadata(ctx):
    # Every nav view is live now; the /view/{name} stub still answers unknown
    # names (defensive) and must carry the head metadata enhance.js needs.
    client, _cid = ctx
    body = client.get("/view/nonexistent").text
    assert 'class="stub"' in body
    assert 'data-view="nonexistent"' in body
    assert 'data-title="Nonexistent"' in body


def test_workspaces_view_is_live(ctx):
    """Workspaces is no longer a stub: a drillable master ledger whose rows mount
    the per-workspace detail keyed on ``ws`` (distinct from the folio's id)."""
    client, _cid = ctx
    body = client.get("/view/workspaces").text
    assert 'class="stub"' not in body
    assert 'data-view="workspaces"' in body and 'data-title="Workspaces"' in body
    # body list (default magnitude sort → is-ranked bar column)
    assert 'ledger ledger--usage ledger--ws is-ranked' in body
    assert 'hx-get="/workspace?ws=' in body and 'hx-target="#main"' in body


def test_sessions_view_is_live(ctx):
    """Sessions is no longer a stub: live zone + day-grouped ingested timeline,
    rows mounting the folio like every conversation row."""
    client, _cid = ctx
    body = client.get("/view/sessions").text
    assert 'class="stub"' not in body
    assert 'data-view="sessions"' in body and 'data-title="Sessions"' in body
    assert 'class="zone zone--live"' in body      # ctx app: live endpoints on
    assert 'class="day__head"' in body            # day grouping over real rows
    assert 'class="hist"' in body                 # hour-of-day buckets
    assert 'hx-get="/folio?id=' in body and 'hx-target="#main"' in body


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


def test_dashboard_reads_through_stats_cache(tmp_path, monkeypatch):
    """get_stats is the dashboard's full-table sweep: the first load pays it
    once and writes the per-owner cache; repeat loads serve from cache. A DB
    write invalidates (require_fresh ties the cache to db mtime), and the
    cached numbers stay owner-scoped — alice's cache never feeds bob."""
    import os

    import siftd.api.stats as stats_mod

    db = _make_owned_db(tmp_path / "owned.db")
    calls = {"n": 0}
    real_get_stats = stats_mod.get_stats

    def counting_get_stats(**kw):
        calls["n"] += 1
        return real_get_stats(**kw)

    monkeypatch.setattr("siftd.api.stats.get_stats", counting_get_stats)

    auth = {"static_token": "s3cret", "identity": "alice"}
    hdrs = {"Authorization": "Bearer s3cret"}
    with TestClient(app=create_app(db_path=db, auth_config=auth)) as client:
        first = client.get("/dashboard", headers=hdrs).text
        assert calls["n"] == 1
        second = client.get("/dashboard", headers=hdrs).text
        assert calls["n"] == 1  # served from alice's cache
        assert "bob-proj" not in second and 'data-count="1"' in second
        assert first == second

        # A DB write invalidates: the next load recomputes and re-caches.
        st = os.stat(db)
        os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert client.get("/dashboard", headers=hdrs).status_code == 200
        assert calls["n"] == 2

    # Same DB, different identity: bob never reads alice's cached totals.
    auth_bob = {"static_token": "s3cret", "identity": "bob"}
    with TestClient(app=create_app(db_path=db, auth_config=auth_bob)) as client:
        body = client.get("/dashboard", headers=hdrs).text
        assert calls["n"] == 3  # cache miss — bob computes his own
        assert "alice-proj" not in body and "bob-proj" in body


# ---------------------------------------------------------------------------
# Tags view (Phase B slice) — live index, drill into Find, owner-scoped pins
# ---------------------------------------------------------------------------


def _make_tagged_db(path: Path) -> Path:
    """One conversation bearing three conversation tags: two under the
    ``research:`` namespace + one ungrouped (``bug``) — enough to exercise the
    namespace tree, the most-used zone, and the ungrouped bucket."""
    from siftd.storage.tags import apply_tag, get_or_create_tag

    conn = create_database(path)
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2026-01-01T00:00:00Z")
    cid = insert_conversation(
        conn, external_id="c-tags", harness_id=h, workspace_id=w,
        started_at="2026-01-15T10:00:00Z",
    )
    for name in ("research:auth", "research:embeddings", "bug"):
        apply_tag(conn, "conversation", cid, get_or_create_tag(conn, name))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def tags_ctx(tmp_path):
    db = _make_tagged_db(tmp_path / "tags.db")
    with TestClient(app=create_app(db_path=db, auth_config=None)) as c:
        yield c


def test_tags_view_is_live_not_stub(tags_ctx):
    body = tags_ctx.get("/view/tags").text
    assert 'class="stub"' not in body
    assert 'data-view="tags"' in body and 'data-title="Tags"' in body
    assert 'data-count="3"' in body
    assert 'class="ledger ledger--tags"' in body
    # namespace tree: a research: band groups its two leaves; bug is ungrouped
    assert "research:</span>" in body
    assert "2 tags" in body
    assert ">ungrouped<" in body
    # derived zones
    assert "Most used" in body


def test_tags_rows_drill_into_find(tags_ctx):
    body = tags_ctx.get("/view/tags").text
    assert 'hx-get="/find?tag=research%3Aauth"' in body
    assert 'hx-target="#main"' in body
    assert 'hx-push-url="/?tag=research%3Aauth"' in body
    # an unpinned tag offers the pin affordance
    assert 'class="pin"' in body


def test_tag_deep_link_propagates_through_shell_and_find(tags_ctx):
    shell = tags_ctx.get("/", params={"tag": "research:auth"}).text
    assert 'hx-get="/find?tag=research%3Aauth"' in shell
    find = tags_ctx.get("/find", params={"tag": "research:auth"}).text
    assert "/meta?tag=research%3Aauth" in find and "/query?tag=research%3Aauth" in find
    meta = tags_ctx.get("/meta", params={"tag": "research:auth"}).text
    assert 'value="research:auth" selected' in meta


def _make_owned_tagged_db(path: Path) -> Path:
    """Two tenants, both bearing one shared tag. A pin is per-owner: alice
    pinning ``shared`` must never make it pinned in bob's view."""
    from siftd.storage.tags import apply_tag, get_or_create_tag

    conn = create_database(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2026-01-01T00:00:00Z")
    shared = get_or_create_tag(conn, "shared")
    for ext, owner in (("cA", "alice"), ("cB", "bob")):
        cid = insert_conversation(
            conn, external_id=ext, harness_id=h, workspace_id=w,
            started_at="2026-01-15T10:00:00Z",
        )
        apply_tag(conn, "conversation", cid, shared)
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, owner, None, "2026-01-15T10:00:00Z"),
        )
    conn.commit()
    conn.close()
    return path


def test_tags_pin_is_owner_scoped(tmp_path):
    """The pin write + read are owner-scoped end-to-end: alice's pin shows in her
    pinned zone and persists, but bob — who shares the tag — never sees it."""
    db = _make_owned_tagged_db(tmp_path / "owned-tags.db")
    hdrs = {"Authorization": "Bearer s3cret"}
    alice_auth = {"static_token": "s3cret", "identity": "alice"}
    bob_auth = {"static_token": "s3cret", "identity": "bob"}

    with TestClient(app=create_app(db_path=db, auth_config=alice_auth)) as alice:
        assert "zone--pinned" not in alice.get("/view/tags", headers=hdrs).text
        r = alice.post("/tag/pin", headers=hdrs, data={"action": "pin", "tag": "shared"})
        assert r.status_code == 201  # Litestar POST default; htmx swaps any 2xx
        assert "zone--pinned" in r.text and "pin--on" in r.text  # re-rendered view
        assert "zone--pinned" in alice.get("/view/tags", headers=hdrs).text  # persists

    with TestClient(app=create_app(db_path=db, auth_config=bob_auth)) as bob:
        body = bob.get("/view/tags", headers=hdrs).text
        assert "shared" in body              # bob sees the shared tag
        assert "zone--pinned" not in body    # but not alice's pin

    with TestClient(app=create_app(db_path=db, auth_config=alice_auth)) as alice:
        r = alice.post("/tag/pin", headers=hdrs, data={"action": "unpin", "tag": "shared"})
        assert "zone--pinned" not in r.text


def _make_owned_ws_db(path: Path) -> tuple[Path, str]:
    """Two tenants, both participating in one workspace. A pin is per-owner:
    alice pinning the workspace must never make it pinned in bob's view."""
    conn = create_database(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2026-01-01T00:00:00Z")
    for ext, owner in (("cA", "alice"), ("cB", "bob")):
        cid = insert_conversation(
            conn, external_id=ext, harness_id=h, workspace_id=w,
            started_at="2026-01-15T10:00:00Z",
        )
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, owner, None, "2026-01-15T10:00:00Z"),
        )
    conn.commit()
    conn.close()
    return path, w


def test_workspace_pin_is_owner_scoped(tmp_path):
    """The workspace pin write + read are owner-scoped end-to-end: alice's pin
    lifts the workspace into her head zone and persists, but bob — who shares the
    workspace — never sees it pinned."""
    db, wid = _make_owned_ws_db(tmp_path / "owned-ws.db")
    hdrs = {"Authorization": "Bearer s3cret"}
    alice_auth = {"static_token": "s3cret", "identity": "alice"}
    bob_auth = {"static_token": "s3cret", "identity": "bob"}

    with TestClient(app=create_app(db_path=db, auth_config=alice_auth)) as alice:
        assert "zone--pinned" not in alice.get("/view/workspaces", headers=hdrs).text
        r = alice.post(
            "/workspace/pin", headers=hdrs,
            data={"action": "pin", "ws": wid, "sort": "sessions"},
        )
        assert r.status_code == 201  # Litestar POST default; htmx swaps any 2xx
        assert "zone--pinned" in r.text and "pin--on" in r.text  # re-rendered view
        assert "zone--pinned" in alice.get("/view/workspaces", headers=hdrs).text  # persists

    with TestClient(app=create_app(db_path=db, auth_config=bob_auth)) as bob:
        body = bob.get("/view/workspaces", headers=hdrs).text
        assert "proj" in body              # bob sees the shared workspace
        assert "zone--pinned" not in body  # but not alice's pin

    with TestClient(app=create_app(db_path=db, auth_config=alice_auth)) as alice:
        r = alice.post(
            "/workspace/pin", headers=hdrs,
            data={"action": "unpin", "ws": wid, "sort": "sessions"},
        )
        assert "zone--pinned" not in r.text


def test_workspace_sort_param(tmp_path):
    """?sort= reorders the body and toggles the magnitude bar: a magnitude sort
    is is-ranked (bar column present); the recency sort drops it."""
    db, _ = _make_owned_ws_db(tmp_path / "ws-sort.db")
    hdrs = {"Authorization": "Bearer s3cret"}
    auth = {"static_token": "s3cret", "identity": "alice"}
    with TestClient(app=create_app(db_path=db, auth_config=auth)) as c:
        ranked = c.get("/view/workspaces?sort=tokens", headers=hdrs).text
        assert "ledger--ws is-ranked" in ranked
        assert 'class="ws-sort__opt is-active"' in ranked
        recent = c.get("/view/workspaces?sort=recent", headers=hdrs).text
        assert "is-ranked" not in recent
