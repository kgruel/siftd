"""Route handlers for siftd serve."""

from __future__ import annotations

import tempfile
from pathlib import Path

from litestar import Request, get, post
from litestar.response import Response


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
        result = receive_database(tmp_path, db_path, rebuild_fts=rebuild_fts)
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
