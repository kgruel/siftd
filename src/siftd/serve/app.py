"""Litestar application factory for siftd serve."""

from __future__ import annotations

from pathlib import Path

from litestar import Litestar
from litestar.di import Provide

from siftd.serve.routes import health, pull, push


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

    return Litestar(
        route_handlers=[health, push, pull],
        dependencies={
            "db_path": Provide(provide_db_path),
            "fts_rebuild": Provide(provide_fts_rebuild),
        },
    )
