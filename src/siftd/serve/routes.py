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
        ],
    }


@get("/v1/health", opt={"no_auth": True})
async def health(db_path: Path) -> dict:
    """Health check — returns DB status."""
    from siftd.storage.sqlite import open_database

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
        "status": "ok",
        "db_size_bytes": size_bytes,
        "conversations": conversations,
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
        try:
            result = receive_database(tmp_path, db_path, rebuild_fts=rebuild_fts)
        except ValueError as e:
            return Response(content={"error": str(e)}, status_code=400)

        # Attribution: record push in push_log
        identity = _get_push_identity(request)
        _record_push_log(db_path, identity, result["conversations"], len(body), request)

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
    if not db_path.exists():
        return Response(
            content=b"",
            status_code=200,
            media_type="application/octet-stream",
            headers={
                "X-Siftd-Conversations": "0",
                "X-Siftd-Size": "0",
            },
        )

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
    search: str | None = Parameter(query="search", default=None),
    n: int = Parameter(query="n", default=20),
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
        tags=tag,
        limit=n,
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
            q, db_path=db_path, limit=n,
            workspace=workspace, model=model,
            since=since, before=before,
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
