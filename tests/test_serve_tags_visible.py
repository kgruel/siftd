"""Route test: GET /api/v1/tags?visible=activity enriches with the activity spark.

Verifies the general ?visible= enrichment-request mechanism end-to-end through
the serve route: the tag returns an activity series only when 'activity' is in
?visible=, and the base payload (no ?visible=) carries activity=None.
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.serve import routes
from siftd.storage.sqlite import create_database, get_or_create_harness, insert_conversation
from siftd.storage.tags import apply_tag, get_or_create_tag


def _run(result):
    # Most routes are sync now (threadpool via sync_to_thread); body-reading
    # handlers (tag_write/session_queue/push) stay async and return coroutines.
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _seed(db_path):
    conn = create_database(db_path)
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    tag_id = get_or_create_tag(conn, "topic:foo")
    cid = insert_conversation(
        conn, external_id="c1", harness_id=harness, workspace_id=None,
        started_at="2026-06-01T00:00:00Z",
    )
    apply_tag(conn, "conversation", cid, tag_id)
    conn.commit()
    conn.close()


def test_tags_route_enriches_with_activity_when_visible(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)

    out = _run(routes.tags_route.fn(SimpleNamespace(), db, since=None, before=None, visible="activity"))
    foo = next(t for t in out["tags"] if t["name"] == "topic:foo")
    assert foo["activity"] is not None
    assert len(foo["activity"]) == 12
    assert foo["activity"][11] == 1


def test_tags_route_omits_activity_without_visible(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)

    out = _run(routes.tags_route.fn(SimpleNamespace(), db, since=None, before=None, visible=None))
    foo = next(t for t in out["tags"] if t["name"] == "topic:foo")
    assert foo["activity"] is None
