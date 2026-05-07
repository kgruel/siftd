"""Litestar application factory for siftd serve."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar.di import Provide
from litestar.static_files import create_static_files_router

from siftd.serve.html_routes import (
    ui_export,
    ui_follow,
    ui_meta,
    ui_peek,
    ui_query,
    ui_search,
    ui_shell,
    ui_stats,
    ui_tag,
    ui_tags_suggest,
)
from siftd.serve.routes import (
    conversation_detail,
    conversation_list,
    event_detail_route,
    export_route,
    health,
    index,
    pull,
    push,
    search_route,
    session_queue_tag_route,
    stats_route,
    sync_status_route,
    tag_write_route,
    tags_route,
    tools_by_workspace_route,
    tools_route,
    workspaces_route,
)


def create_app(
    *,
    db_path: Path,
    auth_config: dict | None = None,
    fts_rebuild: str = "on_push",
) -> Litestar:
    """Create the Litestar application.

    Args:
        db_path: Path to the team SQLite database.
        auth_config: Auth config dict (None = no auth).
        fts_rebuild: FTS rebuild strategy ("on_push", "scheduled", "off").
    """

    async def provide_db_path() -> Path:
        return db_path

    async def provide_fts_rebuild() -> str:
        return fts_rebuild

    middleware: list[Any] = []
    if auth_config:
        from siftd.serve.auth import create_auth_middleware

        middleware.append(create_auth_middleware(auth_config))

    static_dir = Path(__file__).parent / "static"
    static_router = create_static_files_router(path="/static", directories=[static_dir])

    return Litestar(
        route_handlers=[
            index, health, stats_route, workspaces_route, tools_route, tools_by_workspace_route,
            tag_write_route, session_queue_tag_route, tags_route,
            export_route,
            push, pull, sync_status_route, conversation_detail, conversation_list,
            event_detail_route, search_route,
            ui_shell, ui_meta, ui_query, ui_search, ui_peek, ui_follow, ui_stats,
            ui_tag, ui_tags_suggest, ui_export,
            static_router,
        ],
        dependencies={
            "db_path": Provide(provide_db_path),
            "fts_rebuild": Provide(provide_fts_rebuild),
        },
        middleware=middleware,
    )
