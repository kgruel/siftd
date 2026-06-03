"""Filtered database slice — export a subset of conversations into a standalone SQLite DB.

Uses ATTACH DATABASE for efficient cross-DB INSERT...SELECT without
round-tripping through Python. A temp table holds matched conversation IDs,
then each table is copied with appropriate WHERE clauses.
"""

from __future__ import annotations

from pathlib import Path

from siftd.storage.sqlite import SCHEMA_VERSION, open_database


def slice_database(
    source_db: Path,
    target_path: Path,
    *,
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
    search: str | None = None,
    rebuild_fts: bool = True,
    owner: str | None = None,
) -> dict:
    """Export filtered conversations into a standalone SQLite database.

    Args:
        source_db: Path to the source siftd database.
        target_path: Path to write the sliced database.
        workspace..search: Standard filter kwargs (same as list_conversations).
        rebuild_fts: Whether to rebuild the FTS5 index in the target.

    Returns:
        Dict with 'conversations' count and 'size_bytes'.

    Raises:
        FileNotFoundError: If source database does not exist.
    """
    from painted import Fidelity

    from siftd.api.conversations import list_conversations

    if not source_db.exists():
        raise FileNotFoundError(f"Database not found: {source_db}")

    # Verify source DB is at the current schema version. We do not auto-migrate here
    # because opening in write mode creates a backup file alongside the source, which
    # is surprising and breaks on read-only filesystems. Callers must upgrade first.
    _check_conn = open_database(source_db, read_only=True, auto_upgrade=False)
    try:
        _src_version = _check_conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        _check_conn.close()
    if _src_version < SCHEMA_VERSION:
        raise RuntimeError(
            f"Source database is at schema v{_src_version}; current schema is v{SCHEMA_VERSION}. "
            "Run 'siftd query' against the source to upgrade it first."
        )

    # Step 1: Resolve conversation IDs using existing filter infrastructure
    conversations = list_conversations(
        fidelity=Fidelity(),
        db_path=source_db,
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        search=search,
        tool=tool,
        tag=tag,
        all_tags=all_tags,
        no_tag=no_tag,
        tag_kind=tag_kind,
        tool_tag=tool_tag,
        n=0,  # unlimited
        owner=owner,
    )
    conv_ids = [c.id for c in conversations]

    # Step 2: Create target DB with full schema
    target_path.parent.mkdir(parents=True, exist_ok=True)
    from siftd.storage.sqlite import create_empty_database

    create_empty_database(target_path)

    # Step 3: Open source read-only and ATTACH target (writable) for cross-DB INSERT...SELECT.
    # ATTACH and CREATE TEMP TABLE work on read-only connections — the restriction only
    # applies to writes on the main (source) file.
    conn = open_database(source_db, read_only=True)
    try:
        conn.execute("ATTACH DATABASE ? AS slice", (str(target_path),))

        if conv_ids:
            _populate_slice(conn, conv_ids)

        conn.commit()
        conn.execute("DETACH DATABASE slice")
    finally:
        conn.close()

    # Step 4: Rebuild the derived tier (and FTS, if requested) in the target.
    # The slice copies raw rows only; usage_by_conv_model / conversation_stats
    # are derived, so without this the stats reads that GROUP BY over the rollup
    # would hit an absent table (rebuild_fts=False) or an empty one (silently
    # reporting zero tokens for real conversations).  This is the same invariant
    # ingest holds — any bulk raw-row write ends by rebuilding the derived tier.
    # Always run, even for an empty slice (conv_ids == []): create_empty_database
    # writes schema.sql only, which has no derived-tier tables, so an empty slice
    # would otherwise leave them absent and stats reads would crash instead of
    # returning zeros.  An empty rebuild just creates the empty tables and is
    # trivially fast.  Unconditional on rebuild_fts too: the rollup backs basic
    # stats even when the FTS index is skipped.
    target_conn = open_database(target_path)
    try:
        from siftd.storage.usage_rollup import rebuild_rollups

        rebuild_rollups(target_conn, commit=True)
        if rebuild_fts:
            from siftd.storage.fts import rebuild_fts_index

            rebuild_fts_index(target_conn, commit=True)
    finally:
        target_conn.close()

    size_bytes = target_path.stat().st_size
    return {"conversations": len(conv_ids), "size_bytes": size_bytes}


def _populate_slice(conn, conv_ids: list[str]) -> None:
    """Copy matched conversations and all related data into the attached 'slice' DB.

    Uses a temp table for conversation IDs to avoid the 999-parameter limit.

    Note: Tables with columns added via ALTER TABLE (conversations.branch,
    tool_calls.result_hash) have different column order in migrated source DBs
    vs fresh schema.sql targets. These use explicit column lists to avoid
    positional mismatch corruption.
    """
    # Disable FK enforcement during cross-DB copy. Re-enable + validate after.
    # INSERT OR IGNORE does NOT suppress FK violations (only UNIQUE/NOT NULL/CHECK/PK),
    # and column ordering differences between migrated source and fresh target can
    # cause values to land in wrong columns, triggering spurious FK failures.
    conn.execute("PRAGMA foreign_keys = OFF")

    # Create temp table with matched conversation IDs
    conn.execute("CREATE TEMP TABLE _slice_conv_ids (id TEXT PRIMARY KEY)")
    _batch_insert_ids(conn, conv_ids)

    # --- Vocabulary: copy only referenced entities ---

    # Workspaces referenced by matched conversations
    conn.execute("""
        INSERT OR IGNORE INTO slice.workspaces
        SELECT w.* FROM workspaces w
        WHERE w.id IN (SELECT c.workspace_id FROM conversations c
                        WHERE c.id IN (SELECT id FROM _slice_conv_ids))
    """)

    # Harnesses referenced by matched conversations
    conn.execute("""
        INSERT OR IGNORE INTO slice.harnesses
        SELECT h.* FROM harnesses h
        WHERE h.id IN (SELECT c.harness_id FROM conversations c
                        WHERE c.id IN (SELECT id FROM _slice_conv_ids))
    """)

    # Models referenced by responses in matched conversations
    conn.execute("""
        INSERT OR IGNORE INTO slice.models
        SELECT m.* FROM models m
        WHERE m.id IN (SELECT DISTINCT er.model_id FROM event_response er
                        JOIN events e ON e.id = er.event_id
                        WHERE e.conversation_id IN (SELECT id FROM _slice_conv_ids)
                        AND er.model_id IS NOT NULL)
    """)

    # Providers referenced by responses, OR named by a copied harness's source.
    # The canonical rollup cost prices NULL-provider responses through the
    # harness source (usage_rollup.py: providers.name = harnesses.source), so the
    # fallback provider — which no response.provider_id references — must travel
    # with the slice or the target reprices that cost as NULL/0.  (Harnesses are
    # copied above, so slice.harnesses is populated here.)
    conn.execute("""
        INSERT OR IGNORE INTO slice.providers
        SELECT p.* FROM providers p
        WHERE p.id IN (SELECT DISTINCT er.provider_id FROM event_response er
                        JOIN events e ON e.id = er.event_id
                        WHERE e.conversation_id IN (SELECT id FROM _slice_conv_ids)
                        AND er.provider_id IS NOT NULL)
           OR p.name IN (SELECT source FROM slice.harnesses WHERE source IS NOT NULL)
    """)

    # Tools referenced by tool_calls
    conn.execute("""
        INSERT OR IGNORE INTO slice.tools
        SELECT t.* FROM tools t
        WHERE t.id IN (SELECT DISTINCT etc.tool_id FROM event_tool_call etc
                        JOIN events e ON e.id = etc.event_id
                        WHERE e.conversation_id IN (SELECT id FROM _slice_conv_ids)
                        AND etc.tool_id IS NOT NULL)
    """)

    # Tool aliases for copied tools and harnesses
    conn.execute("""
        INSERT OR IGNORE INTO slice.tool_aliases
        SELECT ta.* FROM tool_aliases ta
        WHERE ta.tool_id IN (SELECT id FROM slice.tools)
          AND ta.harness_id IN (SELECT id FROM slice.harnesses)
    """)

    # Pricing for copied models and providers
    conn.execute("""
        INSERT OR IGNORE INTO slice.pricing
        SELECT pr.* FROM pricing pr
        WHERE pr.model_id IN (SELECT id FROM slice.models)
          AND pr.provider_id IN (SELECT id FROM slice.providers)
    """)

    # --- Core tables ---

    # Explicit columns: source DBs that went through _migrate_add_branch_column
    # have 'branch' as the last column (ALTER TABLE), but schema.sql has it at
    # position 5. SELECT * would silently swap branch/started_at/ended_at.
    conn.execute("""
        INSERT OR IGNORE INTO slice.conversations
            (id, external_id, harness_id, workspace_id, branch, started_at, ended_at)
        SELECT id, external_id, harness_id, workspace_id, branch, started_at, ended_at
        FROM conversations
        WHERE id IN (SELECT id FROM _slice_conv_ids)
    """)

    # Content blobs referenced by event_tool_call (must precede event_tool_call for FK).
    # event_tool_call is authoritative: new writes store result_hash only there.
    conn.execute("""
        INSERT OR IGNORE INTO slice.content_blobs
        SELECT cb.* FROM content_blobs cb
        WHERE cb.hash IN (
            SELECT etc.result_hash FROM event_tool_call etc
            JOIN events e ON e.id = etc.event_id
            WHERE e.conversation_id IN (SELECT id FROM _slice_conv_ids)
            AND etc.result_hash IS NOT NULL
        )
    """)

    # --- Polymorphic event tables ---

    conn.execute("""
        INSERT OR IGNORE INTO slice.events
            (id, kind, conversation_id, parent_id, external_id, timestamp)
        SELECT id, kind, conversation_id, parent_id, external_id, timestamp
        FROM events
        WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
    """)
    conn.execute("""
        INSERT OR IGNORE INTO slice.event_response
            (event_id, model_id, provider_id, input_tokens, output_tokens)
        SELECT er.event_id, er.model_id, er.provider_id, er.input_tokens, er.output_tokens
        FROM event_response er
        WHERE er.event_id IN (SELECT id FROM slice.events)
    """)
    conn.execute("""
        INSERT OR IGNORE INTO slice.event_tool_call
            (event_id, tool_id, input, result_hash, status)
        SELECT etc.event_id, etc.tool_id, etc.input, etc.result_hash, etc.status
        FROM event_tool_call etc
        WHERE etc.event_id IN (SELECT id FROM slice.events)
    """)
    conn.execute("""
        INSERT OR IGNORE INTO slice.event_content
            (id, event_id, block_index, block_type, content)
        SELECT ec.id, ec.event_id, ec.block_index, ec.block_type, ec.content
        FROM event_content ec
        WHERE ec.event_id IN (SELECT id FROM slice.events)
    """)

    # --- Attribute tables ---

    conn.execute("""
        INSERT OR IGNORE INTO slice.attributes
        SELECT a.* FROM attributes a
        WHERE a.target_id IN (SELECT id FROM slice.events)
           OR a.target_id IN (SELECT id FROM _slice_conv_ids)
    """)

    # --- Tag tables ---

    # Tags referenced by tag_assignments
    conn.execute("""
        INSERT OR IGNORE INTO slice.tags
        SELECT t.* FROM tags t
        WHERE t.id IN (
            SELECT ta.tag_id FROM tag_assignments ta
            WHERE (ta.target_kind = 'conversation' AND ta.target_id IN (SELECT id FROM _slice_conv_ids))
               OR (ta.target_kind = 'workspace' AND ta.target_id IN (SELECT id FROM slice.workspaces))
               OR (ta.target_kind IN ('prompt','response','tool_call','exchange')
                   AND ta.target_id IN (
                       SELECT id FROM events WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
                   ))
        )
    """)

    # tag_assignments (polymorphic, added slice 5)
    conn.execute("""
        INSERT OR IGNORE INTO slice.tag_assignments
        SELECT ta.*
        FROM tag_assignments ta
        WHERE (ta.target_kind = 'conversation' AND ta.target_id IN (SELECT id FROM _slice_conv_ids))
           OR (ta.target_kind = 'workspace' AND ta.target_id IN (SELECT id FROM slice.workspaces))
           OR (ta.target_kind IN ('prompt','response','tool_call','exchange')
               AND ta.target_id IN (
                   SELECT id FROM events WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
               ))
    """)

    # Skip ephemeral: ingested_files, active_sessions, pending_tags

    conn.execute("DROP TABLE _slice_conv_ids")

    # Re-enable FK enforcement and validate the slice
    conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA slice.foreign_key_check").fetchall()
    if violations:
        tables = {v[0] for v in violations}
        raise RuntimeError(
            f"Foreign key violations in sliced database (tables: {', '.join(sorted(tables))}). "
            "This may indicate a schema migration mismatch — please report this bug."
        )


def _batch_insert_ids(conn, ids: list[str], batch_size: int = 500) -> None:
    """Insert IDs into temp table in batches to avoid parameter limit."""
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        placeholders = ",".join(["(?)"] * len(batch))
        conn.execute(
            f"INSERT OR IGNORE INTO _slice_conv_ids VALUES {placeholders}",
            batch,
        )
