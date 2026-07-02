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

# The fragment endpoints (/folio, /dashboard, /find, /view/*, …) are htmx-only:
# a direct (non-htmx) GET now 303s to the canonical /?view=… shell URL (the
# URL-as-state foundation). These tests exercise the fragment surface, so they
# fire as htmx requests — the same header htmx sets on every fetch.
_HX = {"HX-Request": "true"}


def _hx_client(app):
    """A TestClient that fires every request as htmx (the fragment surface).

    litestar's TestClient takes no constructor ``headers``, so set the default on
    the underlying httpx client; it applies to all .get/.post calls."""
    c = TestClient(app=app)
    c.headers.update(_HX)
    return c


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
    insert_tool_call,
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
        if i == 0:
            # A tool call so trace mode has interleaved I/O to inline. Reading
            # mode keeps it out of the body (the ledger/chip own it) — the two
            # modes are exercised against the same conversation below.
            insert_response_content(
                conn, rid, 1, "tool_use",
                '{"id": "toolu_1", "name": "Read", "input": {"file_path": "x.py"}}',
            )
            insert_tool_call(
                conn, rid, cid, None, "toolu_1",
                '{"file_path": "x.py"}', '{"text": "file body"}', "success", ts,
            )
            insert_response_content(
                conn, rid, 2, "thinking", '{"thinking": "considering the approach"}',
            )
    conn.commit()
    conn.close()
    return path, cid


@pytest.fixture
def ctx(tmp_path):
    db, cid = _make_db(tmp_path / "team.db")
    with _hx_client(create_app(db_path=db, auth_config=None)) as c:
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


def test_shell_versions_own_static_assets(ctx):
    # Cache-bust query on our own CSS/JS so a stale browser cache can't survive
    # an edit; the static router ignores the query and still serves the file.
    client, _cid = ctx
    body = client.get("/").text
    assert "/static/siftd.css?v=" in body
    assert "/static/enhance.js?v=" in body
    assert "/static/auth.js?v=" in body
    # The asset still loads with the query (router ignores it).
    assert client.get("/static/siftd.css?v=123").status_code == 200
    # Vendored, version-pinned assets are left un-queried.
    assert '/static/vendor/htmx.min.js"' in body


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


def test_folio_reading_mode_keeps_tool_io_out_of_body(ctx):
    client, cid = ctx
    body = client.get("/folio", params={"id": cid}).text
    assert 'data-mode="reading"' in body
    assert "tool-call" not in body  # reading body: tools in the ledger, not inline
    assert "considering the approach" not in body  # thinking not fetched in reading


def test_folio_trace_mode_inlines_tool_io(ctx):
    # The route must resolve a tools/thinking-visible fidelity from ?mode=trace
    # so get_conversation actually FETCHES tool input/result + thinking (it gates
    # both on fidelity.shows). Unit tests pass fidelity directly and can't prove
    # this route-level resolution — if the route dropped tools= or thinking=, the
    # tool I/O or the agent's reasoning would silently vanish from the trace.
    client, cid = ctx
    body = client.get("/folio", params={"id": cid, "mode": "trace"}).text
    assert 'data-mode="trace"' in body
    assert 'class="tool-call"' in body
    assert "Read" in body
    assert '<details class="thinking">' in body
    assert "considering the approach" in body


def test_folio_mode_toggle_offers_both_modes(ctx):
    client, cid = ctx
    body = client.get("/folio", params={"id": cid}).text
    assert 'class="folio-mode"' in body
    assert "mode=trace" in body and "mode=reading" in body


def test_find_context_unfold_renders_reading_preview(ctx):
    # Slice 2a: the unfold is a READING preview (prose around the match), not the
    # trace — so it renders the .turn prose and keeps tool I/O OUT of the slice
    # body (tools belong to the chip/ledger, and the full thing is one
    # "open in folio" away). The fixture has a tool call in turn 0; it must not
    # appear inline in the unfold.
    client, cid = ctx
    r = client.get("/find/context", params={"id": cid, "at": 0, "w": 2})
    assert r.status_code == 200
    assert 'class="hit-context__slice"' in r.text
    assert 'class="turn' in r.text          # prose turns rendered
    assert "reply number" in r.text          # real assistant prose in the preview
    assert 'class="tool-call"' not in r.text  # tool I/O is not inlined (reading)


def test_find_context_unfold_truncates_long_turns(tmp_path):
    # Slice 2a (#3): a giant turn is capped to a scannable preview (the full text
    # is one "open in folio" away). Assert the truncation marker appears.
    from siftd.output.html_fmt import SEARCH_PREVIEW_CHARS

    db = tmp_path / "long.db"
    conn = create_database(db)
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2026-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-opus")
    p = get_or_create_provider(conn, "anthropic")
    cid = insert_conversation(
        conn, external_id="c-long", harness_id=h, workspace_id=w,
        started_at="2026-01-15T10:00:00Z",
    )
    long_text = "lorem ipsum dolor sit amet " * 40  # well over the preview cap
    ts = "2026-01-15T10:00:00Z"
    pid = insert_prompt(conn, cid, "p-0", ts)
    insert_prompt_content(conn, pid, 0, "text", '{"text": "a short question"}')
    rid = insert_response(conn, cid, pid, m, p, "r-0", ts, input_tokens=10, output_tokens=5)
    insert_response_content(conn, rid, 0, "text", '{"text": "%s"}' % long_text)
    conn.commit()
    conn.close()

    assert len(long_text) > SEARCH_PREVIEW_CHARS
    with _hx_client(create_app(db_path=db, auth_config=None)) as c:
        body = c.get("/find/context", params={"id": cid, "at": 0, "w": 2}).text
    assert "..." in body                      # the preview is truncated
    assert long_text not in body              # the full giant turn is NOT inlined


def test_query_rows_mount_folio_in_main(ctx):
    """Rows are the click path from Find to a conversation. They must target
    #main (the only swap target in the Swiss shell) and mount the folio —
    the old #detail target matched nothing, so clicks were silent no-ops."""
    client, cid = ctx
    # A facet (workspace) browses the list (Slice 2b: bare /query is the prompt).
    body = client.get("/query", params={"workspace": "/proj"}).text
    assert 'hx-get="/folio?id=' in body
    assert 'hx-target="#main"' in body
    assert 'hx-push-url="/?id=' in body
    assert "#detail" not in body


def test_folio_hosts_tag_and_export_curation(ctx):
    """The folio is the single detail surface, so it owns the tag/export
    affordances the deleted /query?id= detail mode used to carry."""
    client, cid = ctx
    body = client.get("/folio", params={"id": cid}).text
    assert "folio__bargroup--actions" in body  # tags/export ride the command bar
    assert 'hx-post="/tag"' in body          # interactive tag add/remove
    assert 'id="tags-' in body               # stable swap target for /tag
    assert "format=md" in body and "format=json" in body  # export links


def test_folio_body_offers_element_tag_affordance(ctx):
    """WS4: each turn in the folio body carries its own hover-reveal element-tag
    section (chips + add form), distinct from the conversation section in the
    command bar."""
    client, cid = ctx
    body = client.get("/folio", params={"id": cid}).text
    assert "tag-section--elem" in body                    # per-element sections
    assert 'name="entity_type" value="prompt"' in body   # prompt block tags on prompt
    assert 'name="entity_type" value="response"' in body  # assistant block on response


def test_trace_tool_call_offers_corner_tag_affordance(ctx):
    """WS4b: in trace mode each tool-call block carries a top-right dropdown tag
    affordance (a native <details> menu, so it opens with no JS — CSP-safe). It
    is a SIBLING wrapper (.trace-block) so a collapsed block still shows it, and
    it tags the tool call itself (entity_type=tool_call), not the response."""
    client, cid = ctx
    body = client.get("/folio", params={"id": cid, "mode": "trace"}).text
    assert 'data-mode="trace"' in body
    assert "trace-block--tool" in body               # positioned wrapper
    assert 'class="tag-menu"' in body                # native-details dropdown
    assert 'name="entity_type" value="tool_call"' in body  # tags the tool call


def test_trace_tool_call_tag_roundtrip_via_post_tag(tmp_path):
    """The corner affordance's form round-trips through POST /tag: the tool call
    resolves owner-safely, tags apply/remove, and the audit records target_type
    'tool_call' keyed on the tool call's own event ULID."""
    import re

    from siftd.storage.sqlite import open_database

    db, cid = _make_db(tmp_path / "team.db")
    conn = open_database(db)
    try:
        tool_id = conn.execute("SELECT id FROM events WHERE kind='tool_call' LIMIT 1").fetchone()["id"]
    finally:
        conn.close()

    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get("/folio", params={"id": cid, "mode": "trace"}).text
        # The tool_call's own id rides its affordance form (not the response id).
        assert re.search(rf'name="id" value="{re.escape(tool_id)}"', body)

        r = client.post("/tag", data={
            "action": "apply", "id": tool_id, "entity_type": "tool_call", "tag": "iface",
        })
        assert r.status_code == 201
        assert "iface" in r.text
        assert 'name="entity_type" value="tool_call"' in r.text

        r2 = client.post("/tag", data={
            "action": "remove", "id": tool_id, "entity_type": "tool_call", "tag": "iface",
        })
        assert r2.status_code == 201
        assert "iface<" not in r2.text

    conn = open_database(db)
    try:
        row = conn.execute(
            "SELECT target_type, target FROM audit_log WHERE action = 'tag.apply'"
            " ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["target_type"] == "tool_call"
    assert row["target"] == tool_id


def test_element_tag_apply_remove_roundtrip_via_post_tag(tmp_path):
    """The generalized POST /tag applies + removes an element-kind tag, returns the
    element fragment, and audits with the resolved kind (not the wire hint)."""
    import re

    from siftd.storage.sqlite import open_database

    db, cid = _make_db(tmp_path / "team.db")
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get("/folio", params={"id": cid}).text
        # Reading-mode body anchors only the prompt with data-event-id.
        m = re.search(r'data-event-id="([^"]+)"', body)
        assert m, "folio body should carry a prompt data-event-id to tag"
        event_id = m.group(1)

        r = client.post("/tag", data={
            "action": "apply", "id": event_id, "entity_type": "prompt", "tag": "flagme",
        })
        assert r.status_code == 201
        assert "flagme" in r.text
        assert "tag-section--elem" in r.text
        assert 'name="entity_type" value="prompt"' in r.text

        # Chip is visible in a fresh folio render.
        assert "flagme" in client.get("/folio", params={"id": cid}).text

        r2 = client.post("/tag", data={
            "action": "remove", "id": event_id, "entity_type": "prompt", "tag": "flagme",
        })
        assert r2.status_code == 201
        assert "flagme" not in r2.text

    # Audited under the resolved kind (prompt), keyed on the resolved ULID.
    conn = open_database(db)
    try:
        row = conn.execute(
            "SELECT target_type, target FROM audit_log WHERE action = 'tag.apply'"
            " ORDER BY occurred_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["target_type"] == "prompt"
    assert row["target"] == event_id


def test_exchange_chip_on_prompt_section_removes_as_exchange(tmp_path):
    """Regression: a prompt section unions its 'exchange'-kind tags into its chips.
    The rendered remove button must carry entity_type=exchange so the round-trip
    actually deletes the exchange assignment — before the (name, kind) chips, the
    remove posted entity_type=prompt and the exchange tag silently survived."""
    import re

    db, cid = _make_db(tmp_path / "team.db")
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get("/folio", params={"id": cid}).text
        event_id = re.search(r'data-event-id="([^"]+)"', body).group(1)

        # Tag the exchange (anchors on the prompt event id).
        r = client.post("/tag", data={
            "action": "apply", "id": event_id, "entity_type": "exchange", "tag": "exch",
        })
        assert r.status_code == 201
        assert "exch" in r.text
        # The chip's remove wire carries the EXCHANGE kind, not the prompt hint
        # (hx-vals JSON is HTML-escaped in the rendered fragment).
        assert "entity_type&quot;: &quot;exchange" in r.text

        # Remove as exchange — the fragment now reflects an empty chip set.
        r2 = client.post("/tag", data={
            "action": "remove", "id": event_id, "entity_type": "exchange", "tag": "exch",
        })
        assert r2.status_code == 201
        assert "exch<" not in r2.text  # no lingering chip
        assert "exch" not in client.get("/folio", params={"id": cid}).text


def test_element_tag_post_is_owner_scoped_404_not_403(tmp_path):
    """An owner-scoped caller tagging another tenant's element resolves to nothing
    (the resolver scopes events through the owning conversation) → 404, never a
    403 that would confirm the target exists."""
    conn = create_database(tmp_path / "owned.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    ws = get_or_create_workspace(conn, "/bob-proj", "2026-01-01T00:00:00Z")
    bob_cid = insert_conversation(
        conn, external_id="cB", harness_id=h, workspace_id=ws,
        started_at="2026-01-15T10:00:00Z",
    )
    bob_pid = insert_prompt(conn, bob_cid, "cBp", "2026-01-15T10:00:00Z")
    insert_prompt_content(conn, bob_pid, 0, "text", '{"text": "bobs prompt"}')
    conn.execute(
        "INSERT INTO conversation_owners VALUES (?,?,?,?)",
        (bob_cid, "bob", None, "2026-01-15T10:00:00Z"),
    )
    conn.commit()
    conn.close()

    auth = {"static_token": "s3cret", "identity": "alice"}
    with _hx_client(create_app(db_path=tmp_path / "owned.db", auth_config=auth)) as client:
        r = client.post(
            "/tag",
            data={"action": "apply", "id": bob_pid, "entity_type": "prompt", "tag": "x"},
            headers={"Authorization": "Bearer s3cret"},
        )
    assert r.status_code == 404


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
    assert 'class="leaf__head"' in body           # daybook leaf per day over real rows
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


def test_search_nav_is_js_driven_not_stub(ctx):
    """The Search rail item is JS-driven (data-nav-search) so re-clicking it
    resumes the last query (last-selected); enhance.js mounts /find. It must not
    point at the /view/search stub."""
    client, _cid = ctx
    body = client.get("/").text
    assert 'data-view="search"' in body and "data-nav-search" in body
    assert 'hx-get="/view/search"' not in body


def test_stats_and_workspaces_nav_are_resumable(ctx):
    """Stats + Workspaces rail items stay htmx-declarative (so htmx keeps the
    history snapshots) but carry data-resume + data-mount-base, the hooks
    enhance.js rewrites at settle so re-clicking resumes the last brush / sort."""
    client, _cid = ctx
    body = client.get("/").text
    # htmx mount is still present (history snapshots intact) AND the resume hooks.
    assert 'hx-get="/dashboard"' in body and 'data-resume="stats"' in body
    assert 'data-mount-base="/dashboard"' in body
    assert 'hx-get="/view/workspaces"' in body and 'data-resume="workspaces"' in body
    assert 'data-mount-base="/view/workspaces"' in body


def test_find_deep_link_propagates_query(ctx):
    """?q= deep-links through the shell to /find, which seeds both the control
    strip (prefill) and the initial list with the term."""
    client, _cid = ctx
    # The search view is server-rendered inline into #main (so a back-restore
    # reproduces it), so the shell carries the find host's /meta + /query mounts
    # directly rather than a /find?q= mount.
    shell = client.get("/", params={"q": "needle"}).text
    assert 'class="find"' in shell
    assert "/meta?search=needle" in shell and "/query?search=needle" in shell
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


def test_meta_has_two_state_structure(ctx):
    """Slice 2c: the strip carries the builder↔collapsed scaffolding — a force-
    expand checkbox (#sx-expand), a primary-facet group (.find__facets), a
    secondary "more filters" disclosure, and the collapsed-mode expand chevron.
    The collapse itself is CSS-only (asserted in the browser smoke)."""
    client, _cid = ctx
    body = client.get("/meta").text
    assert 'id="sx-expand"' in body
    assert 'class="find__facets"' in body
    assert 'class="find__more"' in body
    assert 'class="find__expand"' in body
    # Primary facets live in the group; secondary ones in the disclosure.
    facets = body.split('class="find__facets"')[1].split("</div>")[0]
    assert 'name="workspace"' in facets and 'name="model"' in facets and 'name="tag"' in facets
    more = body.split('class="find__more"')[1]
    assert 'name="owner"' in more and 'name="since"' in more and 'name="before"' in more


def test_meta_has_view_toggle(ctx):
    """The control strip carries a view toggle (result shape) on every server —
    the chunks/thread/conversations shapes are post-processing, engine-agnostic."""
    client, _cid = ctx
    body = client.get("/meta").text
    assert 'name="view"' in body and 'class="search-toggle"' in body
    # All three shapes are offered; chunks is the default selection.
    for shape in ("chunks", "thread", "conversations"):
        assert f'value="{shape}"' in body
    assert 'value="chunks" selected' in body
    # The view toggle survives a deep-link pre-select.
    assert 'value="thread" selected' in client.get("/meta", params={"view": "thread"}).text


def test_meta_hides_engine_toggle_without_embeddings(ctx):
    """No embeddings → every engine collapses to keyword, so the mode toggle is
    a no-op and is omitted (the header's truthful [fts] label is the only signal
    needed). The default ctx server has no embeddings."""
    client, _cid = ctx
    body = client.get("/meta").text
    assert 'name="mode"' not in body


def test_meta_shows_engine_toggle_with_embeddings(ctx, monkeypatch, tmp_path):
    """With embeddings the engine choice is real, so the mode toggle appears
    (auto/hybrid/semantic/keyword). Only the control strip is exercised — no
    search runs — so faking availability is sufficient."""
    client, _cid = ctx
    existing = tmp_path / "embeds.db"
    existing.write_bytes(b"")
    monkeypatch.setattr("siftd.api.embeddings_available", lambda: True)
    monkeypatch.setattr("siftd.paths.embeddings_db_path", lambda: existing)

    body = client.get("/meta").text
    assert 'name="mode"' in body
    for engine in ("auto", "hybrid", "semantic", "fts"):
        assert f'value="{engine}"' in body
    assert 'value="auto" selected' in body


def test_find_box_punctuation_does_not_500(ctx):
    """The find box is untrusted text fed into FTS5 MATCH. Bare punctuation
    (", :, *, (, AND) must sanitize to a clean list, never an fts5 syntax 500."""
    client, _cid = ctx
    for raw in ('"', "a:b", "*", "foo AND", "(", '")'):
        resp = client.get("/query", params={"search": raw})
        assert resp.status_code == 200, f"{raw!r} -> {resp.status_code}"


def test_query_no_search_no_facet_is_prompt(ctx):
    """Slice 2b: bare Find (no content query, no facet) is the search PROMPT, not
    the old recency list — Find opens as a search surface."""
    client, _cid = ctx
    body = client.get("/query").text
    assert 'class="find-prompt"' in body
    assert 'class="conversation-list"' not in body
    assert "search-results" not in body


def test_query_no_search_with_facet_is_browse_list(ctx):
    """Slice 2b: a facet without a content query still browses — the facet-filtered
    conversation table (the Tags/Workspaces drill-downs land here)."""
    client, _cid = ctx
    body = client.get("/query", params={"workspace": "/proj"}).text
    assert 'class="conversation-list"' in body
    assert 'class="find-prompt"' not in body
    assert "search-results" not in body


def test_query_search_routes_through_engine(tmp_path):
    """A content query in Find runs the real search ENGINE: ranked excerpt hits
    (not the recency-ordered keyword filter), with the resolved engine reported
    in the header (truthfulness) and hits drilling into the folio."""
    from siftd.api import open_database
    from siftd.api.search import rebuild_fts_index

    db, _cid = _make_db(tmp_path / "team.db")
    conn = open_database(db, read_only=False)
    try:
        rebuild_fts_index(conn)
        conn.commit()
    finally:
        conn.close()

    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get("/query", params={"search": "reply"}).text

    assert 'class="search-results chunks"' in body   # ranked-chunk shape
    assert "[fts]" in body                            # no embeddings here → fts engine, named
    assert 'class="search-hit"' in body               # an excerpt hit, not a table row
    assert 'hx-get="/folio?id=' in body               # hit drills into the folio
    assert 'class="conversation-list"' not in body    # not the recency list table


def test_query_search_no_match_shows_empty_not_500(tmp_path):
    """A content query with no FTS hits renders an empty search result (named
    engine + 'No matches'), never the recency list and never a 500."""
    from siftd.api import open_database
    from siftd.api.search import rebuild_fts_index

    db, _cid = _make_db(tmp_path / "team.db")
    conn = open_database(db, read_only=False)
    try:
        rebuild_fts_index(conn)
        conn.commit()
    finally:
        conn.close()

    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        resp = client.get("/query", params={"search": "zzzznomatch"})
    assert resp.status_code == 200
    body = resp.text
    assert 'class="search-results chunks"' in body
    assert "[fts]" in body
    assert "No matches" in body


def _searchable_db(tmp_path: Path) -> Path:
    """A _make_db DB with the FTS index built, so the engine returns hits."""
    from siftd.api import open_database
    from siftd.api.search import rebuild_fts_index

    db, _cid = _make_db(tmp_path / "team.db")
    conn = open_database(db, read_only=False)
    try:
        rebuild_fts_index(conn)
        conn.commit()
    finally:
        conn.close()
    return db


def test_query_search_thread_view(tmp_path):
    """view=thread runs the same engine + the thread recipe server-side, so the
    browser inherits the tier1/tier2 thread shape — not just chunks. The view
    toggle's value rides #filters into /query."""
    db = _searchable_db(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get("/query", params={"search": "reply", "view": "thread"}).text

    assert 'class="search-results thread"' in body   # thread shape, not chunks
    assert 'class="search-results chunks"' not in body
    assert "[fts]" in body                            # engine still named truthfully
    assert 'class="conversation-list"' not in body    # not the recency browse table


def test_query_search_conversations_view(tmp_path):
    """view=conversations aggregates engine chunks per conversation (Max/Mean/
    Chunks) — distinct from the no-query recency browse, which is also a
    .conversation-list but carries no search-results section or engine tag."""
    db = _searchable_db(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get(
            "/query", params={"search": "reply", "view": "conversations"}
        ).text

    assert 'class="search-results conversations"' in body
    assert 'class="search-results chunks"' not in body
    assert "[fts]" in body
    assert "Conversations for:" in body               # the aggregate heading


def test_query_view_defaults_to_chunks(tmp_path):
    """An engine query with no view param keeps the chunks shape (the default),
    so the toggle is purely additive — existing deep links are unaffected."""
    db = _searchable_db(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get("/query", params={"search": "reply"}).text
    assert 'class="search-results chunks"' in body


def test_query_bad_view_falls_back_to_chunks_results(tmp_path):
    """A hand-crafted out-of-vocab ?view= must not mask real hits. ui_query
    clamps view to the canonical vocabulary (mirroring the control strip), so a
    bogus view returns the same chunks results as no view param — never a
    misleading empty '[fts] No matches' pane. Regression guard for the
    strip-vs-results clamp asymmetry the review caught."""
    db = _searchable_db(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        bogus = client.get("/query", params={"search": "reply", "view": "bogus"}).text
    assert 'class="search-results chunks"' in bogus   # clamped to the default shape
    assert 'class="search-hit"' in bogus              # real hits, not an empty pane
    assert "No matches" not in bogus                  # the masking bug, were it unfixed


def test_query_empty_thread_and_conversations_show_no_matches(tmp_path):
    """A zero-hit content query in thread/conversations view renders an explicit
    'No matches' affordance, not a headed-but-bodyless pane — parity with the
    chunks empty state, now that the toggle exposes these paths to the browser."""
    db = _searchable_db(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        thread = client.get(
            "/query", params={"search": "zzzznomatch", "view": "thread"}
        ).text
        convs = client.get(
            "/query", params={"search": "zzzznomatch", "view": "conversations"}
        ).text
    assert 'class="search-results thread"' in thread and "No matches" in thread
    assert 'class="search-results conversations"' in convs and "No matches" in convs


def _searchable_db_cid(tmp_path: Path) -> tuple[Path, str]:
    """A searchable DB plus the conversation id, for the context-unfold tests."""
    from siftd.api import open_database
    from siftd.api.search import rebuild_fts_index

    db, cid = _make_db(tmp_path / "team.db")
    conn = open_database(db, read_only=False)
    try:
        rebuild_fts_index(conn)
        conn.commit()
    finally:
        conn.close()
    return db, cid


def test_chunks_hit_carries_unfold_trigger(tmp_path):
    """Each chunks hit splits into a __main block (the folio jump) + a
    .hit-context unfold control anchored on the matched turn — the in-place
    context view. The control fetches the first ring (w=2)."""
    db, _cid = _searchable_db_cid(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        body = client.get("/query", params={"search": "reply", "view": "chunks"}).text
    assert 'class="search-hit__main"' in body          # the navigable block
    assert 'hx-get="/folio?id=' in body                # folio jump on __main
    assert 'class="hit-context"' in body
    assert 'hx-get="/find/context?id=' in body and "w=2" in body  # unfold trigger


def test_find_context_unfolds_window_with_anchor(tmp_path):
    """The context endpoint runs the anchored windowed read and renders the
    surrounding exchanges with the matched turn flagged + stepped controls."""
    db, cid = _searchable_db_cid(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        sl = client.get("/find/context", params={"id": cid, "at": 0, "w": 2}).text
    assert 'class="hit-context__slice"' in sl          # the inline context region
    assert 'class="turn' in sl                         # rendered exchanges
    assert "is-anchor" in sl                            # the matched turn is flagged
    assert "more context" in sl and "w=5" in sl        # stepped: widen to next ring
    assert "collapse" in sl and "w=0" in sl


def test_find_context_last_ring_defers_to_folio(tmp_path):
    """At the final ring the progression terminates in the deliberate folio jump
    (its own #main swap), not another widen."""
    db, cid = _searchable_db_cid(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        sl = client.get("/find/context", params={"id": cid, "at": 0, "w": 10}).text
    assert "open in folio" in sl and 'hx-target="#main"' in sl
    assert "more context" not in sl


def test_find_context_collapse_and_clamp_return_trigger(tmp_path):
    """w=0 (collapse), an out-of-range w (clamped), and a missing id all return
    the harmless collapsed trigger — never a windowed slice, never a 500."""
    db, cid = _searchable_db_cid(tmp_path)
    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        collapse = client.get("/find/context", params={"id": cid, "at": 0, "w": 0})
        clamp = client.get("/find/context", params={"id": cid, "at": 0, "w": 999})
        noid = client.get("/find/context", params={"at": 0, "w": 2})
    for resp in (collapse, clamp, noid):
        assert resp.status_code == 200
        assert "unfold context" in resp.text and 'hit-context__slice' not in resp.text


def test_find_context_is_owner_scoped(tmp_path):
    """A hit's context can't leak a conversation the requester doesn't own.
    get_conversation resolves the id under the effective owner, so bob's
    conversation id — fetched while authed as alice — yields the collapsed
    trigger (no window), never bob's exchange text."""
    conn = create_database(tmp_path / "owned.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    m = get_or_create_model(conn, "claude-x")
    p = get_or_create_provider(conn, "anthropic")
    ws = get_or_create_workspace(conn, "/bob-proj", "2026-01-01T00:00:00Z")
    bob_cid = insert_conversation(
        conn, external_id="cB", harness_id=h, workspace_id=ws,
        started_at="2026-01-15T10:00:00Z",
    )
    pid = insert_prompt(conn, bob_cid, "cBp", "2026-01-15T10:00:00Z")
    insert_prompt_content(conn, pid, 0, "text", '{"text": "bobs secret prompt"}')
    rid = insert_response(
        conn, bob_cid, pid, m, p, "cBr", "2026-01-15T10:00:00Z",
        input_tokens=10, output_tokens=5,
    )
    insert_response_content(conn, rid, 0, "text", '{"text": "bobs secret reply"}')
    conn.execute(
        "INSERT INTO conversation_owners VALUES (?,?,?,?)",
        (bob_cid, "bob", None, "2026-01-15T10:00:00Z"),
    )
    conn.commit()
    conn.close()

    auth = {"static_token": "s3cret", "identity": "alice"}
    with _hx_client(create_app(db_path=tmp_path / "owned.db", auth_config=auth)) as client:
        sl = client.get(
            "/find/context", params={"id": bob_cid, "at": 0, "w": 2},
            headers={"Authorization": "Bearer s3cret"},
        ).text
    assert "bobs secret" not in sl              # no cross-tenant content leak
    assert 'hit-context__slice' not in sl       # no window rendered
    assert "unfold context" in sl               # just the harmless trigger


def test_find_context_flags_correct_anchor_with_offset(tmp_path):
    """anchor_pos = min(at, w): with turns BEFORE the anchor inside the window,
    the is-anchor flag must land on the matched exchange (the at-th), not the
    window's first turn. Guards the offset arithmetic against a real >w anchor."""
    import re

    conn = create_database(tmp_path / "long.db")
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    ws = get_or_create_workspace(conn, "/proj", "2026-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-x")
    p = get_or_create_provider(conn, "anthropic")
    cid = insert_conversation(
        conn, external_id="long", harness_id=h, workspace_id=ws,
        started_at="2026-01-15T10:00:00Z",
    )
    for i in range(6):  # six exchanges so at=3,w=2 has turns on both sides
        ts = f"2026-01-15T10:0{i}:00Z"
        pid = insert_prompt(conn, cid, f"p{i}", ts)
        insert_prompt_content(conn, pid, 0, "text", f'{{"text": "anchormark{i}"}}')
        rid = insert_response(conn, cid, pid, m, p, f"r{i}", ts, input_tokens=10, output_tokens=5)
        insert_response_content(conn, rid, 0, "text", f'{{"text": "resp{i}"}}')
    conn.commit()
    conn.close()

    with _hx_client(create_app(db_path=tmp_path / "long.db", auth_config=None)) as client:
        # at=3, w=2 → window = exchanges 1..5; anchor at offset min(3,2)=2 (3rd shown).
        sl = client.get("/find/context", params={"id": cid, "at": 3, "w": 2}).text

    assert "anchormark3" in sl                    # the anchor turn is in the window
    # The first is-anchor block must be the at=3 prompt, not the window start (mark1).
    flagged = re.search(r"is-anchor.*?anchormark(\d)", sl, re.DOTALL)
    assert flagged and flagged.group(1) == "3", f"anchor flagged the wrong turn: {flagged}"


def test_deep_link_id_mounts_folio(ctx):
    client, cid = ctx
    body = client.get("/", params={"id": cid}).text
    assert f'hx-get="/folio?id={cid}"' in body
    assert 'data-view="transcript"' in body and 'aria-current="page"' in body


# --- Stats dashboard (Phase B slice 2) -------------------------------------


def test_dashboard_route_renders_swiss_dashboard(ctx):
    client, _cid = ctx
    body = client.get("/dashboard").text
    assert 'class="reck' in body  # the reckoning (Stats) article
    assert 'data-view="stats"' in body
    assert "Model mix" in body and "Workspace mix" in body
    # the activity/rhythm charts + the measure toggle ride the reckoning
    assert 'id="trend-plot"' in body
    assert 'id="hod-plot"' in body and 'id="dow-plot"' in body
    assert 'name="measure"' in body


def test_dashboard_model_brushing_scopes_and_validates(tmp_path):
    """?model= names a real model → the Model-mix row is marked is-current and a
    'show all' reset appears (the activity charts scope to it). An unknown model
    falls back to the unscoped view, never an empty-but-scoped chart."""
    db, _cid = _make_db(tmp_path / "brush.db")
    # the route reads by_model off the rollup; build it as a real ingest would.
    from siftd.storage.sqlite import open_database

    conn = open_database(db)
    rebuild_rollups(conn)
    conn.commit()
    conn.close()

    with _hx_client(create_app(db_path=db, auth_config=None)) as client:
        scoped = client.get("/dashboard", params={"model": "claude-opus"}).text
        assert "Activity &middot; claude-opus" in scoped
        assert 'class="reck__clear"' in scoped
        assert 'class="ledger__row is-current"' in scoped

        unknown = client.get("/dashboard", params={"model": "ghost-model"}).text
        assert 'class="reck__clear"' not in unknown
        assert "Activity over the period" in unknown


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
    with _hx_client(create_app(db_path=db, auth_config=auth)) as client:
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
    with _hx_client(create_app(db_path=db, auth_config=auth)) as client:
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
    with _hx_client(create_app(db_path=db, auth_config=auth_bob)) as client:
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
    with _hx_client(create_app(db_path=db, auth_config=None)) as c:
        yield c


def test_tags_view_is_live_not_stub(tags_ctx):
    body = tags_ctx.get("/view/tags").text
    assert 'class="stub"' not in body
    assert 'data-view="tags"' in body and 'data-title="Tags"' in body
    assert 'data-count="3"' in body
    assert 'class="index"' in body and 'class="idx-entries"' in body
    # subject namespace tree: a research band groups its two leaves; bug is ungrouped
    assert '>research<span class="idx-head__count"' in body
    assert ">ungrouped<" in body
    # the hand-applied subject index book
    assert "Subject index" in body


def test_tags_rows_drill_into_find(tags_ctx):
    body = tags_ctx.get("/view/tags").text
    assert 'hx-get="/find?tag=research%3Aauth"' in body
    assert 'hx-target="#main"' in body
    assert 'hx-push-url="/?tag=research%3Aauth"' in body
    # an unpinned tag offers the pin affordance
    assert 'class="pin"' in body


def test_tag_deep_link_propagates_through_shell_and_find(tags_ctx):
    shell = tags_ctx.get("/", params={"tag": "research:auth"}).text
    # ?tag= → search view, server-rendered inline (host carries the /query mount).
    assert 'class="find"' in shell and "/query?tag=research%3Aauth" in shell
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

    with _hx_client(create_app(db_path=db, auth_config=alice_auth)) as alice:
        assert "zone--pinned" not in alice.get("/view/tags", headers=hdrs).text
        r = alice.post("/tag/pin", headers=hdrs, data={"action": "pin", "tag": "shared"})
        assert r.status_code == 201  # Litestar POST default; htmx swaps any 2xx
        assert "zone--pinned" in r.text and "pin--on" in r.text  # re-rendered view
        assert "zone--pinned" in alice.get("/view/tags", headers=hdrs).text  # persists

    with _hx_client(create_app(db_path=db, auth_config=bob_auth)) as bob:
        body = bob.get("/view/tags", headers=hdrs).text
        assert "shared" in body              # bob sees the shared tag
        assert "zone--pinned" not in body    # but not alice's pin

    with _hx_client(create_app(db_path=db, auth_config=alice_auth)) as alice:
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

    with _hx_client(create_app(db_path=db, auth_config=alice_auth)) as alice:
        assert "zone--pinned" not in alice.get("/view/workspaces", headers=hdrs).text
        r = alice.post(
            "/workspace/pin", headers=hdrs,
            data={"action": "pin", "ws": wid, "sort": "sessions"},
        )
        assert r.status_code == 201  # Litestar POST default; htmx swaps any 2xx
        assert "zone--pinned" in r.text and "pin--on" in r.text  # re-rendered view
        assert "zone--pinned" in alice.get("/view/workspaces", headers=hdrs).text  # persists

    with _hx_client(create_app(db_path=db, auth_config=bob_auth)) as bob:
        body = bob.get("/view/workspaces", headers=hdrs).text
        assert "proj" in body              # bob sees the shared workspace
        assert "zone--pinned" not in body  # but not alice's pin

    with _hx_client(create_app(db_path=db, auth_config=alice_auth)) as alice:
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
    with _hx_client(create_app(db_path=db, auth_config=auth)) as c:
        ranked = c.get("/view/workspaces?sort=tokens", headers=hdrs).text
        assert "ledger--ws is-ranked" in ranked
        assert 'class="ws-sort__opt is-active"' in ranked
        recent = c.get("/view/workspaces?sort=recent", headers=hdrs).text
        assert "is-ranked" not in recent


def test_folio_trace_anchors_events(ctx):
    # Trace mode anchors each response/prompt by its event ULID — the substrate
    # the search → folio jump targets (event_id is already threaded through
    # walk_narrative; this proves the route renders it as an anchor).
    client, cid = ctx
    body = client.get("/folio", params={"id": cid, "mode": "trace"}).text
    assert "data-event-id=" in body


def test_folio_event_jump_scrolls_and_marks(ctx):
    # A search hit jumps with ?event=<ULID>; the route marks that element
    # is-target and emits data-scroll-to so enhance.js lands ON the match, not
    # the folio top. Pull a real anchor the trace emitted and feed it back.
    import re

    client, cid = ctx
    trace = client.get("/folio", params={"id": cid, "mode": "trace"}).text
    m = re.search(r'data-event-id="([^"]+)"', trace)
    assert m, "trace should anchor at least one event"
    ev = m.group(1)
    body = client.get("/folio", params={"id": cid, "mode": "trace", "event": ev}).text
    assert f'data-scroll-to="{ev}"' in body
    assert "is-target" in body


def test_folio_rejects_unsafe_event(ctx):
    # A hostile/selector-breaking ?event= is validated away: no data-scroll-to, no
    # reflection, no crash (the value never reaches the attribute or the client
    # querySelector).
    client, cid = ctx
    r = client.get(
        "/folio", params={"id": cid, "mode": "trace", "event": '"]<script>'}
    )
    assert r.status_code == 200
    assert "data-scroll-to" not in r.text
    assert "<script>" not in r.text


def test_find_context_threads_event_through_rings(ctx):
    # The matched event rides the unfold's ring URLs so the last ring's "open in
    # folio" jump stays event-precise across in-place steps.
    client, cid = ctx
    r = client.get(
        "/find/context", params={"id": cid, "at": 0, "w": 2, "event": "01EVENTID"}
    )
    assert r.status_code == 200
    assert "event=01EVENTID" in r.text


def test_shell_deep_link_carries_trace_jump(ctx):
    # A hard reload of a search → folio jump URL must re-mount the folio in trace
    # mode at the matched event: the shell carries mode+event onto the inner
    # /folio mount (the htmx push-url already put them in the address bar).
    client, cid = ctx
    body = client.get("/", params={"id": cid, "mode": "trace", "event": "01EVENTID"}).text
    assert "/folio?id=" in body
    assert "mode=trace" in body
    assert "event=01EVENTID" in body


def test_shell_drops_unsafe_event_from_mount(ctx):
    # A hostile ?event= is validated away before it reaches the mount URL — never
    # reflected into the page.
    client, cid = ctx
    body = client.get("/", params={"id": cid, "mode": "trace", "event": '"><x'}).text
    assert "mode=trace" in body      # mode still rides
    assert "event=" not in body      # the bad event does not
    assert '"><x' not in body


def test_folio_reading_mode_ignores_event(ctx):
    # The event target is trace-only: a reading-mode folio with ?event= must not
    # emit a data-scroll-to hint (no element to scroll to in reading).
    import re

    client, cid = ctx
    trace = client.get("/folio", params={"id": cid, "mode": "trace"}).text
    ev = re.search(r'data-event-id="([^"]+)"', trace).group(1)
    body = client.get("/folio", params={"id": cid, "mode": "reading", "event": ev}).text
    assert "data-scroll-to" not in body
    assert "is-target" not in body


def test_shell_reading_deep_link_drops_event(ctx):
    # event is nested under mode=trace in the mount, so a reading deep-link never
    # carries an unrenderable target.
    client, cid = ctx
    body = client.get(
        "/", params={"id": cid, "mode": "reading", "event": "01EVENTID"}
    ).text
    assert "/folio?id=" in body
    assert "event=" not in body


# ---------------------------------------------------------------------------
# URL-as-state foundation (Slice 1): canonical /?view= grammar, fragment
# redirect, canonical nav push, and deep-linkable brush/sort.
# ---------------------------------------------------------------------------


def _browser(db):
    """A TestClient that does NOT send HX-Request — i.e. a real browser
    navigation/refresh, the case the fragment redirect guards target."""
    return TestClient(app=create_app(db_path=db, auth_config=None))


def test_fragment_direct_get_redirects_to_canonical(tmp_path):
    # A direct (non-htmx) GET of a fragment 303s to its canonical shell URL, so a
    # typed/refreshed/shared fragment URL never renders a chrome-less fragment.
    db, cid = _make_db(tmp_path / "r.db")
    with _browser(db) as b:
        r = b.get("/folio", params={"id": cid}, follow_redirects=False)
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/?view=transcript") and f"id={cid}" in loc

        r2 = b.get("/dashboard", params={"model": "claude-opus"}, follow_redirects=False)
        assert r2.status_code == 303
        assert r2.headers["location"] == "/?view=stats&model=claude-opus"

        r3 = b.get("/view/tags", follow_redirects=False)
        assert r3.status_code == 303
        assert r3.headers["location"] == "/?view=tags"


def test_htmx_get_of_fragment_serves_fragment_not_redirect(tmp_path):
    # The same endpoint, fetched as htmx (how the shell mounts it), returns the
    # fragment — the redirect is browser-only.
    db, cid = _make_db(tmp_path / "h.db")
    with _hx_client(create_app(db_path=db, auth_config=None)) as c:
        r = c.get("/view/tags", follow_redirects=False)
        assert r.status_code == 200
        assert 'data-view="tags"' in r.text


def test_nav_pushes_canonical_view_urls(ctx):
    # The stateless rail items push their canonical /?view=<vid> shell URL
    # declaratively via htmx. The three resumable views (search/stats/workspaces)
    # are JS-driven — they push their (possibly stateful) canonical URL in
    # enhance.js to resume last-selected, not via hx-push-url.
    client, _cid = ctx
    body = client.get("/").text
    # Search is JS-driven (resumes the last query) — it pushes its canonical URL
    # in enhance.js, not via hx-push-url. The other five push declaratively
    # (stats/workspaces have their push rewritten at settle to resume state).
    for vid in ("sessions", "transcript", "tags", "workspaces", "stats"):
        assert f'hx-push-url="/?view={vid}"' in body
    assert "data-nav-search" in body


def test_view_param_mounts_correct_fragment(ctx):
    # The explicit ?view= grammar decodes to the right mount target.
    client, _cid = ctx
    assert 'hx-get="/dashboard"' in client.get("/", params={"view": "stats"}).text
    assert 'hx-get="/view/tags"' in client.get("/", params={"view": "tags"}).text
    assert 'hx-get="/view/sessions"' in client.get("/", params={"view": "sessions"}).text
    # search is server-rendered inline (host), not a /find #main mount.
    assert 'class="find"' in client.get("/", params={"view": "search"}).text
    # Facets ride the mount: stats model, workspaces sort.
    assert 'hx-get="/dashboard?model=claude-opus"' in client.get(
        "/", params={"view": "stats", "model": "claude-opus"}
    ).text
    assert 'hx-get="/view/workspaces?sort=cost"' in client.get(
        "/", params={"view": "workspaces", "sort": "cost"}
    ).text


def test_legacy_presence_params_still_resolve(ctx):
    # Back-compat: the old presence-based deep links keep working without ?view=.
    client, cid = ctx
    assert "/folio?id=" in client.get("/", params={"id": cid}).text
    # ?q= → search view (inline host carrying the control-name mount).
    assert "/query?search=needle" in client.get("/", params={"q": "needle"}).text


def test_ws_sort_links_push_canonical_shell_url(ctx):
    # The workspace sort ordering is deep-linkable: each sort pushes /?view=workspaces&sort=.
    client, _cid = ctx
    body = client.get("/view/workspaces").text
    assert 'hx-push-url="/?view=workspaces&amp;sort=cost"' in body
    assert 'hx-push-url="/?view=workspaces&amp;sort=tokens"' in body


def test_brush_pushes_canonical_shell_url(tmp_path):
    # A model-mix brush is deep-linkable: clicking a model pushes /?view=stats&model=.
    db, _cid = _make_db(tmp_path / "brush.db")
    import sqlite3

    conn = sqlite3.connect(db)
    rebuild_rollups(conn)
    conn.commit()
    conn.close()
    with _hx_client(create_app(db_path=db, auth_config=None)) as c:
        body = c.get("/dashboard").text
        assert 'hx-push-url="/?view=stats&amp;model=claude-opus"' in body


# ---------------------------------------------------------------------------
# Slice 3a — search state in the URL (retain / refresh / share the full query).
# ---------------------------------------------------------------------------


def test_shell_decodes_full_search_facets(ctx):
    """The shell decodes ?view=search + canonical facets into an inline find host
    whose /meta + /query mounts carry the whole query (mapped to control names),
    so a refresh / shared link / back-restore reproduces it."""
    client, _cid = ctx
    body = client.get("/", params={
        "view": "search", "q": "needle", "shape": "thread",
        "engine": "hybrid", "workspace": "/proj",
    }).text
    assert 'class="find"' in body  # server-rendered inline (not an empty mount)
    assert "/query?" in body
    assert "search=needle" in body and "view=thread" in body
    assert "mode=hybrid" in body and "workspace=" in body


def test_find_threads_facets_to_meta_and_query(ctx):
    """ui_find maps the canonical facets onto the control names (shape→view,
    engine→mode, q→search) and threads them to BOTH /meta and /query mounts."""
    client, _cid = ctx
    body = client.get("/find", params={
        "q": "needle", "shape": "thread", "engine": "hybrid",
        "workspace": "/proj", "model": "claude-opus",
    }).text
    meta = body.split('id="filters"')[1].split("</div>")[0]
    lst = body.split('id="list"')[1].split("</div>")[0]
    for frag in (meta, lst):
        assert "search=needle" in frag
        assert "view=thread" in frag and "mode=hybrid" in frag
        assert "workspace=" in frag and "model=claude-opus" in frag


def test_meta_prefills_all_facets(ctx):
    """A deep-linked /meta reflects every facet (not just search/tag/view/mode),
    so the rebuilt strip mirrors the URL — the retain/refresh contract."""
    client, _cid = ctx
    body = client.get("/meta", params={
        "workspace": "/proj", "model": "claude-opus", "owner": "alice",
        "since": "2026-01-01", "view": "thread",
    }).text
    # workspace + model selects carry the selected option; owner + since prefill.
    ws = body.split('name="workspace"')[1].split("</select>")[0]
    assert "selected" in ws
    md = body.split('name="model"')[1].split("</select>")[0]
    assert 'value="claude-opus" selected' in md
    assert 'name="owner"' in body and 'value="alice"' in body
    assert 'name="since"' in body and 'value="2026-01-01"' in body
