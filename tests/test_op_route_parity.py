"""Contract tests: CLI Operation params must be accepted by their serve route.

The wire-form parity drift that motivated `docs/guides/delegation-contract.md`
(see commits ce0a1a0f..9d9d31ad on the homelab-thin-client branch) was caused
by silent divergence between what the CLI puts in ``op.params`` and what the
matching Litestar route declares as ``Parameter(query=...)``. Litestar
silently drops unknown query params, so the bug was invisible to existing
tests until someone ran a real delegated call against a real server.

These tests assert the property directly: for each representative delegated
read, every key in ``wire_query(op)`` must be a name the matching serve route
will actually parse. New delegated paths should add a case here; the
contract doc directs contributors to this file.

Implementation note: we introspect Litestar route functions via
``inspect.signature``. Litestar's ``Parameter(query="...")`` is exposed as a
default value with a ``.query`` attribute on the parameter, so we can read
the wire name without instantiating Litestar's machinery.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from siftd.api.dispatch import Operation
from siftd.serve import routes


def _declared_query_names(route_fn) -> set[str]:
    """Extract the set of query param names a Litestar route accepts.

    Reads ``Parameter(query="...")`` defaults from the route's signature. The
    wrapped Litestar handler exposes the original function as ``.fn``.
    """
    fn = getattr(route_fn, "fn", route_fn)
    sig = inspect.signature(fn)
    names: set[str] = set()
    for pname, param in sig.parameters.items():
        default = param.default
        # Litestar Parameter() exposes its query name via .query.
        query = getattr(default, "query", None)
        if isinstance(query, str):
            names.add(query)
    return names


def _path_param_names(route_path: str) -> set[str]:
    """Extract `{name}` placeholders from a route path."""
    out = set()
    i = 0
    while i < len(route_path):
        if route_path[i] == "{":
            j = route_path.find("}", i)
            if j > i:
                # Strip type suffix like {id:str}
                inner = route_path[i + 1:j].split(":")[0]
                out.add(inner)
                i = j + 1
                continue
        i += 1
    return out


# ---------------------------------------------------------------------------
# Per-route contract tests
# ---------------------------------------------------------------------------


def test_conversation_detail_op_keys_accepted_by_route():
    """The CLI's `query <id>` op must match /api/v1/conversations/{id}'s params.

    This is the contract that drifted for months pre-Phase-A — `anchor`,
    `anchor_value`, `window_start`, `window_end` were in the CLI op but
    Litestar silently dropped them at the route. Reproduces the catch.
    """
    from painted import Fidelity

    op = Operation(
        path="/api/v1/conversations/{id}",
        method="GET",
        fn=lambda **kwargs: None,
        params={
            "id": "01HX...",
            "fidelity": Fidelity(visible=frozenset({"text", "thinking"})),
            "tool_filter": "shell.run",
            "anchor": "at_turn",
            "anchor_value": 5,
            "window_start": -1,
            "window_end": 1,
            "db_path": Path("/tmp/x"),
        },
        render_method="detail",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )

    wire_keys = set(op.to_wire().keys())
    route_keys = _declared_query_names(routes.conversation_detail)
    path_keys = _path_param_names("/api/v1/conversations/{id:str}")

    accepted = route_keys | path_keys
    leftover = wire_keys - accepted
    assert not leftover, (
        f"CLI op for /api/v1/conversations/{{id}} sends wire keys not declared on the route: "
        f"{sorted(leftover)}. Either add a Parameter(query=...) on the route or "
        f"extend the matching OpSpec's wire_excludes in siftd.api.op_spec.SPECS."
    )


def test_conversation_list_op_keys_accepted_by_route():
    """The CLI's `query` list mode against /api/v1/conversations."""
    from painted import Fidelity

    op = Operation(
        path="/api/v1/conversations",
        method="GET",
        fn=lambda **kwargs: None,
        params={
            "fidelity": Fidelity(),
            "workspace": "/proj",
            "since": "2024-01-01",
            "before": "2024-12-31",
            "model": "gpt-4",
            "tag": ["work"],
            "all_tags": None,
            "no_tag": None,
            "tag_kind": None,
            "tool": None,
            "tool_tag": None,
            "search": "error",
            "n": 20,
            "oldest": False,
            "db_path": Path("/tmp/x"),
        },
        render_method="list",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )

    wire_keys = set(op.to_wire().keys())
    route_keys = _declared_query_names(routes.conversation_list)

    leftover = wire_keys - route_keys
    assert not leftover, (
        f"CLI op for /api/v1/conversations sends wire keys the route doesn't declare: "
        f"{sorted(leftover)}"
    )


def test_search_op_keys_accepted_by_route():
    """The CLI's `siftd search` op against /api/v1/search.

    Round-4 review found that this op was sending `embed_db` (local path,
    info leak) and `around` (CLI annotation) — silently dropped by Litestar.
    The fix was to add those keys to ``_WIRE_EXCLUDE``; this test pins the
    contract so the bug class can't regress.

    ST-4a update: `mode` was also in `_WIRE_EXCLUDE` (the route had no
    matching Parameter), which meant FTS mode was unreachable on the wire.
    ST-4a added ``mode`` to the route and removed it from ``_WIRE_EXCLUDE``,
    so ``mode`` now travels on the wire. The assertion below reflects this.
    """
    from pathlib import Path as _Path

    from painted import Fidelity

    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=lambda **kwargs: None,
        params={
            "q": "hello",
            "db_path": _Path("/tmp/x"),
            "embed_db": _Path("/tmp/embed.db"),  # local-only — must not reach wire
            "n": 10,
            "mode": "hybrid",                    # now travels on the wire (ST-4a)
            "workspace": None,
            "model": None,
            "since": None,
            "before": None,
            "tag": None,
            "all_tags": None,
            "no_tag": None,
            "tag_kind": None,
            "owner": None,
            "exclude_active": True,
            "include_derivative": False,
            "recall": 80,
            "rerank": "mmr",
            "lambda_": 0.7,
            "recency": False,
            "recency_half_life": 30.0,
            "recency_max_boost": 1.15,
            "backend": None,
            "embeddings_only": False,            # deprecated alias; still sent for old-server compat
            "raw_fts": False,
            "debug_ids": False,                  # CLI annotation
            "around": None,                      # CLI annotation
        },
        render_method="search",
        fidelity=Fidelity(),
        db=_Path("/tmp/x"),
    )

    wire_keys = set(op.to_wire().keys())
    route_keys = _declared_query_names(routes.search_route)

    leftover = wire_keys - route_keys
    assert not leftover, (
        f"CLI search op sends wire keys the route doesn't declare: "
        f"{sorted(leftover)}. Round-4 caught this for embed_db/around — "
        f"any new addition needs either a Parameter() on the route or an "
        f"entry in the search OpSpec's wire_excludes in siftd.api.op_spec.SPECS."
    )
    # Local paths and CLI annotations must not bleed to the wire.
    assert "embed_db" not in wire_keys, "local embeddings DB path must not leak to the wire"
    assert "around" not in wire_keys, "around is a CLI-only post-processing annotation"
    # mode now travels on the wire so the route can dispatch all three modes (ST-4a).
    assert "mode" in wire_keys, "mode must travel to the route (ST-4a: fts/hybrid/semantic)"


def test_export_op_format_aware_keys_accepted_by_route():
    """The format-aware path (Phase C). CLI's `cmd_export` builds this shape."""
    from painted import Fidelity

    op = Operation(
        path="/api/v1/export",
        method="GET",
        fn=lambda **kwargs: None,
        params={
            "format": "md",
            "fidelity": Fidelity(visible=frozenset({"text", "thinking"})),
            "no_header": False,
            "id": None,
            "last": 1,
            "workspace": None,
            "tag": None,
            "no_tag": None,
            "tag_kind": None,
            "since": None,
            "before": None,
            "search": None,
            "db_path": Path("/tmp/x"),
        },
        render_method="export-artifact",
        fidelity=Fidelity(),
        db=Path("/tmp/x"),
    )

    wire_keys = set(op.to_wire().keys())
    route_keys = _declared_query_names(routes.export_route)

    leftover = wire_keys - route_keys
    assert not leftover, (
        f"CLI export op sends wire keys the route doesn't declare: {sorted(leftover)}"
    )


# ---------------------------------------------------------------------------
# Sanity check: the introspection works at all
# ---------------------------------------------------------------------------


def test_route_parameter_introspection_finds_expected_names():
    """Sanity: our _declared_query_names helper correctly reads Parameter()
    defaults — guards against silently-broken contract tests above."""
    names = _declared_query_names(routes.conversation_detail)
    # We know these are declared (added in commit 2f230d6e).
    assert "anchor" in names
    assert "anchor_value" in names
    assert "window_start" in names
    assert "window_end" in names
    assert "include_thinking" in names
    assert "include_tool_content" in names
    assert "tool_filter" in names


# ---------------------------------------------------------------------------
# Registry-iteration parity: every OpSpec entry has a matching Litestar route
# ---------------------------------------------------------------------------


def _route_map() -> dict[tuple[str, str], object]:
    """Build {(template_path, method): handler_fn} for every route in siftd.serve.routes.

    Litestar's HTTPRouteHandler exposes ``paths`` (set of literal route paths,
    with Litestar type suffixes like ``{id:str}``) and ``http_methods`` (set
    of HTTP methods). This walker normalizes the paths to the SPECS template
    form (stripping ``:str`` and friends) so the result is directly comparable
    to keys in :data:`siftd.api.op_spec.SPECS`.
    """
    import re as _re

    out: dict[tuple[str, str], object] = {}
    for name in dir(routes):
        handler = getattr(routes, name)
        # Filter to actual Litestar handlers: they expose `.fn` (the wrapped
        # async function). Class-level descriptors on imported types also
        # satisfy `hasattr(..., "paths")` but aren't real route handlers.
        if not callable(getattr(handler, "fn", None)):
            continue
        paths = getattr(handler, "paths", None)
        methods = getattr(handler, "http_methods", None)
        if not paths or not methods:
            continue
        for raw_path in paths:
            template = _re.sub(r"\{(\w+):\w+\}", r"{\1}", str(raw_path))
            for method in methods:
                out[(template, str(method))] = handler
    return out


def test_every_op_spec_has_matching_route():
    """Every entry in :data:`SPECS` must correspond to a real Litestar route.

    Catches orphan specs (route removed without removing its spec) and typos
    in spec keys. Combined with :func:`Operation.to_wire` raising
    :class:`MissingOpSpec` for un-specced ops, this closes the "silent wire
    drift" bug class declaratively.
    """
    from siftd.api.op_spec import SPECS

    rmap = _route_map()
    missing = [key for key in SPECS if key not in rmap]
    assert not missing, (
        f"OpSpec entries with no matching Litestar route: {missing}. "
        f"Either remove the spec from siftd.api.op_spec.SPECS or add the route "
        f"in siftd.serve.routes."
    )


def test_every_op_spec_wire_remap_lands_in_route():
    """If a spec remaps ``foo`` → ``bar``, the route must declare ``bar`` as a query param.

    Remaps exist because of Python-keyword collisions (``lambda_`` → ``lambda``).
    A remap that doesn't land on a declared route param means the renamed key
    is silently dropped — exactly the bug class this substrate dissolves.
    """
    from siftd.api.op_spec import SPECS

    rmap = _route_map()
    failures = []
    for (template, method), spec in SPECS.items():
        handler = rmap.get((template, method))
        if handler is None:
            continue  # covered by the previous test
        declared = _declared_query_names(handler)
        for src_name, wire_name in spec.wire_remaps.items():
            if wire_name not in declared:
                failures.append((template, method, src_name, wire_name))
    assert not failures, (
        f"OpSpec wire_remaps that don't land on a declared route param: {failures}. "
        f"Either remove the remap or add a Parameter(query=...) on the route."
    )
