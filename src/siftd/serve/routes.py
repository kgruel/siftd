"""Route handlers for siftd serve."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from litestar import Request, get, post
from litestar.params import Parameter
from litestar.response import Response


def _dispatch(
    path: str, method: str, fn: Callable, params: dict[str, Any],
    render_method: str, db: Path,
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

    from siftd.api.dispatch import Operation, dispatch
    from siftd.serialization import serve_fmt

    try:
        op = Operation(
            path=path, method=method, fn=fn, params=params,
            render_method=render_method, fidelity=Fidelity(), db=db,
        )
        return dispatch(op, fmt=serve_fmt)
    except Exception as exc:
        logging.getLogger("siftd.serve").exception("dispatch error on %s %s", method, path)
        return Response(
            content={"error": f"{path} failed: {exc}"},
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
            {"method": "GET", "path": "/api/v1/tools", "description": "Tool tag usage summary"},
            {"method": "GET", "path": "/api/v1/tools/workspaces", "description": "Tool tags by workspace"},
            {"method": "GET", "path": "/api/v1/tags", "description": "List tags with counts"},
            {"method": "GET", "path": "/api/v1/tool-search", "description": "Search tool calls"},
            {"method": "GET", "path": "/api/v1/export", "description": "Export full conversations"},
            {"method": "POST", "path": "/api/v1/tag", "description": "Apply, remove, rename, or delete tags"},
        ],
    }


@get("/api/v1/health", opt={"no_auth": True})
async def health(db_path: Path) -> dict:
    """Health check — returns DB status."""
    from siftd.storage.sqlite import open_database

    db_path_str = str(db_path.resolve())
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    conversations = 0
    if db_path.exists():
        conn = open_database(db_path, read_only=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
            conversations = row[0]
        finally:
            conn.close()

    return {
        "service": "siftd",
        "status": "ok",
        "db_path": db_path_str,
        "db_size_bytes": size_bytes,
        "conversations": conversations,
    }


@get("/api/v1/stats")
async def stats_route(db_path: Path) -> dict:
    """Return database statistics. Server has DB warm, so this is fast."""
    from siftd.api.stats import get_stats

    return _dispatch("/api/v1/stats", "GET", get_stats, {"db_path": db_path}, "stats", db_path)


@get("/api/v1/workspaces")
async def workspaces_route(
    db_path: Path,
    n: int = Parameter(query="n", default=10000),
) -> dict:
    """List workspaces with conversation counts."""
    from siftd.api.stats import list_workspaces

    return _dispatch("/api/v1/workspaces", "GET", list_workspaces, {"db_path": db_path, "n": n}, "workspaces", db_path)


@get("/api/v1/tools")
async def tools_route(
    db_path: Path,
    prefix: str = Parameter(query="prefix", default="shell:"),
) -> dict:
    """Tool tag usage summary."""
    from siftd.api.tools import get_tool_tag_summary

    return _dispatch(
        "/api/v1/tools", "GET", get_tool_tag_summary,
        {"db_path": db_path, "prefix": prefix}, "tools", db_path,
    )


@get("/api/v1/tools/workspaces")
async def tools_by_workspace_route(
    db_path: Path,
    prefix: str = Parameter(query="prefix", default="shell:"),
    n: int = Parameter(query="n", default=20),
) -> dict:
    """Tool tag usage broken down by workspace."""
    from siftd.api.tools import get_tool_tags_by_workspace

    return _dispatch(
        "/api/v1/tools/workspaces", "GET", get_tool_tags_by_workspace,
        {"db_path": db_path, "prefix": prefix, "n": n}, "tools_by_workspace", db_path,
    )


@get("/api/v1/tags")
async def tags_route(
    db_path: Path,
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
) -> dict:
    """List tags with usage counts."""
    from siftd.api.tags import list_tags

    return _dispatch("/api/v1/tags", "GET", list_tags, {"db_path": db_path, "since": since, "before": before}, "tags", db_path)


@post("/api/v1/tag")
async def tag_write_route(request: Request, db_path: Path) -> dict:
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
    from siftd.serve.auth import require_write

    require_write(request)

    import json as json_mod

    from siftd.api.conversations import (
        get_recent_conversation_ids,
        resolve_entity_id,
    )
    from siftd.api.tags import (
        apply_tag,
        delete_tag,
        get_or_create_tag,
        get_tag_id,
        remove_tag,
        rename_tag,
    )
    from siftd.storage.sqlite import open_database

    body = json_mod.loads(await request.body())
    action = body.get("action", "apply")
    conn = open_database(db_path)

    try:
        if action == "rename":
            old_name = body["old_name"]
            new_name = body["new_name"]
            rename_tag(old_name, new_name, conn=conn, commit=True)
            return {"status": "renamed", "old_name": old_name, "new_name": new_name}

        if action == "delete":
            tag_name = body["tag_name"]
            delete_tag(conn, tag_name, commit=True)
            return {"status": "deleted", "tag_name": tag_name}

        # apply or remove
        tags = body.get("tags", [])
        entity_type = body.get("entity_type", "conversation")
        entity_id = body.get("entity_id")
        last_n = body.get("last")

        if last_n:
            ids = get_recent_conversation_ids(conn, int(last_n))
        elif entity_id:
            resolved = resolve_entity_id(conn, entity_type, entity_id)
            ids = [resolved] if resolved else []
        else:
            return {"error": "entity_id or last required"}

        if not ids:
            return {"error": "no matching entities found"}

        results = []
        for tag_name in tags:
            if action == "remove":
                tag_id = get_tag_id(conn, tag_name)
                if not tag_id:
                    results.append({"tag": tag_name, "status": "not_found"})
                    continue
                count = sum(1 for eid in ids if remove_tag(conn, entity_type, eid, tag_id, commit=False))
                results.append({"tag": tag_name, "status": "removed", "count": count})
            else:
                tag_id = get_or_create_tag(conn, tag_name)
                count = sum(1 for eid in ids if apply_tag(conn, entity_type, eid, tag_id, commit=False))
                results.append({"tag": tag_name, "status": "applied", "count": count})

        conn.commit()

        # Refresh stats cache
        try:
            from siftd.api.stats import get_stats, write_stats_cache

            write_stats_cache(get_stats(db_path=db_path))
        except Exception:
            pass

        return {"action": action, "results": results}
    finally:
        conn.close()


@get("/api/v1/tool-search")
async def tool_search_route(
    db_path: Path,
    q: str = Parameter(query="q"),
    workspace: str | None = Parameter(query="workspace", default=None),
    model: str | None = Parameter(query="model", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    tool: str | None = Parameter(query="tool", default=None),
    tool_tag: str | None = Parameter(query="tool_tag", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    all_tags: list[str] | None = Parameter(query="all_tags", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    n: int = Parameter(query="n", default=20),
    owner: str | None = Parameter(query="owner", default=None),
) -> dict:
    """Search tool calls via FTS."""
    from siftd.api.tool_search import search_tool_calls

    return _dispatch(
        "/api/v1/tool-search", "GET", search_tool_calls,
        {"q": q, "db_path": db_path, "n": n, "workspace": workspace, "model": model,
         "since": since, "before": before, "tag": tag, "all_tags": all_tags,
         "no_tag": no_tag, "tool": tool, "tool_tag": tool_tag, "owner": owner},
        "tool_search", db_path,
    )


@get("/api/v1/export")
async def export_route(
    db_path: Path,
    id: list[str] | None = Parameter(query="id", default=None),
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    n: int = Parameter(query="n", default=0),
    owner: str | None = Parameter(query="owner", default=None),
) -> dict:
    """Export full conversation data."""
    from siftd.api.export import export_conversations

    return _dispatch(
        "/api/v1/export", "GET", export_conversations,
        {"id": id, "workspace": workspace, "since": since, "before": before,
         "tag": tag, "no_tag": no_tag, "n": n, "db_path": db_path,
         "owner": owner},
        "export", db_path,
    )


@post("/api/v1/push")
async def push(request: Request, db_path: Path, fts_rebuild: str) -> Response | dict:
    """Receive a pushed slice and merge into team DB."""
    from siftd.serve.auth import require_write

    require_write(request)

    body = await request.body()
    if len(body) < 16:
        return Response(content={"error": "empty or invalid slice"}, status_code=400)

    with tempfile.NamedTemporaryFile(
        prefix="siftd-serve-push-", suffix=".db", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        tmp_path.write_bytes(body)
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
            db_path, identity, result["conversations"], len(body), request,
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
        if tmp_path.exists():
            tmp_path.unlink()


@get("/api/v1/pull")
async def pull(
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    owner: str | None = Parameter(query="owner", default=None),
) -> Response:
    """Slice and stream the team DB based on filters."""
    from siftd.api.slice import slice_database

    with tempfile.TemporaryDirectory(prefix="siftd-serve-pull-") as tmp_dir:
        slice_path = Path(tmp_dir) / "pull-slice.db"
        result = slice_database(
            source_db=db_path,
            target_path=slice_path,
            workspace=workspace,
            since=since,
            before=before,
            model=model,
            tag=tag,
            rebuild_fts=False,
            owner=owner,
        )

        conversations = result["conversations"]
        if conversations == 0:
            return Response(
                content=b"",
                status_code=200,
                media_type="application/octet-stream",
                headers={
                    "X-Siftd-Conversations": "0",
                    "X-Siftd-Size": "0",
                },
            )

        data = slice_path.read_bytes()
        return Response(
            content=data,
            status_code=200,
            media_type="application/octet-stream",
            headers={
                "X-Siftd-Conversations": str(conversations),
                "X-Siftd-Size": str(len(data)),
            },
        )


@get("/api/v1/conversations/{id:str}")
async def conversation_detail(
    db_path: Path,
    id: str,
    include_thinking: bool = Parameter(query="include_thinking", default=False),
    include_tool_content: bool = Parameter(query="include_tool_content", default=False),
    tool_filter: str | None = Parameter(query="tool_filter", default=None),
) -> dict:
    """Get a single conversation by ID (supports prefix match)."""
    from siftd.api.conversations import get_conversation

    return _dispatch(
        "/api/v1/conversations", "GET", get_conversation,
        {"id": id, "db_path": db_path, "include_thinking": include_thinking,
         "include_tool_content": include_tool_content, "tool_filter": tool_filter},
        "detail", db_path,
    )


@get("/api/v1/conversations")
async def conversation_list(
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
    all_tags: list[str] | None = Parameter(query="all_tags", default=None),
    no_tag: list[str] | None = Parameter(query="no_tag", default=None),
    tool: str | None = Parameter(query="tool", default=None),
    tool_tag: str | None = Parameter(query="tool_tag", default=None),
    search: str | None = Parameter(query="search", default=None),
    n: int = Parameter(query="n", default=20),
    oldest: bool = Parameter(query="oldest", default=False),
    owner: str | None = Parameter(query="owner", default=None),
) -> dict:
    """List conversations with filtering."""
    from siftd.api.conversations import list_conversations

    return _dispatch(
        "/api/v1/conversations", "GET", list_conversations,
        {"db_path": db_path, "workspace": workspace, "model": model,
         "since": since, "before": before, "search": search, "tool": tool,
         "tag": tag, "all_tags": all_tags, "no_tag": no_tag,
         "tool_tag": tool_tag, "n": n, "oldest": oldest, "owner": owner},
        "list", db_path,
    )


@get("/api/v1/search")
async def search_route(
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
    include_derivative: bool = Parameter(query="include_derivative", default=False),
    owner: str | None = Parameter(query="owner", default=None),
) -> dict | Response:
    """Semantic + FTS search against team DB."""
    try:
        from siftd.api.search import hybrid_search
    except ImportError:
        return Response(
            content={"error": "search requires siftd[embed]"},
            status_code=501,
        )

    mode = "semantic" if embeddings_only else "hybrid"
    try:
        return _dispatch(
            "/api/v1/search", "GET", hybrid_search,
            {"q": q, "db_path": db_path, "n": n, "recall": recall,
             "mode": mode, "workspace": workspace,
             "model": model, "since": since, "before": before,
             "backend": backend, "exclude_active": exclude_active,
             "rerank": rerank, "lambda_": lambda_, "recency": recency,
             "recency_half_life": recency_half_life,
             "recency_max_boost": recency_max_boost,
             "threshold": threshold, "tag": tag, "all_tags": all_tags,
             "no_tag": no_tag, "include_derivative": include_derivative,
             "owner": owner},
            "search", db_path,
        )
    except Exception as e:
        return Response(
            content={"error": f"search failed: {e}"},
            status_code=501,
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
    from datetime import UTC, datetime

    from siftd.ids import ulid
    from siftd.storage.sqlite import ensure_push_log_table, open_database

    conn = open_database(db_path)
    try:
        ensure_push_log_table(conn)
        conn.execute(
            "INSERT INTO push_log (push_id, user_identity, pushed_at, conversations, size_bytes, source_ip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                push_id or ulid(),
                identity,
                datetime.now(UTC).isoformat(),
                conversations,
                size_bytes,
                request.client.host if request.client else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()
