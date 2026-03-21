"""Route handlers for siftd serve."""

from __future__ import annotations

import tempfile
from pathlib import Path

from litestar import Request, get, post
from litestar.params import Parameter
from litestar.response import Response


@get("/", opt={"no_auth": True})
async def index() -> dict:
    """Root — list available API endpoints."""
    return {
        "service": "siftd",
        "endpoints": [
            {"method": "GET", "path": "/v1/health", "description": "Health check and DB status"},
            {"method": "POST", "path": "/v1/push", "description": "Push a database slice"},
            {"method": "GET", "path": "/v1/pull", "description": "Pull a filtered database slice"},
            {"method": "GET", "path": "/v1/query", "description": "List or detail conversations"},
            {"method": "GET", "path": "/v1/search", "description": "Semantic + FTS search"},
            {"method": "GET", "path": "/v1/stats", "description": "Database statistics"},
            {"method": "GET", "path": "/v1/workspaces", "description": "List workspaces"},
        ],
    }


@get("/v1/health", opt={"no_auth": True})
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


@get("/v1/stats")
async def stats_route(db_path: Path) -> dict:
    """Return database statistics. Server has DB warm, so this is fast."""
    from siftd.api.stats import _stats_to_dict, get_stats

    stats = get_stats(db_path=db_path)
    return _stats_to_dict(stats)


@get("/v1/workspaces")
async def workspaces_route(
    db_path: Path,
    n: int = Parameter(query="n", default=10000),
) -> dict:
    """List workspaces with conversation counts."""
    from siftd.api.stats import list_workspaces
    from siftd.storage.sqlite import open_database

    conn = open_database(db_path, read_only=True)
    try:
        rows = list_workspaces(conn, limit=n)
    finally:
        conn.close()
    return {
        "workspaces": [
            {"path": r["path"], "conversations": r["convs"], "last_activity": r["last_activity"]}
            for r in rows
        ]
    }


@post("/v1/push")
async def push(request: Request, db_path: Path, fts_rebuild: str) -> Response | dict:
    """Receive a pushed slice and merge into team DB."""
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

        rebuild_fts = fts_rebuild == "on_push"
        result = receive_database(tmp_path, db_path, rebuild_fts=rebuild_fts)

        # Attribution: record push in push_log
        identity = _get_push_identity(request)
        _record_push_log(db_path, identity, result["conversations"], len(body), request)

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


@get("/v1/pull")
async def pull(
    db_path: Path,
    workspace: str | None = Parameter(query="workspace", default=None),
    since: str | None = Parameter(query="since", default=None),
    before: str | None = Parameter(query="before", default=None),
    model: str | None = Parameter(query="model", default=None),
    tag: list[str] | None = Parameter(query="tag", default=None),
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
            tags=tag,
            rebuild_fts=False,
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


@get("/v1/query")
async def query(
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
    id: str | None = Parameter(query="id", default=None),
) -> dict:
    """List or detail conversations."""
    import dataclasses

    from siftd.api.conversations import get_conversation, list_conversations

    if id is not None:
        detail = get_conversation(id, db_path=db_path)
        if detail is None:
            return {"error": f"conversation not found: {id}"}
        d = dataclasses.asdict(detail)
        d.pop("exchanges", None)  # property, not serializable
        return {"conversation": d}

    rows = list_conversations(
        db_path=db_path,
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        search=search,
        tool=tool,
        tags=tag,
        all_tags=all_tags,
        exclude_tags=no_tag,
        tool_tag=tool_tag,
        limit=n,
        oldest_first=oldest,
    )
    return {"conversations": [dataclasses.asdict(r) for r in rows]}


@get("/v1/search")
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
) -> dict | Response:
    """Semantic + FTS search against team DB."""
    try:
        from siftd.search import hybrid_search
    except ImportError:
        return Response(
            content={"error": "search requires siftd[embed]"},
            status_code=501,
        )

    try:
        results = hybrid_search(
            q,
            db_path=db_path,
            limit=n,
            recall=recall,
            embeddings_only=embeddings_only,
            workspace=workspace,
            model=model,
            since=since,
            before=before,
            backend=backend,
            exclude_active=exclude_active,
            rerank=rerank,
            lambda_=lambda_,
            recency=recency,
            recency_half_life=recency_half_life,
            recency_max_boost=recency_max_boost,
            tags=tag,
            all_tags=all_tags,
            exclude_tags=no_tag,
            include_derivative=include_derivative,
        )
    except Exception as e:
        return Response(
            content={"error": f"search failed: {e}"},
            status_code=501,
        )

    if threshold > 0:
        results = [r for r in results if r.score >= threshold]

    import dataclasses

    serialized = [dataclasses.asdict(r) for r in results]
    return {
        "query": q,
        "result_count": len(serialized),
        "results": serialized,
    }


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
                ulid(),
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
