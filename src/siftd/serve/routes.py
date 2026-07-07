"""Route handlers for siftd serve."""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from litestar import Request, get, post
from litestar.background_tasks import BackgroundTask
from litestar.params import Parameter
from litestar.response import File, Response

log = logging.getLogger(__name__)


def _effective_owner(request: Request, owner: str | None) -> str | None:
    """Bind owner filtering to the authenticated identity when auth is enabled.

    When auth middleware is installed, request.user is always present for
    non-no_auth routes and contains a UserIdentity with .sub.
    When auth is not installed, request.user access raises — owner remains advisory.
    """
    try:
        user = request.user
    except Exception:
        return owner
    sub = getattr(user, "sub", None)
    if sub and sub != "anonymous":
        return sub
    return owner


def _actor_identity(request: Request) -> str:
    """Resolve the authenticated actor for audit records, or 'anonymous'."""
    return _effective_owner(request, None) or "anonymous"


def _client_ip(request: Request) -> str | None:
    """Resolve the real client IP, honoring X-Forwarded-For only from trusted proxies.

    Behind a reverse proxy (Caddy), ``request.client.host`` is the proxy's
    address, so provenance records lose the real client. When the immediate
    peer is in the configured ``serve.trusted_proxies`` list, trust the
    left-most ``X-Forwarded-For`` entry; otherwise fall back to the peer. With
    no configuration, no XFF is trusted (a client could otherwise spoof it).
    See finding F8b.
    """
    peer = getattr(getattr(request, "client", None), "host", None)
    try:
        from siftd.config import get_config

        trusted = get_config("serve.trusted_proxies")
    except Exception:
        trusted = None
    if peer and trusted:
        trusted_set = {p.strip() for p in str(trusted).split(",") if p.strip()}
        if peer in trusted_set:
            xff = request.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()
    return peer


def _not_found_message(path: str) -> str:
    """Entity-specific 404 message derived from the dispatch path.

    Explicit map rather than singularizing the path segment — the existing
    conversation-detail body (``"conversation not found"``) is contract-pinned
    by tests, so we preserve it exactly and extend per entity.
    """
    if "/workspaces" in path:
        return "workspace not found"
    return "conversation not found"


def _dispatch(
    path: str, method: str, fn: Callable, params: dict[str, Any],
    render_method: str, db: Path,
    render_context: dict | None = None,
    *,
    fidelity: Any | None = None,
) -> Any:
    """Build an Operation and dispatch it through the format protocol.

    Shared helper for simple routes: extract params → execute → render.
    Uses serve_fmt (serialization layer) so the JSON wire contract is
    owned by serialization/; html_routes deliberately uses output/ for
    fragment rendering.

    Catches exceptions and returns a structured JSON error response
    instead of letting raw tracebacks propagate as 500 errors.
    """
    import logging

    from painted import Fidelity

    from siftd.api.conversations import AmbiguousPrefix, AnchorError, QueryError
    from siftd.api.dispatch import Operation, execute, render
    from siftd.api.op_spec import spec_for_path
    from siftd.serialization import serve_fmt

    try:
        op = Operation(
            path=path, method=method, fn=fn, params=params,
            render_method=render_method, fidelity=fidelity or Fidelity(), db=db,
            render_context=render_context or {},
        )
        result = execute(op)
        # Per-entity detail Operations declare not_found_on_none in their OpSpec
        # (conversations/{id}, workspaces/{id}). A None result means "no such
        # entity" → 404. List/aggregate Operations leave the flag False so an
        # empty result renders as a normal 200.
        spec = spec_for_path(path, method)
        if spec is not None and spec.not_found_on_none and result is None:
            return Response(
                content={"error": _not_found_message(path)}, status_code=404,
            )

        # Run caveat producers (stale-embeddings/truncation/ambiguity warnings)
        # so the serve envelope carries them, like `siftd query/search --json`.
        # Without this the HTTP API silently drops every caveat for agents.
        # Caveats are advisory enrichment: a producer failure must never fail
        # the request, so this is best-effort and logged, not fatal.
        findings: list = []
        try:
            from siftd.api.caveats import ProducerContext, run_producers
            from siftd.paths import db_path as default_db_path

            ctx = ProducerContext(db_path=params.get("db_path") or db or default_db_path())
            try:
                findings = run_producers(op, result, ctx)
            finally:
                ctx.close()
        except Exception:
            logging.getLogger("siftd.serve").exception("caveat producers failed on %s %s", method, path)
            findings = []

        rendered = render(result, op, fmt=serve_fmt)
        if findings and isinstance(rendered, dict) and "caveats" not in rendered:
            rendered["caveats"] = serve_fmt.serialize_caveats(findings)
        return rendered
    except ImportError as e:
        if "siftd.embeddings" in str(e) or "fastembed" in str(e):
            return Response(content={"error": str(e)}, status_code=501)
        logging.getLogger("siftd.serve").exception("dispatch import error on %s %s", method, path)
        return Response(content={"error": f"{path} failed"}, status_code=500)
    except FileNotFoundError:
        # str(e) is "Database not found: <abs path>" — don't leak the path (F8a).
        logging.getLogger("siftd.serve").warning(
            "dispatch file-not-found on %s %s", method, path,
        )
        return Response(content={"error": "resource not found"}, status_code=404)
    except AnchorError as e:
        # AnchorOutOfRange / AnchorNotFound / AnchorPhraseInvalid are all
        # user-input errors (bad --at-turn N, --around PHRASE). The local CLI
        # treats them as exit 2 with a friendly message; the wire equivalent
        # is 400, not 500.
        return Response(content={"error": str(e)}, status_code=400)
    except AmbiguousPrefix as e:
        # Preserve the structured shape so an HTTP agent can programmatically
        # pick a longer prefix — mirrors `siftd id --json`. Must precede the
        # generic ValueError branch.
        return Response(
            content={
                "error": str(e),
                "kind": "ambiguous_prefix",
                "prefix": e.prefix,
                "matched_ids": e.matched_ids,
                "total": e.total,
            },
            status_code=400,
        )
    except (ValueError, KeyError, QueryError) as e:
        return Response(content={"error": str(e)}, status_code=400)
    except Exception as e:
        if e.__class__.__name__ == "EmbeddingsNotAvailable":
            return Response(content={"error": str(e)}, status_code=501)
        if e.__class__.__name__ == "EmbeddingConfigError":
            # A configured remote backend is present but unusable (e.g. a revoked key).
            # Not degradable (config errors never fall back to FTS) and not a generic 500 —
            # report it honestly as an unavailable dependency. Matched by name so serve
            # doesn't import siftd.embeddings (tests/architecture/test_imports.py).
            return Response(content={"error": str(e)}, status_code=503)

        logging.getLogger("siftd.serve").exception("dispatch error on %s %s", method, path)
        return Response(
            content={"error": f"{path} failed"},
            status_code=500,
        )


def _fidelity_from_visible(visible: str | None, *, depth: int = 1):
    """Build a Fidelity from a ``?visible=`` query param (comma-separated tags).

    The general client-facing mechanism for requesting result enrichments: a
    consumer asks for ``?visible=activity,facets`` and the listed tags land in
    ``Fidelity.visible``, which the API fns gate their optional (and possibly
    expensive) enrichment queries on. ``"text"`` is always present so the base
    payload is unaffected; unknown tags are harmless (a fn that doesn't honor a
    tag simply ignores it).
    """
    from painted import Fidelity

    tags = {"text"}
    if visible:
        tags |= {t.strip() for t in visible.split(",") if t.strip()}
    return Fidelity(depth=depth, visible=frozenset(tags))


@get("/api/v1", sync_to_thread=False)
def index() -> dict:
    """API index — list available endpoints."""
    return {
        "service": "siftd",
        "endpoints": [
            {"method": "GET", "path": "/api/v1/health", "description": "Health check and DB status"},
            {"method": "POST", "path": "/api/v1/push", "description": "Push a database slice"},
            {"method": "GET", "path": "/api/v1/pull", "description": "Pull a filtered database slice"},
            {"method": "GET", "path": "/api/v1/conversations", "description": "List conversations"},
            {"method": "GET", "path": "/api/v1/conversations/{id}", "description": "Get conversation detail"},
            {"method": "GET", "path": "/api/v1/search", "description": "Semantic + FTS search"},
            {"method": "GET", "path": "/api/v1/stats", "description": "Database statistics"},
            {"method": "GET", "path": "/api/v1/workspaces", "description": "List workspaces"},
            {"method": "GET", "path": "/api/v1/workspaces/{id}", "description": "Workspace detail by ULID"},
            {"method": "GET", "path": "/api/v1/tags", "description": "List tags with counts"},
            {"method": "GET", "path": "/api/v1/export", "description": "Export full conversations"},
            {"method": "POST", "path": "/api/v1/tag", "description": "Apply, remove, rename, or delete tags"},
            {"method": "POST", "path": "/api/v1/sessions/{id}/tags", "description": "Queue a pending tag for a live session"},
            {"method": "GET", "path": "/api/v1/events/{id}", "description": "Get a single event by ID"},
        ],
    }


@get("/api/v1/health", opt={"no_auth": True}, sync_to_thread=True)
def health(db_path: Path) -> dict:
    """Health check — returns DB status."""
    from siftd.api import get_health_status
    from siftd.serialization import serialize_health_status

    return serialize_health_status(get_health_status(db_path))


@get("/api/v1/stats", sync_to_thread=True)
def stats_route(request: Request, db_path: Path) -> dict | Response:
    """Return database statistics. Server has DB warm, so this is fast."""
    from siftd.api.stats import get_stats

    owner = _effective_owner(request, None)
    return _dispatch("/api/v1/stats", "GET", get_stats, {"db_path": db_path, "owner": owner}, "stats", db_path)


@get("/api/v1/workspaces", sync_to_thread=True)
def workspaces_route(
    request: Request,
    db_path: Path,
    n: int = Parameter(query="n", default=10000),
) -> dict | Response:
    """List workspaces with conversation counts."""
    from siftd.api.stats import list_workspaces

    owner = _effective_owner(request, None)
    return _dispatch(
        "/api/v1/workspaces", "GET", list_workspaces,
        {"db_path": db_path, "n": n, "owner": owner},
        "workspaces", db_path,
    )


@get("/api/v1/workspaces/{id:str}", sync_to_thread=True)
def workspace_detail_route(
    request: Request,
    db_path: Path,
    id: str,
) -> dict | Response:
    """Detail for one workspace, addressed by its ULID (workspaces.id).

    Mirrors /api/v1/conversations/{id}: the master list is /api/v1/workspaces,
    this is the per-entity detail (stat grid + by-model mix + recent sessions).
    404 when no workspace has that id. Owner-scoped via the effective owner.

    Routed through ``_dispatch`` (like the sibling detail/list routes) so it
    shares the structured-error envelope and caveat-channel plumbing; the 404
    comes from the OpSpec ``not_found_on_none`` flag.
    """
    from siftd.api.stats import workspace_detail

    owner = _effective_owner(request, None)
    fidelity = _fidelity_from_visible(None, depth=2)
    return _dispatch(
        "/api/v1/workspaces/{id}", "GET", workspace_detail,
        {"workspace_id": id, "fidelity": fidelity, "db_path": db_path,
         "owner": owner},
        "workspace_detail", db_path,
        fidelity=fidelity,
    )


@get("/api/v1/tags", sync_to_thread=True)
def tags_route(
    request: Request,
    db_path: Path,
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    visible: str | None = Parameter(query="visible", default=None),
) -> dict | Response:
    """List tags with usage counts.

    ``?visible=activity`` enriches each tag with a per-week activity sparkline
    (see :func:`siftd.api.tags.list_tags`); omitted, the enrichment query is
    skipped.
    """
    from siftd.api.tags import list_tags

    owner = _effective_owner(request, None)
    fidelity = _fidelity_from_visible(visible)
    return _dispatch(
        "/api/v1/tags", "GET", list_tags,
        {"db_path": db_path, "since": since, "before": before, "owner": owner,
         "fidelity": fidelity},
        "tags", db_path, fidelity=fidelity,
    )


@post("/api/v1/tag", status_code=200)
async def tag_write_route(request: Request, db_path: Path) -> dict | Response:
    """Apply, remove, rename, or delete tags.

    Request body (JSON):
      action: "apply" | "remove" | "rename" | "delete"
      tags: list[str]              — tag names (apply/remove)
      entity_type: str             — "conversation" (default), "workspace", "tool_call"
      entity_id: str               — target entity ID (apply/remove)
      last: int                    — apply/remove to N most recent (alternative to entity_id)
      old_name: str, new_name: str — for rename
      tag_name: str                — for delete
    """
    from litestar.exceptions import PermissionDeniedException

    from siftd.api import record_audit_event
    from siftd.api.tags import apply_tags, delete_tag_safe, rename_tag_safe
    from siftd.serialization.tags import (
        serialize_apply_result,
        serialize_delete_result,
        serialize_rename_result,
    )
    from siftd.serve.auth import require_write

    require_write(request)
    owner = _effective_owner(request, None)

    import json as json_mod

    try:
        body = json_mod.loads(await request.body())
    except (json_mod.JSONDecodeError, ValueError):
        return Response(content={"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return Response(content={"error": "request body must be a JSON object"}, status_code=400)

    action = body.get("action", "apply")
    audit_target: str | None = None
    audit_detail: str | None = None
    try:
        if action == "rename":
            old_name = str(body.get("old_name") or "")
            new_name = str(body.get("new_name") or "")
            result = rename_tag_safe(
                db_path=db_path, old_name=old_name, new_name=new_name, owner=owner,
            )
            payload = serialize_rename_result(result)
            audit_target, audit_detail = old_name, f"-> {new_name}"
        elif action == "delete":
            tag_name = str(body.get("tag_name") or "")
            result = delete_tag_safe(db_path=db_path, tag_name=tag_name, owner=owner)
            payload = serialize_delete_result(result)
            audit_target = tag_name
        else:
            tag_list = [str(t) for t in body.get("tags", [])]
            result = apply_tags(
                db_path=db_path,
                tags=tag_list,
                entity_type=str(body.get("entity_type", "conversation")),
                entity_id=body.get("entity_id"),
                last=body.get("last"),
                owner=owner,
                remove=action == "remove",
            )
            payload = serialize_apply_result(result)
            audit_target = (
                str(body.get("entity_id"))
                if body.get("entity_id") is not None
                else (f"last:{body.get('last')}" if body.get("last") is not None else None)
            )
            audit_detail = ",".join(tag_list) or None
    except PermissionError as e:
        raise PermissionDeniedException(str(e)) from e
    except ValueError as e:
        # Domain validation messages are safe to surface (no internal paths).
        return Response(content={"error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        # apply_tags/rename/delete overload FileNotFoundError for both a missing
        # db file AND safe, contract-pinned domain messages ("no matching
        # entities found", "Tag not found: <name>"). The db-file branch is
        # unreachable once F9 pre-creates the team DB, and the domain messages
        # carry no path — so surface str(e) here rather than genericizing it
        # (which would mask those two legitimate messages). See report note.
        return Response(content={"error": str(e)}, status_code=404)

    record_audit_event(
        db_path=db_path,
        actor=_actor_identity(request),
        action=f"tag.{action}",
        target_type=str(body.get("entity_type", "conversation"))
        if action in ("apply", "remove")
        else "tag",
        target=audit_target,
        detail=audit_detail,
        source_ip=_client_ip(request),
    )

    # Refresh stats cache
    try:
        from siftd.api.stats import effective_db_mtime_ns, get_stats, write_stats_cache

        db_mtime = effective_db_mtime_ns(db_path)  # captured before the sweep
        write_stats_cache(get_stats(db_path=db_path), db_mtime_ns=db_mtime)
    except Exception:
        pass
    return payload


@get("/api/v1/events/{event_id:str}", sync_to_thread=True)
def event_detail_route(
    request: Request, event_id: str, db_path: Path,
    neighbors: bool = Parameter(query="neighbors", default=False),
) -> dict | Response:
    """Return a single event by ID."""
    from siftd.api.events import get_event
    from siftd.serialization.events import serialize_event_detail

    # Scope to the authenticated identity like every other read route — the
    # auth middleware only authenticates, it does not scope reads. A
    # cross-owner event resolves to None below and surfaces as 404 (not 403),
    # so existence isn't leaked across tenants.
    owner = _effective_owner(request, None)

    try:
        detail = get_event(
            event_id, db_path=db_path, include_neighbors=neighbors, owner=owner,
        )
    except FileNotFoundError:
        # Don't leak the absolute server path in the error body (F8a).
        log.warning("event detail: database not found at %s", db_path)
        return Response(content={"error": "database not found"}, status_code=404)
    if detail is None:
        return Response(content={"error": "event not found"}, status_code=404)
    return serialize_event_detail(detail)


@post("/api/v1/sessions/{session_id:str}/tags", status_code=200)
async def session_queue_tag_route(
    request: Request, session_id: str, db_path: Path,
) -> dict | Response:
    """Queue a pending tag against a live session.

    Request body (JSON):
      tags: list[str]              — required
      entity_type: str             — "conversation" (default), "exchange",
                                     "prompt", "response", "tool_call"
      exchange_index: int | null   — 1-based; mutually exclusive with last_marker
      last_marker: str | null      — "last_prompt" | "last_response" |
                                     "last_exchange" | "last_tool_call"

    Returns: {"queued": [...], "duplicate": [...]}.
    """
    from litestar.exceptions import PermissionDeniedException

    from siftd.api import open_database, record_audit_event
    from siftd.api.sessions import queue_tag as _queue_tag
    from siftd.serve.auth import require_write

    require_write(request)

    import json as json_mod

    try:
        body = json_mod.loads(await request.body())
    except (json_mod.JSONDecodeError, ValueError):
        return Response(content={"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return Response(content={"error": "request body must be a JSON object"}, status_code=400)

    tag_names = body.get("tags") or []
    if not tag_names or not isinstance(tag_names, list):
        return Response(content={"error": "tags must be a non-empty list"}, status_code=400)

    entity_type = str(body.get("entity_type") or "conversation")
    exchange_index_raw = body.get("exchange_index")
    last_marker = body.get("last_marker")
    last_marker = str(last_marker) if last_marker is not None else None

    try:
        exchange_index = int(exchange_index_raw) if exchange_index_raw is not None else None
    except (TypeError, ValueError):
        return Response(content={"error": "exchange_index must be an integer"}, status_code=400)

    if not db_path.exists():
        # Don't leak the absolute server path in the error body (F8a).
        log.warning("session queue tag: database not found at %s", db_path)
        return Response(content={"error": "database not found"}, status_code=404)
    conn = open_database(db_path)

    try:
        queued: list[str] = []
        duplicate: list[str] = []
        try:
            for name in tag_names:
                result = _queue_tag(
                    conn, session_id, str(name),
                    entity_type=entity_type,
                    exchange_index=exchange_index,
                    last_marker=last_marker,
                    commit=False,
                )
                if result:
                    queued.append(str(name))
                else:
                    duplicate.append(str(name))
        except ValueError as e:
            return Response(content={"error": str(e)}, status_code=400)
        except PermissionError as e:
            raise PermissionDeniedException(str(e)) from e

        conn.commit()
    finally:
        conn.close()

    record_audit_event(
        db_path=db_path,
        actor=_actor_identity(request),
        action="session.queue_tag",
        target_type="session",
        target=session_id,
        detail=",".join(str(n) for n in tag_names) or None,
        source_ip=_client_ip(request),
    )

    return {"queued": queued, "duplicate": duplicate}


@get("/api/v1/export", sync_to_thread=True)
def export_route(
    request: Request,
    db_path: Path,
    id: list[str] | None = Parameter(query="id", default=None),
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    search: str | None = Parameter(query="search", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    tag_kind: list[str] | None = Parameter(query="tag_kind", default=None),
    n: int = Parameter(query="n", default=0),
    last: int | None = Parameter(query="last", default=None),
    owner: str | None = Parameter(query="owner", default=None),
    # Format-aware path (added in Phase C of the wire-form dissolution).
    # When `format` is set, dispatch through `export_document` and return
    # the rendered ExportArtifact. When absent, retain legacy behavior
    # (return conversation list — backward compat for any external clients).
    format: str | None = Parameter(query="format", default=None),
    view: str = Parameter(query="view", default="conversations"),
    no_header: bool = Parameter(query="no_header", default=False),
    include_thinking: bool = Parameter(query="include_thinking", default=False),
    include_tool_content: bool = Parameter(query="include_tool_content", default=False),
) -> dict | Response:
    """Export full conversation data.

    Two shapes depending on the ``format`` query param:

    - ``format`` set to ``"md"`` or ``"json"``: route dispatches through
      :func:`siftd.api.export.export_document` and returns the rendered
      ``ExportArtifact`` as ``{content, media_type, filename, count}``.
      This is the path the CLI's ``siftd export`` delegation uses.
    - ``format`` absent: legacy behavior — returns
      ``{"conversations": [...]}`` of full conversation dicts.
    """
    from painted import Fidelity

    owner = _effective_owner(request, owner)

    if format is not None:
        from siftd.api.export import export_document

        if format not in ("md", "json"):
            return Response(
                content={"error": f"format must be 'md' or 'json', got {format!r}"},
                status_code=400,
            )
        visible: set[str] = {"text"}
        if include_thinking:
            visible.add("thinking")
        if include_tool_content:
            visible.add("tools")
        fidelity = Fidelity(depth=3, visible=frozenset(visible))
        return _dispatch(
            "/api/v1/export", "GET", export_document,
            {"format": format, "fidelity": fidelity, "no_header": no_header,
             "id": id, "last": last, "n": n,
             "workspace": workspace, "tag": tag, "no_tag": no_tag,
             "tag_kind": tag_kind, "since": since, "before": before,
             "search": search, "view": view, "db_path": db_path, "owner": owner},
            "export-artifact", db_path,
            fidelity=fidelity,
        )

    # Legacy path: return conversation list.
    from siftd.api.export import export_conversations

    fidelity = Fidelity(
        depth=3, visible=frozenset({"text", "thinking", "tools"}),
    )
    return _dispatch(
        "/api/v1/export", "GET", export_conversations,
        {"fidelity": fidelity, "id": id, "workspace": workspace, "since": since,
         "before": before, "tag": tag, "no_tag": no_tag, "tag_kind": tag_kind,
         "n": n, "db_path": db_path, "owner": owner},
        "export", db_path,
        fidelity=fidelity,
    )


@post("/api/v1/push")
async def push(request: Request, db_path: Path, fts_rebuild: str) -> Response | dict:
    """Receive a pushed slice and merge into team DB."""
    from siftd.serve.auth import require_write

    require_write(request)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="siftd-serve-push-", suffix=".db", delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            size_bytes = 0
            async for chunk in request.stream():
                tmp.write(chunk)
                size_bytes += len(chunk)

        if size_bytes < 16:
            return Response(content={"error": "empty or invalid slice"}, status_code=400)

        from siftd.api.receive import receive_database
        from siftd.ids import ulid

        identity = _get_push_identity(request)
        push_id = ulid()

        rebuild_fts = fts_rebuild == "on_push"
        try:
            result = receive_database(
                tmp_path, db_path,
                rebuild_fts=rebuild_fts,
                user_id=identity,
                push_id=push_id,
            )
        except Exception as exc:
            # Map deterministic, client-fixable failures to structured 4xx/409/
            # 503 with an error_type the client parses — instead of an opaque
            # 500 that hides the cause and makes e.g. a version-mismatched fleet
            # member fail every push unactionably. Mirrors the SSH receive
            # path's JSON envelope (cli/db.py cmd_db_receive). Truly unexpected
            # errors still propagate to a 500.
            import sqlite3

            from siftd.api.database import PreflightError

            if isinstance(exc, PreflightError):
                return Response(
                    content={"error": str(exc), "error_type": "preflight_failed"},
                    status_code=422,
                )
            if isinstance(exc, ValueError):
                return Response(
                    content={"error": str(exc), "error_type": "invalid_source"},
                    status_code=400,
                )
            if isinstance(exc, sqlite3.OperationalError):
                if "locked" in str(exc).lower():
                    return Response(
                        content={"error": str(exc), "error_type": "database_locked"},
                        status_code=503,
                    )
                raise
            if isinstance(exc, RuntimeError):
                # Schema-version mismatch / missing runtime schema: same-version
                # slices only. Distinguishable so the client can prompt an upgrade.
                msg = str(exc)
                et = "schema_mismatch" if "version" in msg.lower() else "merge_failed"
                return Response(content={"error": msg, "error_type": et}, status_code=409)
            raise

        # Attribution: record push in push_log
        _record_push_log(
            db_path, identity, result["conversations"], size_bytes, request,
            push_id=push_id,
        )

        # Refresh stats cache (server has DB warm from the merge)
        try:
            from siftd.api.stats import effective_db_mtime_ns, get_stats, write_stats_cache

            db_mtime = effective_db_mtime_ns(db_path)  # captured before the sweep
            write_stats_cache(get_stats(db_path=db_path), db_mtime_ns=db_mtime)
        except Exception:
            pass  # Cache refresh failure is never fatal

        status_code = 201 if result["status"] == "created" else 200
        content = {
            "status": result["status"],
            "conversations": result["conversations"],
        }
        # receive_database stamps ownership only for authenticated pushes
        # (user_id set); surface the count when it did, omit the key when it
        # didn't — clients render the suffix only when the server supplied it.
        if "owned" in result:
            content["owned"] = result["owned"]
        return Response(content=content, status_code=status_code)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


@get("/api/v1/pull", sync_to_thread=True)
def pull(
    request: Request,
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    tag_kind: list[str] | None = Parameter(query="tag_kind", default=None),
    owner: str | None = Parameter(query="owner", default=None),
    dry_run: int = Parameter(query="dry_run", default=0),
) -> Response | File:
    """Slice and stream the team DB based on filters."""
    from siftd.api.sync import SYNC_HTTP_CHUNK_SIZE

    owner = _effective_owner(request, owner)

    if dry_run:
        # Count-only path — never creates a slice file.
        from painted import Fidelity

        from siftd.api.conversations import list_conversations

        count_fidelity = Fidelity()
        convs = list_conversations(
            fidelity=count_fidelity,
            db_path=db_path,
            workspace=workspace,
            model=model,
            since=since,
            before=before,
            tag=tag,
            no_tag=no_tag,
            tag_kind=tag_kind,
            n=0,
            owner=owner,
        )
        conversations = len(convs)

        estimated_size = 0
        try:
            db_size = db_path.stat().st_size
            total_count = len(list_conversations(fidelity=count_fidelity, db_path=db_path, n=0))
            if total_count > 0:
                estimated_size = (db_size * conversations) // total_count
        except Exception:
            pass

        return Response(
            content=b"",
            status_code=200,
            media_type="application/octet-stream",
            headers={
                "X-Siftd-Conversations": str(conversations),
                "X-Siftd-Size": "0",
                "X-Siftd-Estimated-Size": str(estimated_size),
                "Content-Length": "0",
            },
        )

    from siftd.api.slice import slice_database

    tmp_dir = tempfile.mkdtemp(prefix="siftd-serve-pull-")
    try:
        slice_path = Path(tmp_dir) / "pull-slice.db"
        result = slice_database(
            source_db=db_path,
            target_path=slice_path,
            workspace=workspace,
            since=since,
            before=before,
            model=model,
            tag=tag,
            no_tag=no_tag,
            tag_kind=tag_kind,
            rebuild_fts=False,
            owner=owner,
        )

        conversations = result["conversations"]
        if conversations == 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return Response(
                content=b"",
                status_code=200,
                media_type="application/octet-stream",
                headers={
                    "X-Siftd-Conversations": "0",
                    "X-Siftd-Size": "0",
                },
            )

        size_bytes = slice_path.stat().st_size

        def _cleanup() -> None:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                log.warning("Failed to clean up pull temp dir %s", tmp_dir)

        return File(
            path=slice_path,
            chunk_size=SYNC_HTTP_CHUNK_SIZE,
            media_type="application/octet-stream",
            headers={
                "X-Siftd-Conversations": str(conversations),
                "X-Siftd-Size": str(size_bytes),
            },
            background=BackgroundTask(_cleanup),
        )
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


@get("/api/v1/sync/status", opt={"no_auth": True}, sync_to_thread=True)
def sync_status_route(db_path: Path, request_max_body_size: int) -> dict:
    """Return sync capabilities and inbox status."""
    from siftd.api.inbox import get_inbox_status
    from siftd.domain.sync import SYNC_CAPABILITIES, SYNC_PROTOCOL_VERSION

    inbox = get_inbox_status(db_path)
    # Redact potentially sensitive error strings on the public status endpoint.
    if isinstance(inbox, dict):
        last = inbox.get("last")
        if isinstance(last, dict):
            last.pop("error", None)
    return {
        "capabilities": sorted(SYNC_CAPABILITIES),
        "inbox": inbox,
        "protocol_version": SYNC_PROTOCOL_VERSION,
        "max_body_size": request_max_body_size,
    }


@get("/api/v1/conversations/{id:str}", sync_to_thread=True)
def conversation_detail(
    request: Request,
    db_path: Path,
    id: str,
    include_thinking: bool = Parameter(query="include_thinking", default=False),
    include_tool_content: bool = Parameter(query="include_tool_content", default=False),
    tool_filter: str | None = Parameter(query="tool_filter", default=None),
    anchor: str | None = Parameter(query="anchor", default=None),
    anchor_value: str | None = Parameter(query="anchor_value", default=None),
    window_start: int | None = Parameter(query="window_start", default=None),
    window_end: int | None = Parameter(query="window_end", default=None),
) -> dict | Response:
    """Get a single conversation by ID (supports prefix match).

    Anchor + window axes mirror the CLI's `query <id>` flags so the CLI can
    delegate detail reads to a remote serve. `anchor_value` is carried as a
    string on the wire (Litestar query params are scalar); the API layer
    coerces to int for ``anchor='at_turn'``.
    """
    from painted import Fidelity

    from siftd.api.conversations import get_conversation

    visible: set[str] = {"text"}
    if include_thinking:
        visible.add("thinking")
    if include_tool_content:
        visible.add("tools")
    fidelity = Fidelity(depth=3, visible=frozenset(visible))

    # Coerce anchor_value: int for at_turn, str for around, None otherwise.
    coerced_value: int | str | None = anchor_value
    if anchor == "at_turn" and anchor_value is not None:
        try:
            coerced_value = int(anchor_value)
        except ValueError:
            return Response(
                content={"error": f"anchor_value must be an integer for anchor=at_turn, got {anchor_value!r}"},
                status_code=400,
            )

    owner = _effective_owner(request, None)
    # Pass the detail template path (not the list path) so _dispatch's OpSpec
    # lookup resolves the conversations-detail spec (not_found_on_none=True).
    return _dispatch(
        "/api/v1/conversations/{id}", "GET", get_conversation,
        {"id": id, "fidelity": fidelity, "db_path": db_path,
         "tool_filter": tool_filter, "owner": owner,
         "anchor": anchor, "anchor_value": coerced_value,
         "window_start": window_start, "window_end": window_end},
        "detail", db_path,
        fidelity=fidelity,
    )


@get("/api/v1/conversations", sync_to_thread=True)
def conversation_list(
    request: Request,
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    all_tags: list[str] | None = Parameter(query="all_tags", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    tag_kind: list[str] | None = Parameter(query="tag_kind", default=None),
    tool: str | None = Parameter(query="tool", default=None),
    tool_tag: str | None = Parameter(query="tool_tag", default=None),
    search: str | None = Parameter(query="search", default=None),
    n: int = Parameter(query="n", default=20),
    oldest: bool = Parameter(query="oldest", default=False),
    owner: str | None = Parameter(query="owner", default=None),
    # Accepted but unused: the CLI's wire form expands Fidelity into these
    # axes, but list rendering doesn't currently surface narrative content
    # so the values are intentionally ignored. Declaring them on the route
    # keeps the wire-form parity contract honest (see
    # docs/guides/delegation-contract.md + tests/test_op_route_parity.py).
    include_thinking: bool = Parameter(query="include_thinking", default=False),
    include_tool_content: bool = Parameter(query="include_tool_content", default=False),
) -> dict | Response:
    """List conversations with filtering."""
    from painted import Fidelity

    from siftd.api.conversations import list_conversations

    del include_thinking, include_tool_content  # accepted-but-unused; see signature comment
    fidelity = Fidelity(depth=3)

    owner = _effective_owner(request, owner)
    return _dispatch(
        "/api/v1/conversations", "GET", list_conversations,
        {"fidelity": fidelity, "db_path": db_path, "workspace": workspace, "model": model,
         "since": since, "before": before, "search": search, "tool": tool,
         "tag": tag, "all_tags": all_tags, "no_tag": no_tag,
         "tag_kind": tag_kind, "tool_tag": tool_tag,
         "n": n, "oldest": oldest, "owner": owner},
        "list", db_path,
        fidelity=fidelity,
    )


@get("/api/v1/search", sync_to_thread=True)
def search_route(
    request: Request,
    db_path: Path,
    q: str = Parameter(query="q", default=""),
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    n: int = Parameter(query="n", default=10),
    recall: int | None = Parameter(query="recall", default=None),
    exclude_active: bool = Parameter(query="exclude_active", default=True),
    rerank: str = Parameter(query="rerank", default="mmr"),
    lambda_: float = Parameter(query="lambda", default=0.7),
    recency: bool = Parameter(query="recency", default=False),
    recency_half_life: float = Parameter(query="recency_half_life", default=30.0),
    recency_max_boost: float = Parameter(query="recency_max_boost", default=1.15),
    tag: list[str] | None = Parameter(query="tag", default=None),
    all_tags: list[str] | None = Parameter(query="all_tags", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    tag_kind: list[str] | None = Parameter(query="tag_kind", default=None),
    include_derivative: bool = Parameter(query="include_derivative", default=False),
    owner: str | None = Parameter(query="owner", default=None),
    tool: str | None = Parameter(query="tool", default=None),
    tool_tag: str | None = Parameter(query="tool_tag", default=None),
    debug_ids: bool = Parameter(query="debug_ids", default=False),
    raw_fts: bool = Parameter(query="raw_fts", default=False),
    # Engine selector: auto|fts|semantic|hybrid. 'auto' resolves to hybrid when
    # this server has embeddings, else fts (resolve_search_mode is shared with the CLI).
    mode: str = Parameter(query="mode", default="auto"),
    # Post-processing recipe controls (Slice 4): the route runs the same
    # search_view recipe the CLI does, so REST/HTML inherit the full view
    # repertoire. Defaults reproduce the prior flat chunks envelope.
    view: str = Parameter(query="view", default="chunks"),
    sort: str = Parameter(query="sort", default="score"),
    select: str = Parameter(query="select", default="all"),
    threshold: float | None = Parameter(query="threshold", default=None),
    full: bool = Parameter(query="full", default=False),
    around: str | None = Parameter(query="around", default=None),
    turns: str | None = Parameter(query="turns", default=None),
) -> dict | Response:
    """Semantic + FTS search against team DB."""
    try:
        from siftd.api.search import (
            EmbeddingsRequiredError,
            resolve_search_mode,
            search_view,
        )
    except ImportError:
        return Response(
            content={"error": "search requires siftd[embed]"},
            status_code=501,
        )

    owner = _effective_owner(request, owner)

    # Resolve the engine and report the concrete value back via the envelope.
    # embeddings_available comes through the api boundary (serve must not import
    # siftd.embeddings directly — see tests/architecture/test_imports.py).
    from siftd.api import embeddings_available
    from siftd.paths import embeddings_db_path

    has_embed = embeddings_available() and embeddings_db_path().exists()
    try:
        mode = resolve_search_mode(mode, has_embeddings=has_embed)
    except EmbeddingsRequiredError as e:
        return Response(
            content={"error": f"mode {e.mode!r} requires embeddings (siftd[embed] + a built index)"},
            status_code=400,
        )
    except ValueError as e:
        return Response(content={"error": str(e)}, status_code=400)
    try:
        return _dispatch(
            "/api/v1/search", "GET", search_view,
            {"q": q, "db_path": db_path, "n": n, "recall": recall,
             "mode": mode, "workspace": workspace,
             "model": model, "since": since, "before": before,
             "exclude_active": exclude_active,
             "rerank": rerank, "lambda_": lambda_, "recency": recency,
             "recency_half_life": recency_half_life,
             "recency_max_boost": recency_max_boost,
             "tag": tag, "all_tags": all_tags,
             "no_tag": no_tag, "tag_kind": tag_kind,
             "include_derivative": include_derivative,
             "owner": owner, "tool": tool, "tool_tag": tool_tag,
             "raw_fts": raw_fts,
             # Recipe controls — search_view runs the post-processing recipe.
             "view": view, "sort": sort, "select": select,
             "threshold": threshold, "full": full,
             "around": around, "turns": turns},
            "search", db_path,
            render_context={"debug_ids": debug_ids, "mode": mode},
        )
    except Exception:
        import logging

        logging.getLogger("siftd.serve").exception("search error")
        return Response(
            content={"error": "search failed"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Attribution helpers
# ---------------------------------------------------------------------------


def _get_push_identity(request: Request) -> str:
    """Extract user identity from auth middleware or header fallback."""
    # From auth middleware (if enabled)
    try:
        user = request.user
    except Exception:
        user = None
    if user and hasattr(user, "sub") and user.sub != "anonymous":
        return user.sub
    # Fallback: explicit header (for --no-auth setups)
    header_identity = request.headers.get("x-siftd-identity")
    if header_identity:
        return header_identity
    return "anonymous"


def _record_push_log(
    db_path: Path, identity: str, conversations: int, size_bytes: int, request: Request,
    *, push_id: str | None = None,
) -> None:
    """Record a push event in the push_log table."""
    from siftd.api import record_push_log

    record_push_log(
        db_path=db_path,
        identity=identity,
        conversations=conversations,
        size_bytes=size_bytes,
        source_ip=_client_ip(request),
        push_id=push_id,
    )
