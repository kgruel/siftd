"""Tests for wire-format expansion + parity between CLI op params and serve routes.

Covers two parity properties that previously caused silent drift on delegated reads:
1. Fidelity objects in op.params must expand to include_thinking/include_tool_content
   wire fields, not be stringified opaquely by urlencode.
2. /api/v1/conversations/{id} must accept anchor + window query params and pass
   them through to get_conversation (so CLI `query <id> --json --at-turn N` against
   a remote serve actually anchors).

Exercises :meth:`Operation.to_local` / :meth:`Operation.to_wire` /
:meth:`Operation.to_wire_body` and the per-op :class:`OpSpec` registry in
:mod:`siftd.api.op_spec`. See ``docs/guides/delegation-contract.md`` for the
pattern.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve


def _run(coro):
    return asyncio.run(coro)


# --- Operation.to_local / to_wire / to_wire_body ---


def test_to_local_strips_annotation_keys():
    """to_local() drops OpSpec.local_excludes (CLI annotations the fn doesn't take)."""
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation

    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=lambda **kwargs: kwargs,  # echo fn for the test
        params={
            "q": "hello",
            "n": 10,
            "around": "delta",          # excluded — CLI annotation
            "debug_ids": True,           # excluded — render-context annotation
            "embeddings_only": False,    # excluded — routing key
            "action": "search",          # excluded — routing key
            "db_path": Path("/tmp/x"),   # NOT excluded — fn takes db_path
        },
        render_method="search",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )
    out = op.to_local()
    assert "around" not in out
    assert "debug_ids" not in out
    assert "embeddings_only" not in out
    assert "action" not in out
    assert out["q"] == "hello"
    assert out["n"] == 10
    assert out["db_path"] == Path("/tmp/x")


def test_to_local_strips_action_routing_key_for_tag_post():
    """Tag-POST ops carry ``action`` as a server routing key the local fns don't accept.

    The local fallback path in cli/tags.py today calls the typed fn directly
    (bypassing ``execute(op)``), so this is defense-in-depth: if anyone
    refactors that fallback to use ``execute(op)``, ``to_local`` must strip
    ``action`` so the fn doesn't TypeError on unexpected kwarg. Preserves
    the pre-ST-5 _LOCAL_FN_EXCLUDE semantics for this op.
    """
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation

    op = Operation(
        path="/api/v1/tag",
        method="POST",
        fn=lambda **kwargs: kwargs,
        params={
            "action": "rename",
            "old_name": "foo",
            "new_name": "bar",
            "db_path": Path("/tmp/x"),
        },
        render_method="raw",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )
    local = op.to_local()
    assert "action" not in local, "action is a server routing key, not a local fn kwarg"
    assert local["old_name"] == "foo"
    assert local["new_name"] == "bar"
    # db_path remains — the local fns accept it.
    assert local["db_path"] == Path("/tmp/x")
    # action must STILL travel on the wire (server reads it to dispatch).
    body = op.to_wire_body()
    assert body["action"] == "rename", "server needs action in the POST body"


def test_to_wire_strips_local_paths_and_translates_fidelity():
    """to_wire() drops db_path, expands Fidelity, drops None, applies key renames."""
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation

    fid = Fidelity(visible=frozenset({"text", "thinking"}))
    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=lambda **kwargs: kwargs,
        params={
            "q": "hi",
            "fidelity": fid,
            "tool_filter": None,            # dropped (None)
            "lambda_": 0.5,                  # renamed → lambda
            "db_path": Path("/tmp/x"),       # dropped (local-only)
            "embed_db": Path("/tmp/e.db"),   # dropped (local-only)
            "around": "phrase",              # dropped (CLI annotation)
        },
        render_method="search",
        fidelity=fid,
        db=Path("/tmp/x"),
    )
    q = op.to_wire()
    assert "db_path" not in q
    assert "embed_db" not in q
    assert "around" not in q
    assert "tool_filter" not in q
    assert "fidelity" not in q
    assert "lambda_" not in q
    assert q["include_thinking"] is True
    assert q["include_tool_content"] is False
    assert q["lambda"] == 0.5
    assert q["q"] == "hi"


def test_to_wire_body_drops_local_paths_but_preserves_None():
    """to_wire_body (POST) drops db_path but preserves None — JSON null is meaningful."""
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation

    op = Operation(
        path="/api/v1/tag",
        method="POST",
        fn=lambda **kwargs: kwargs,
        params={
            "action": "apply",
            "tags": ["foo", "bar"],
            "entity_id": "abc",
            "last": None,                   # None preserved on POST (JSON null)
            "db_path": Path("/tmp/x"),      # dropped
        },
        render_method="tag",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )
    body = op.to_wire_body()
    assert "db_path" not in body
    assert body["action"] == "apply"
    assert body["tags"] == ["foo", "bar"]
    assert body["entity_id"] == "abc"
    # Note: None is preserved for POST bodies; JSON null is a legitimate value.
    # The route handler distinguishes "field omitted" (KeyError) from "field=null".
    assert "last" in body and body["last"] is None


def test_try_serve_propagates_missing_op_spec_not_swallow():
    """``try_serve`` must let :class:`MissingOpSpec` propagate, not swallow it.

    The broad ``except Exception`` clause in ``try_serve`` is for transport-
    level surprises (timeouts, malformed responses) where local fallback is
    the right behavior. ``MissingOpSpec`` is a substrate-level bug — silently
    falling back to local would hide spec/route drift, recreating the bug
    class ST-5 exists to prevent. Pin the explicit re-raise so the broad
    catch can't regress.
    """
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation
    from siftd.api.op_spec import MissingOpSpec
    from siftd.serve import delegation

    op = Operation(
        path="/api/v1/nope-not-a-real-route",
        method="GET",
        fn=lambda **kwargs: kwargs,
        params={"q": "hi"},
        render_method="raw",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )
    with pytest.raises(MissingOpSpec):
        delegation.try_serve(op)


def test_to_wire_raises_missing_op_spec_for_unregistered_path():
    """Wire serialization without a spec must fail loudly, not silently leak params.

    A typo'd path or a forgotten SPECS entry would otherwise send every
    op.params key — including local-only keys like db_path — recreating the
    bug class this substrate dissolves.
    """
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation
    from siftd.api.op_spec import MissingOpSpec

    op = Operation(
        path="/api/v1/nope-not-a-real-route",
        method="GET",
        fn=lambda **kwargs: kwargs,
        params={"db_path": Path("/tmp/x")},
        render_method="raw",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )
    with pytest.raises(MissingOpSpec):
        op.to_wire()


def test_to_local_handles_unregistered_path_gracefully():
    """to_local on an unregistered op returns params unchanged.

    Local execution doesn't need a spec — only delegation does. Purely-local
    commands (no wire form) are valid operations.
    """
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation

    op = Operation(
        path="/api/v1/nope-not-a-real-route",
        method="GET",
        fn=lambda **kwargs: kwargs,
        params={"q": "hi", "n": 5},
        render_method="raw",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )
    assert op.to_local() == {"q": "hi", "n": 5}


# --- Fidelity adapter (typed serializer) ---


def test_fidelity_to_wire_thinking_and_tools():
    """fidelity_to_wire emits the two boolean axis fields the routes accept."""
    from painted import Fidelity

    from siftd.api.op_spec import fidelity_to_wire

    fid = Fidelity(visible=frozenset({"text", "thinking", "tools"}))
    out = fidelity_to_wire(fid)
    assert out == {"include_thinking": True, "include_tool_content": True}


def test_fidelity_to_wire_text_only():
    from painted import Fidelity

    from siftd.api.op_spec import fidelity_to_wire

    fid = Fidelity(visible=frozenset({"text"}))
    out = fidelity_to_wire(fid)
    assert out == {"include_thinking": False, "include_tool_content": False}


# --- Full seam: substituted path → spec lookup → wire form (production path) ---


def test_full_seam_substituted_path_resolves_to_conversation_detail_spec():
    """End-to-end: an Operation built with a substituted ID path produces the
    correct wire form via spec_for + apply_wire.

    This is the seam the refactor exists to protect — the CLI builds
    ``f"/api/v1/conversations/{conv_id}"`` (substituted), and ``to_wire`` must
    template-match it back to the SPECS key ``/api/v1/conversations/{id}``
    before applying ``wire_excludes`` and Fidelity expansion. Without this
    test the individual pieces could be green while their composition silently
    leaks ``db_path`` onto the wire.
    """
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation

    fid = Fidelity(visible=frozenset({"text", "thinking"}))
    op = Operation(
        path="/api/v1/conversations/01HXABC123",  # substituted form, as CLI builds
        method="GET",
        fn=lambda **kwargs: kwargs,
        params={
            "id": "01HXABC123",
            "fidelity": fid,
            "tool_filter": None,
            "anchor": "at_turn",
            "anchor_value": 5,
            "db_path": Path("/tmp/x"),       # must be dropped (local-only)
            "around": "phrase-anchor",       # must be dropped (CLI annotation)
        },
        render_method="detail",
        fidelity=fid,
        db=Path("/tmp/x"),
    )
    q = op.to_wire()
    assert "db_path" not in q, "local DB path must not leak to the wire"
    assert "around" not in q, "CLI annotation must not leak to the wire"
    assert "fidelity" not in q, "opaque Fidelity object must be dropped"
    assert "tool_filter" not in q, "None values must be dropped"
    assert q["include_thinking"] is True
    assert q["include_tool_content"] is False
    assert q["anchor"] == "at_turn"
    assert q["anchor_value"] == 5
    assert q["id"] == "01HXABC123"


# --- Path normalization (single template today) ---


def test_normalize_path_maps_substituted_id_to_template():
    """The conversation-detail URL is the one path siftd interpolates a runtime ID into."""
    from siftd.api.op_spec import _normalize_path

    assert _normalize_path("/api/v1/conversations/01HXABC123") == "/api/v1/conversations/{id}"
    assert _normalize_path("/api/v1/conversations") == "/api/v1/conversations"
    # Already-templated form passes through unchanged.
    assert _normalize_path("/api/v1/conversations/{id}") == "/api/v1/conversations/{id}"
    # Unrelated paths pass through.
    assert _normalize_path("/api/v1/search") == "/api/v1/search"


# --- Serve route accepts anchor + window params ---


def test_conversation_detail_route_accepts_anchor_and_window(monkeypatch, tmp_path):
    """/api/v1/conversations/{id} must thread anchor+window into get_conversation."""
    from siftd.serve import routes

    captured: dict = {}

    def fake_dispatch(path, method, fn, params, render_method, db, **kwargs):
        captured.update(params)
        captured["_path"] = path
        captured["_render_method"] = render_method
        return {"ok": True}

    monkeypatch.setattr(routes, "_dispatch", fake_dispatch)
    db = tmp_path / "team.db"
    req = SimpleNamespace()

    _run(routes.conversation_detail.fn(
        req, db, id="abc",
        include_thinking=True, include_tool_content=False, tool_filter=None,
        anchor="at_turn", anchor_value="5", window_start=-2, window_end=2,
    ))

    assert captured["_path"] == "/api/v1/conversations"
    assert captured["id"] == "abc"
    assert captured["anchor"] == "at_turn"
    # anchor_value coerced to int for at_turn
    assert captured["anchor_value"] == 5
    assert isinstance(captured["anchor_value"], int)
    assert captured["window_start"] == -2
    assert captured["window_end"] == 2


def test_conversation_detail_route_anchor_value_str_for_around(monkeypatch, tmp_path):
    """anchor_value stays str for anchor=around (phrase, not int)."""
    from siftd.serve import routes

    captured: dict = {}

    def fake_dispatch(path, method, fn, params, render_method, db, **kwargs):
        captured.update(params)
        return {"ok": True}

    monkeypatch.setattr(routes, "_dispatch", fake_dispatch)
    db = tmp_path / "team.db"
    req = SimpleNamespace()

    _run(routes.conversation_detail.fn(
        req, db, id="abc",
        anchor="around", anchor_value="error", window_start=-2, window_end=2,
    ))

    assert captured["anchor"] == "around"
    assert captured["anchor_value"] == "error"
    assert isinstance(captured["anchor_value"], str)


def test_conversation_detail_route_rejects_non_int_anchor_value_for_at_turn(tmp_path):
    """anchor=at_turn with non-numeric anchor_value must 400, not silently misbehave."""
    from litestar.response import Response

    from siftd.serve import routes

    db = tmp_path / "team.db"
    req = SimpleNamespace()

    out = _run(routes.conversation_detail.fn(
        req, db, id="abc",
        anchor="at_turn", anchor_value="not-a-number",
    ))

    assert isinstance(out, Response)
    assert out.status_code == 400


def test_conversation_detail_route_defaults_anchor_to_none(monkeypatch, tmp_path):
    """When CLI doesn't pass anchor, the route must pass anchor=None (not error)."""
    from siftd.serve import routes

    captured: dict = {}

    def fake_dispatch(path, method, fn, params, render_method, db, **kwargs):
        captured.update(params)
        return {"ok": True}

    monkeypatch.setattr(routes, "_dispatch", fake_dispatch)
    db = tmp_path / "team.db"
    req = SimpleNamespace()

    _run(routes.conversation_detail.fn(
        req, db, id="abc",
        include_thinking=False, include_tool_content=False, tool_filter=None,
        anchor=None, anchor_value=None, window_start=None, window_end=None,
    ))

    assert captured["anchor"] is None
    assert captured["anchor_value"] is None
    assert captured["window_start"] is None
    assert captured["window_end"] is None
