"""Remote first-egress disclosure — text + shown-once persistence.

Lives in ``embeddings`` (not ``api``) because it is a fact about embeddings-database
state, not an API-layer concern — the doctor module (which cannot depend on ``api``)
needs to read the same pending-notice state that the ingest auto-index hook and
``siftd embed`` surface.
"""

from __future__ import annotations

from pathlib import Path

# index_meta key recording that the remote first-egress notice has been shown once.
_EGRESS_NOTICE_META_KEY = "auto_index_egress_notified"


def pending_egress_notice(configured_backend: str | None, embed_db: Path) -> str | None:
    """Text for the one-time remote first-egress disclosure, or None if N/A or already shown.

    Read-only: it does NOT persist the shown flag — the caller persists via
    :func:`mark_egress_notified` only once the notice has actually been surfaced, so a
    disclosure is never burned unshown. Local backends (fastembed) send nothing off-machine.
    A missing embeddings DB counts as pending: the explicit first build is often the very
    first egress.
    """
    if not configured_backend or not configured_backend.startswith("remote:"):
        return None

    if embed_db.exists():
        from siftd.storage.embeddings import get_meta, open_embeddings_db

        conn = open_embeddings_db(embed_db, read_only=True)
        try:
            if get_meta(conn, _EGRESS_NOTICE_META_KEY):
                return None
        finally:
            conn.close()

    from siftd.storage.embeddings import config_backend_name

    provider = config_backend_name(configured_backend)
    return (
        f"semantic indexing sends conversation content to {provider}; auto-indexing on "
        "ingest can be disabled with 'siftd config set embed.auto_index false'"
    )


def mark_egress_notified(embed_db: Path) -> None:
    """Persist the remote first-egress shown-once flag (called only after the notice fired)."""
    if not embed_db.exists():
        return
    from siftd.storage.embeddings import open_embeddings_db, set_meta

    conn = open_embeddings_db(embed_db)
    try:
        set_meta(conn, _EGRESS_NOTICE_META_KEY, "1")
    finally:
        conn.close()


def egress_notice_pending(embed_db_path: Path | None = None) -> str | None:
    """The one-time remote first-egress disclosure if it hasn't been shown yet, else None.

    Public entry point for surfaces that trigger embedding egress directly — the explicit
    ``siftd embed`` build is often the FIRST egress (initial backlog), so it must disclose
    too. Read-only; pair with :func:`mark_egress_notified_default` once the notice has
    actually been surfaced.
    """
    from siftd.embeddings.availability import embedding_status
    from siftd.paths import embeddings_db_path

    st = embedding_status()
    if not st.usable:
        return None
    return pending_egress_notice(st.backend, embed_db_path or embeddings_db_path())


def mark_egress_notified_default(embed_db_path: Path | None = None) -> None:
    """Persist the first-egress shown-once flag (call only after the notice was surfaced)."""
    from siftd.paths import embeddings_db_path

    mark_egress_notified(embed_db_path or embeddings_db_path())
