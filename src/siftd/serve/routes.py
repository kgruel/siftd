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


def _dispatch(
    path: str, method: str, fn: Callable, params: dict[str, Any],
    render_method: str, db: Path,
    render_context: dict | None = None,
    *,
    fidelity: Any | None = None,
) -> Any:
    """Build an Operation and dispatch it through the format protocol.

    Shared helper for simple routes: extract params → execute → render.
    Uses serve_fmt (serialization layer) instead of output/json_fmt
    to respect the architecture boundary (serve cannot import output).

    Catches exceptions and returns a structured JSON error response
    instead of letting raw tracebacks propagate as 500 errors.
    """
    import logging

    from painted import Fidelity

    from siftd.api.conversations import QueryError
    from siftd.api.dispatch import Operation, execute, render
    from siftd.serialization import serve_fmt

    try:
        op = Operation(
            path=path, method=method, fn=fn, params=params,
            render_method=render_method, fidelity=fidelity or Fidelity(), db=db,
            render_context=render_context or {},
        )
        result = execute(op)
        if render_method == "detail" and result is None:
            return Response(content={"error": "conversation not found"}, status_code=404)
        return render(result, op, fmt=serve_fmt)
    except ImportError as e:
        if "siftd.embeddings" in str(e) or "fastembed" in str(e):
            return Response(content={"error": str(e)}, status_code=501)
        logging.getLogger("siftd.serve").exception("dispatch import error on %s %s", method, path)
        return Response(content={"error": f"{path} failed"}, status_code=500)
    except FileNotFoundError as e:
        return Response(content={"error": str(e)}, status_code=404)
    except (ValueError, KeyError, QueryError) as e:
        return Response(content={"error": str(e)}, status_code=400)
    except Exception as e:
        if e.__class__.__name__ == "EmbeddingsNotAvailable":
            return Response(content={"error": str(e)}, status_code=501)

        logging.getLogger("siftd.serve").exception("dispatch error on %s %s", method, path)
        return Response(
            content={"error": f"{path} failed"},
            status_code=500,
        )


@get("/api/v1")
async def index() -> dict:
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
            {"method": "GET", "path": "/api/v1/tags", "description": "List tags with counts"},
            {"method": "GET", "path": "/api/v1/export", "description": "Export full conversations"},
            {"method": "POST", "path": "/api/v1/tag", "description": "Apply, remove, rename, or delete tags"},
            {"method": "POST", "path": "/api/v1/sessions/{id}/tags", "description": "Queue a pending tag for a live session"},
            {"method": "GET", "path": "/api/v1/events/{id}", "description": "Get a single event by ID"},
        ],
    }


@get("/api/v1/health", opt={"no_auth": True})
async def health(db_path: Path) -> dict:
    """Health check — returns DB status."""
    from siftd.api import get_health_status
    from siftd.serialization import serialize_health_status

    return serialize_health_status(get_health_status(db_path))


@get("/api/v1/stats")
async def stats_route(request: Request, db_path: Path) -> dict | Response:
    """Return database statistics. Server has DB warm, so this is fast."""
    from siftd.api.stats import get_stats

    owner = _effective_owner(request, None)
    return _dispatch("/api/v1/stats", "GET", get_stats, {"db_path": db_path, "owner": owner}, "stats", db_path)


@get("/api/v1/workspaces")
async def workspaces_route(
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


@get("/api/v1/tags")
async def tags_route(
    request: Request,
    db_path: Path,
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
) -> dict | Response:
    """List tags with usage counts."""
    from siftd.api.tags import list_tags

    owner = _effective_owner(request, None)
    return _dispatch(
        "/api/v1/tags", "GET", list_tags,
        {"db_path": db_path, "since": since, "before": before, "owner": owner},
        "tags", db_path,
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
    try:
        if action == "rename":
            result = rename_tag_safe(
                db_path=db_path,
                old_name=str(body.get("old_name") or ""),
                new_name=str(body.get("new_name") or ""),
                owner=owner,
            )
            payload = serialize_rename_result(result)
        elif action == "delete":
            result = delete_tag_safe(
                db_path=db_path,
                tag_name=str(body.get("tag_name") or ""),
                owner=owner,
            )
            payload = serialize_delete_result(result)
        else:
            result = apply_tags(
                db_path=db_path,
                tags=[str(t) for t in body.get("tags", [])],
                entity_type=str(body.get("entity_type", "conversation")),
                entity_id=body.get("entity_id"),
                last=body.get("last"),
                owner=owner,
                remove=action == "remove",
            )
            payload = serialize_apply_result(result)
    except PermissionError as e:
        raise PermissionDeniedException(str(e)) from e
    except ValueError as e:
        return Response(content={"error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return Response(content={"error": str(e)}, status_code=404)

    # Refresh stats cache
    try:
        from siftd.api.stats import get_stats, write_stats_cache

        write_stats_cache(get_stats(db_path=db_path))
    except Exception:
        pass
    return payload


@get("/api/v1/events/{event_id:str}")
async def event_detail_route(
    request: Request, event_id: str, db_path: Path,
    neighbors: bool = Parameter(query="neighbors", default=False),
) -> dict | Response:
    """Return a single event by ID."""
    from siftd.api.events import get_event
    from siftd.serialization.events import serialize_event_detail

    del request  # unused; auth middleware enforces read access

    try:
        detail = get_event(
            event_id, db_path=db_path, include_neighbors=neighbors,
        )
    except FileNotFoundError as e:
        return Response(content={"error": str(e)}, status_code=404)
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

    from siftd.api import open_database
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
        return Response(
            content={"error": f"Database not found: {db_path}"},
            status_code=404,
        )
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

    return {"queued": queued, "duplicate": duplicate}


@get("/api/v1/export")
async def export_route(
    request: Request,
    db_path: Path,
    id: list[str] | None = Parameter(query="id", default=None),
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    tag_kind: list[str] | None = Parameter(query="tag_kind", default=None),
    n: int = Parameter(query="n", default=0),
    owner: str | None = Parameter(query="owner", default=None),
) -> dict | Response:
    """Export full conversation data."""
    from painted import Fidelity

    from siftd.api.export import export_conversations

    fidelity = Fidelity(
        depth=3, visible=frozenset({"text", "thinking", "tools"}),
    )

    owner = _effective_owner(request, owner)
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
        result = receive_database(
            tmp_path, db_path,
            rebuild_fts=rebuild_fts,
            user_id=identity,
            push_id=push_id,
        )

        # Attribution: record push in push_log
        _record_push_log(
            db_path, identity, result["conversations"], size_bytes, request,
            push_id=push_id,
        )

        # Refresh stats cache (server has DB warm from the merge)
        try:
            from siftd.api.stats import get_stats, write_stats_cache

            write_stats_cache(get_stats(db_path=db_path))
        except Exception:
            pass  # Cache refresh failure is never fatal

        status_code = 201 if result["status"] == "created" else 200
        return Response(
            content={
                "status": result["status"],
                "conversations": result["conversations"],
            },
            status_code=status_code,
        )
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


@get("/api/v1/pull")
async def pull(
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


@get("/api/v1/sync/status", opt={"no_auth": True})
async def sync_status_route(db_path: Path) -> dict:
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
    }


@get("/api/v1/conversations/{id:str}")
async def conversation_detail(
    request: Request,
    db_path: Path,
    id: str,
    include_thinking: bool = Parameter(query="include_thinking", default=False),
    include_tool_content: bool = Parameter(query="include_tool_content", default=False),
    tool_filter: str | None = Parameter(query="tool_filter", default=None),
) -> dict | Response:
    """Get a single conversation by ID (supports prefix match)."""
    from painted import Fidelity

    from siftd.api.conversations import get_conversation

    visible: set[str] = {"text"}
    if include_thinking:
        visible.add("thinking")
    if include_tool_content:
        visible.add("tools")
    fidelity = Fidelity(depth=3, visible=frozenset(visible))

    owner = _effective_owner(request, None)
    return _dispatch(
        "/api/v1/conversations", "GET", get_conversation,
        {"id": id, "fidelity": fidelity, "db_path": db_path,
         "tool_filter": tool_filter, "owner": owner},
        "detail", db_path,
        fidelity=fidelity,
    )


@get("/api/v1/conversations")
async def conversation_list(
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
) -> dict | Response:
    """List conversations with filtering."""
    from painted import Fidelity

    from siftd.api.conversations import list_conversations

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


@get("/api/v1/search")
async def search_route(
    request: Request,
    db_path: Path,
    q: str = Parameter(query="q"),
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    threshold: float = Parameter(query="threshold", default=0.0),
    n: int = Parameter(query="n", default=10),
    recall: int = Parameter(query="recall", default=80),
    embeddings_only: bool = Parameter(query="embeddings_only", default=True),
    exclude_active: bool = Parameter(query="exclude_active", default=True),
    rerank: str = Parameter(query="rerank", default="mmr"),
    lambda_: float = Parameter(query="lambda", default=0.7),
    recency: bool = Parameter(query="recency", default=False),
    recency_half_life: float = Parameter(query="recency_half_life", default=30.0),
    recency_max_boost: float = Parameter(query="recency_max_boost", default=1.15),
    backend: str | None = Parameter(query="backend", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    all_tags: list[str] | None = Parameter(query="all_tags", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    tag_kind: list[str] | None = Parameter(query="tag_kind", default=None),
    include_derivative: bool = Parameter(query="include_derivative", default=False),
    owner: str | None = Parameter(query="owner", default=None),
    debug_ids: bool = Parameter(query="debug_ids", default=False),
    raw_fts: bool = Parameter(query="raw_fts", default=False),
) -> dict | Response:
    """Semantic + FTS search against team DB."""
    try:
        from siftd.api.search import search_chunks
    except ImportError:
        return Response(
            content={"error": "search requires siftd[embed]"},
            status_code=501,
        )

    owner = _effective_owner(request, owner)
    mode = "semantic" if embeddings_only else "hybrid"
    try:
        return _dispatch(
            "/api/v1/search", "GET", search_chunks,
            {"q": q, "db_path": db_path, "n": n, "recall": recall,
             "mode": mode, "workspace": workspace,
             "model": model, "since": since, "before": before,
             "backend": backend, "exclude_active": exclude_active,
             "rerank": rerank, "lambda_": lambda_, "recency": recency,
             "recency_half_life": recency_half_life,
             "recency_max_boost": recency_max_boost,
             "threshold": threshold, "tag": tag, "all_tags": all_tags,
             "no_tag": no_tag, "tag_kind": tag_kind,
             "include_derivative": include_derivative,
             "owner": owner, "raw_fts": raw_fts},
            "search", db_path,
            render_context={"debug_ids": debug_ids},
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
        source_ip=request.client.host if request.client else None,
        push_id=push_id,
    )
