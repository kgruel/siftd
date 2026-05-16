"""Tests for wire-format expansion + parity between CLI op params and serve routes.

Covers two parity properties that previously caused silent drift on delegated reads:
1. Fidelity objects in op.params must expand to include_thinking/include_tool_content
   wire fields, not be stringified opaquely by urlencode.
2. /api/v1/conversations/{id} must accept anchor + window query params and pass
   them through to get_conversation (so CLI `query <id> --json --at-turn N` against
   a remote serve actually anchors).

Also exercises the named entry points introduced in the wire-form dissolution:
``api.dispatch.local_kwargs(op)`` and ``serve.delegation.wire_query(op)``.
See ``docs/guides/delegation-contract.md`` for the pattern.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve


def _run(coro):
    return asyncio.run(coro)


# --- local_kwargs / wire_query named entry points ---


def test_local_kwargs_strips_annotation_keys():
    """local_kwargs drops _LOCAL_FN_EXCLUDE keys (CLI annotations the fn doesn't take)."""
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation, local_kwargs

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
    out = local_kwargs(op)
    assert "around" not in out
    assert "debug_ids" not in out
    assert "embeddings_only" not in out
    assert "action" not in out
    assert out["q"] == "hello"
    assert out["n"] == 10
    assert out["db_path"] == Path("/tmp/x")


def test_wire_query_strips_db_path_and_translates_fidelity():
    """wire_query drops db_path, expands Fidelity, drops None, applies key renames."""
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation
    from siftd.serve.delegation import wire_query

    fid = Fidelity(visible=frozenset({"text", "thinking"}))
    op = Operation(
        path="/api/v1/conversations/abc",
        method="GET",
        fn=lambda **kwargs: kwargs,
        params={
            "id": "abc",
            "fidelity": fid,
            "tool_filter": None,            # dropped (None)
            "lambda_": 0.5,                  # renamed → lambda
            "db_path": Path("/tmp/x"),       # dropped (local-only)
            "anchor": "at_turn",
        },
        render_method="detail",
        fidelity=fid,
        db=Path("/tmp/x"),
    )
    q = wire_query(op)
    assert "db_path" not in q
    assert "tool_filter" not in q
    assert "fidelity" not in q
    assert "lambda_" not in q
    assert q["include_thinking"] is True
    assert q["include_tool_content"] is False
    assert q["lambda"] == 0.5
    assert q["anchor"] == "at_turn"
    assert q["id"] == "abc"


def test_wire_body_keeps_db_path_excluded_but_preserves_None():
    """wire_body (POST) drops db_path but preserves other shape — JSON bodies handle None."""
    from pathlib import Path

    from painted import Fidelity

    from siftd.api.dispatch import Operation
    from siftd.serve.delegation import wire_body

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
    body = wire_body(op)
    assert "db_path" not in body
    assert body["action"] == "apply"
    assert body["tags"] == ["foo", "bar"]
    assert body["entity_id"] == "abc"
    # Note: None is preserved for POST bodies; JSON null is a legitimate value.
    # The route handler distinguishes "field omitted" (KeyError) from "field=null".
    assert "last" in body and body["last"] is None


# --- Fidelity expansion in _remap_params ---


def test_expand_for_wire_translates_fidelity_to_axis_fields():
    """Fidelity in params expands into include_thinking + include_tool_content."""
    from painted import Fidelity

    from siftd.serve.delegation import _expand_for_wire

    fid = Fidelity(visible=frozenset({"text", "thinking", "tools"}))
    out = _expand_for_wire({"id": "abc", "fidelity": fid})

    assert "fidelity" not in out, "opaque Fidelity object must be dropped"
    assert out["include_thinking"] is True
    assert out["include_tool_content"] is True
    assert out["id"] == "abc"


def test_expand_for_wire_text_only_fidelity_yields_false_flags():
    from painted import Fidelity

    from siftd.serve.delegation import _expand_for_wire

    fid = Fidelity(visible=frozenset({"text"}))
    out = _expand_for_wire({"fidelity": fid})

    assert out["include_thinking"] is False
    assert out["include_tool_content"] is False


def test_expand_for_wire_passes_through_scalars():
    from siftd.serve.delegation import _expand_for_wire

    out = _expand_for_wire({"id": "abc", "n": 5, "anchor": "at_turn", "anchor_value": 3})
    assert out == {"id": "abc", "n": 5, "anchor": "at_turn", "anchor_value": 3}


def test_remap_params_chains_expansion_and_remap():
    """_remap_params expands Fidelity AND remaps lambda_ → lambda."""
    from painted import Fidelity

    from siftd.serve.delegation import _remap_params

    fid = Fidelity(visible=frozenset({"text", "thinking"}))
    out = _remap_params({"lambda_": 0.5, "fidelity": fid, "id": "x"})
    assert out["lambda"] == 0.5
    assert "lambda_" not in out
    assert out["include_thinking"] is True
    assert out["include_tool_content"] is False
    assert "fidelity" not in out


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
