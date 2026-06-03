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


def test_search_stub_echoes_query(ctx):
    client, _cid = ctx
    body = client.get("/view/search", params={"q": "needle"}).text
    assert 'data-view="search"' in body
    assert "needle" in body


def test_deep_link_id_mounts_folio(ctx):
    client, cid = ctx
    body = client.get("/", params={"id": cid}).text
    assert f'hx-get="/folio?id={cid}"' in body
    assert 'data-view="transcript"' in body and 'aria-current="page"' in body
