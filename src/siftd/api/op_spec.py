"""Per-operation wire/local serialization rules.

Every CLI command that can be delegated to ``siftd-serve`` has a local form
(``op.fn(**op.to_local())``) and a wire form (``urlencode(op.to_wire())`` →
Litestar route → ``op.fn``). Historically the rules that translate between
``op.params`` and each form lived as four scattered module-level workarounds:

- ``api/dispatch.py:_LOCAL_FN_EXCLUDE`` — keys to drop for local execution
- ``serve/delegation.py:_WIRE_EXCLUDE`` — keys to drop for wire
- ``serve/delegation.py:_SERVE_PARAM_MAP`` — Python-keyword renames
- ``serve/delegation.py:_expand_for_wire`` — non-scalar expansion (``Fidelity``)

Those four workarounds drifted silently for months — the result-nav slice
sequence added ``anchor`` / ``window_*`` to ``op.params`` without updating
the matching Litestar route, and ``Fidelity`` was urlencoded into garbage
the route ignored. See ``docs/guides/delegation-contract.md`` and the
``wire-format-parity-pattern`` memory for the history.

This module names the pattern: one :class:`OpSpec` per logical operation,
keyed by ``(path_template, method)``. :meth:`Operation.to_local` and
:meth:`Operation.to_wire` apply the spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from painted import Fidelity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from siftd.api.dispatch import Operation


class MissingOpSpec(LookupError):
    """Raised when ``Operation.to_wire`` is called for an op with no registered spec.

    Wire serialization without a spec would silently send every ``op.params``
    key — including local-only keys like ``db_path`` (filesystem leak) and
    annotations like ``around`` that Litestar would drop. Better to fail loudly
    at delegation time than to recreate the bug class.
    """


@dataclass(frozen=True)
class OpSpec:
    """Wire/local serialization rules for one logical operation.

    Attributes:
        local_excludes: ``op.params`` keys that aren't accepted by the local
            ``op.fn`` (CLI annotations, routing keys). Stripped by ``to_local``.
        wire_excludes: ``op.params`` keys that must not travel on the wire
            (local paths, CLI annotations the route doesn't declare). Stripped
            by ``to_wire``.
        wire_remaps: Local kwarg name → wire query param name. Only needed for
            Python-keyword collisions (``lambda_`` → ``lambda``).
    """

    local_excludes: frozenset[str] = frozenset()
    wire_excludes: frozenset[str] = frozenset()
    wire_remaps: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Registry — one entry per delegated logical operation.
# ---------------------------------------------------------------------------
#
# Keys are ``(path_template, method)``. Templates use ``{name}`` placeholders
# (no Litestar type suffix like ``:str``). For CLI sites that interpolate
# path params (today: only ``/api/v1/conversations/{id}``), see
# :func:`_normalize_path`.

_LOCAL_FN_EXCLUDE_SEARCH = frozenset({"action", "embeddings_only", "debug_ids", "around"})
_WIRE_EXCLUDE_COMMON = frozenset({"db_path", "around"})


SPECS: dict[tuple[str, str], OpSpec] = {
    ("/api/v1/conversations", "GET"): OpSpec(
        wire_excludes=_WIRE_EXCLUDE_COMMON,
    ),
    ("/api/v1/conversations/{id}", "GET"): OpSpec(
        wire_excludes=_WIRE_EXCLUDE_COMMON,
    ),
    ("/api/v1/search", "GET"): OpSpec(
        local_excludes=_LOCAL_FN_EXCLUDE_SEARCH,
        # ``embed_db`` is a local-only filesystem path; leaking it to the
        # remote would expose host paths and the server ignores it anyway.
        wire_excludes=_WIRE_EXCLUDE_COMMON | frozenset({"embed_db"}),
        wire_remaps={"lambda_": "lambda"},
    ),
    ("/api/v1/stats", "GET"): OpSpec(
        wire_excludes=_WIRE_EXCLUDE_COMMON,
    ),
    ("/api/v1/workspaces", "GET"): OpSpec(
        wire_excludes=_WIRE_EXCLUDE_COMMON,
    ),
    ("/api/v1/tags", "GET"): OpSpec(
        wire_excludes=_WIRE_EXCLUDE_COMMON,
    ),
    ("/api/v1/export", "GET"): OpSpec(
        wire_excludes=_WIRE_EXCLUDE_COMMON,
    ),
    ("/api/v1/tag", "POST"): OpSpec(
        # ``action`` is a routing key the server reads to dispatch
        # apply/remove/rename/delete; the local typed fns (rename_tag_safe,
        # delete_tag_safe, apply_tags) don't accept it. CLI sites today
        # bypass execute() in the local fallback path, but the local-exclude
        # preserves the pre-ST-5 _LOCAL_FN_EXCLUDE semantics so a future
        # refactor to execute(op) doesn't TypeError.
        local_excludes=frozenset({"action"}),
        # POST body keeps None values (JSON null is meaningful); only strip
        # local-only paths.
        wire_excludes=_WIRE_EXCLUDE_COMMON,
    ),
}


# Only one delegated path interpolates a runtime value into the URL today
# (``/api/v1/conversations/{id}``). Other routes with path params
# (``/api/v1/events/{event_id}``, ``/api/v1/sessions/{id}/tags``) exist but
# are not constructed as CLI delegated Operations.
#
# Contract: if a new CLI command constructs an Operation against a route with
# a path param, extend this normalizer (add a regex + matching branch in
# :func:`_normalize_path`) or replace the regex set with a route-template
# walker that reads templates from ``siftd.serve.routes``. Without that,
# ``spec_for`` returns ``None`` and ``to_wire`` raises ``MissingOpSpec`` —
# loud failure, not silent drift, but the new path won't be delegatable
# until the normalizer is updated.
_CONVERSATION_DETAIL_RE = re.compile(r"^/api/v1/conversations/[^/{]+$")


def _normalize_path(path: str) -> str:
    """Map a substituted URL back to its template key in :data:`SPECS`.

    CLI sites build ``op.path`` as ``f"/api/v1/conversations/{conv_id}"`` —
    the substituted form, which is the URL ``try_delegate`` needs to fetch.
    But the SPECS key is the template form. This function does the inverse
    substitution. Paths that already are templates (have ``{id}`` in them
    or no path params at all) pass through unchanged.
    """
    if "{" in path:
        return path  # already a template (e.g. server-side _dispatch call)
    if _CONVERSATION_DETAIL_RE.match(path):
        return "/api/v1/conversations/{id}"
    return path


def spec_for(op: Operation) -> OpSpec | None:
    """Return the :class:`OpSpec` for an :class:`Operation`, or ``None`` if unregistered.

    ``None`` is a legitimate result for operations that never delegate (e.g.
    purely local commands that happen to be modeled as Operations). The
    delegation path :meth:`Operation.to_wire` upgrades the ``None`` to
    :class:`MissingOpSpec`; local-only callers via :meth:`Operation.to_local`
    treat ``None`` as "no exclusions."
    """
    return SPECS.get((_normalize_path(op.path), op.method))


def apply_local(params: dict[str, Any], spec: OpSpec | None) -> dict[str, Any]:
    """Apply ``spec.local_excludes`` to ``params``. ``None`` spec ⇒ identity.

    Helper exposed for tests; production callers use :meth:`Operation.to_local`.
    """
    if spec is None:
        return dict(params)
    return {k: v for k, v in params.items() if k not in spec.local_excludes}


def apply_wire(params: dict[str, Any], spec: OpSpec) -> dict[str, Any]:
    """Apply ``spec.wire_excludes`` + non-scalar expansion + ``wire_remaps``.

    Helper exposed for tests; production callers use :meth:`Operation.to_wire`.

    Steps:
      1. Drop keys in ``spec.wire_excludes`` and any whose value is ``None``
         (``urlencode`` would emit them as the literal string ``"None"``,
         which the route then parses as a real value).
      2. Expand non-scalar types — today only :class:`painted.Fidelity` is
         non-scalar; it becomes the boolean axis fields
         ``include_thinking`` + ``include_tool_content``.
      3. Apply ``spec.wire_remaps`` for Python-keyword renames.
    """
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k in spec.wire_excludes or v is None:
            continue
        if isinstance(v, Fidelity):
            out.update(fidelity_to_wire(v))
            continue
        out[k] = v
    return {spec.wire_remaps.get(k, k): v for k, v in out.items()}


def apply_wire_body(params: dict[str, Any], spec: OpSpec) -> dict[str, Any]:
    """Apply ``spec.wire_excludes`` for POST bodies. Preserves ``None`` (JSON null).

    POST bodies don't go through urlencode, so ``None`` is legitimate. Fidelity
    doesn't appear in POST bodies today; if a future POST route carries one,
    extend this function rather than reusing :func:`apply_wire`'s expansion
    (the boolean-flag shape is querystring-specific).
    """
    return {k: v for k, v in params.items() if k not in spec.wire_excludes}


def fidelity_to_wire(f: Fidelity) -> dict[str, bool]:
    """Translate :class:`painted.Fidelity` into the wire's boolean axis fields.

    The Litestar routes accept ``include_thinking`` + ``include_tool_content``
    as separate query params. This is the typed serializer; siftd-side
    adapter pending a future painted-side ``Fidelity.to_wire`` upstream.
    """
    return {
        "include_thinking": bool(f.shows("thinking")),
        "include_tool_content": bool(f.shows("tools")),
    }
