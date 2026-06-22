"""Route test for GET /api/v1/workspaces/{id} (workspace-detail Operation).

Mirrors /api/v1/conversations/{id}: addressed by the workspace ULID, 404 on
unknown id, owner-scoped via the effective owner.
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.response import Response

from siftd.serve import routes
from test_workspace_detail import _build


def _run(result):
    # Most routes are sync now (threadpool via sync_to_thread); body-reading
    # handlers (tag_write/session_queue/push) stay async and return coroutines.
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_workspace_detail_route_returns_detail(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)

    out = _run(routes.workspace_detail_route.fn(SimpleNamespace(), db, id=ws_a))
    assert not isinstance(out, Response)
    assert out["id"] == ws_a
    assert out["path"] == "/test/projA"
    assert out["sessions"] == 2
    assert out["input_tokens"] == 1100
    assert any(m["name"] == "claude-3-opus" for m in out["model_mix"])
    assert len(out["recent"]) == 2


def test_workspace_detail_route_404_on_unknown(tmp_path):
    db = tmp_path / "d.db"
    _build(db)

    out = _run(routes.workspace_detail_route.fn(SimpleNamespace(), db, id="01HNOPE"))
    assert isinstance(out, Response)
    assert out.status_code == 404
    # 404 now comes from the OpSpec not_found_on_none flag via _dispatch, with
    # the entity-specific message derived from the path.
    assert out.content["error"] == "workspace not found"


def test_workspace_detail_route_payload_shape_unchanged(tmp_path):
    """Routing through _dispatch preserves the pre-existing payload shape."""
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)

    out = _run(routes.workspace_detail_route.fn(SimpleNamespace(), db, id=ws_a))
    assert not isinstance(out, Response)
    assert set(out) >= {"id", "git_remote", "model_mix", "recent"}
    assert isinstance(out["model_mix"], list)
    assert isinstance(out["recent"], list)
