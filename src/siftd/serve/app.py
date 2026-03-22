"""Litestar application factory for siftd serve."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar.di import Provide

try:
    from siftd.serve.html_routes import ui_query, ui_search, ui_shell

    _HTML_ROUTES = [ui_shell, ui_query, ui_search]
except ImportError:
    _HTML_ROUTES = []

from siftd.serve.routes import (
    conversation_detail,
    conversation_list,
    export_route,
    health,
    index,
    pull,
    push,
    search_route,
    stats_route,
    tag_write_route,
    tags_route,
    tool_search_route,
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

    return Litestar(
        route_handlers=[
            index, health, stats_route, workspaces_route, tools_route, tools_by_workspace_route,
            tag_write_route, tags_route, tool_search_route, export_route,
            push, pull, conversation_detail, conversation_list, search_route,
            *_HTML_ROUTES,
        ],
        dependencies={
            "db_path": Provide(provide_db_path),
            "fts_rebuild": Provide(provide_fts_rebuild),
        },
        middleware=middleware,
    )
