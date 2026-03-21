"""Filtered database slice — export a subset of conversations into a standalone SQLite DB.

Uses ATTACH DATABASE for efficient cross-DB INSERT...SELECT without
round-tripping through Python. A temp table holds matched conversation IDs,
then each table is copied with appropriate WHERE clauses.
"""

from __future__ import annotations

from pathlib import Path

from siftd.storage.sqlite import open_database


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
    tool: str | None = None,
    tool_tag: str | None = None,
    search: str | None = None,
    rebuild_fts: bool = True,
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
    from siftd.api.conversations import list_conversations

    if not source_db.exists():
        raise FileNotFoundError(f"Database not found: {source_db}")

    # Step 1: Resolve conversation IDs using existing filter infrastructure
    conversations = list_conversations(
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
        tool_tag=tool_tag,
        n=0,  # unlimited
    )
    conv_ids = [c.id for c in conversations]

    # Step 2: Create target DB with full schema
    target_path.parent.mkdir(parents=True, exist_ok=True)
    from siftd.storage.sqlite import create_empty_database

    create_empty_database(target_path)

    # Step 3: Open source and ATTACH target, then copy.
    # Not read_only because ATTACH/DETACH and CREATE TEMP TABLE require write capability.
    conn = open_database(source_db)
    try:
        conn.execute("ATTACH DATABASE ? AS slice", (str(target_path),))

        if conv_ids:
            _populate_slice(conn, conv_ids)

        conn.commit()
        conn.execute("DETACH DATABASE slice")
    finally:
        conn.close()

    # Step 4: Rebuild FTS in target if requested
    if rebuild_fts and conv_ids:
        target_conn = open_database(target_path)
        try:
            from siftd.storage.fts import rebuild_fts_index

            rebuild_fts_index(target_conn)
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
        WHERE m.id IN (SELECT DISTINCT r.model_id FROM responses r
                        WHERE r.conversation_id IN (SELECT id FROM _slice_conv_ids)
                        AND r.model_id IS NOT NULL)
    """)

    # Providers referenced by responses
    conn.execute("""
        INSERT OR IGNORE INTO slice.providers
        SELECT p.* FROM providers p
        WHERE p.id IN (SELECT DISTINCT r.provider_id FROM responses r
                        WHERE r.conversation_id IN (SELECT id FROM _slice_conv_ids)
                        AND r.provider_id IS NOT NULL)
    """)

    # Tools referenced by tool_calls
    conn.execute("""
        INSERT OR IGNORE INTO slice.tools
        SELECT t.* FROM tools t
        WHERE t.id IN (SELECT DISTINCT tc.tool_id FROM tool_calls tc
                        WHERE tc.conversation_id IN (SELECT id FROM _slice_conv_ids)
                        AND tc.tool_id IS NOT NULL)
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

    conn.execute("""
        INSERT OR IGNORE INTO slice.prompts
        SELECT * FROM prompts
        WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.responses
        SELECT * FROM responses
        WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
    """)

    # --- Content tables ---

    conn.execute("""
        INSERT OR IGNORE INTO slice.prompt_content
        SELECT pc.* FROM prompt_content pc
        WHERE pc.prompt_id IN (SELECT p.id FROM prompts p
                                WHERE p.conversation_id IN (SELECT id FROM _slice_conv_ids))
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.response_content
        SELECT rc.* FROM response_content rc
        WHERE rc.response_id IN (SELECT r.id FROM responses r
                                  WHERE r.conversation_id IN (SELECT id FROM _slice_conv_ids))
    """)

    # Content blobs referenced by tool_calls (must precede tool_calls for FK)
    conn.execute("""
        INSERT OR IGNORE INTO slice.content_blobs
        SELECT cb.* FROM content_blobs cb
        WHERE cb.hash IN (SELECT tc.result_hash FROM tool_calls tc
                          WHERE tc.conversation_id IN (SELECT id FROM _slice_conv_ids)
                          AND tc.result_hash IS NOT NULL)
    """)

    # Explicit columns: source DBs that went through ensure_content_blobs_table
    # have 'result_hash' as the last column (ALTER TABLE), but schema.sql has it
    # at position 8. SELECT * would put 'status' into result_hash, triggering
    # FK violation against content_blobs.
    conn.execute("""
        INSERT OR IGNORE INTO slice.tool_calls
            (id, response_id, conversation_id, tool_id, external_id,
             input, result, result_hash, status, timestamp)
        SELECT id, response_id, conversation_id, tool_id, external_id,
               input, result, result_hash, status, timestamp
        FROM tool_calls
        WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
    """)

    # --- Attribute tables ---

    conn.execute("""
        INSERT OR IGNORE INTO slice.conversation_attributes
        SELECT * FROM conversation_attributes
        WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.prompt_attributes
        SELECT pa.* FROM prompt_attributes pa
        WHERE pa.prompt_id IN (SELECT p.id FROM prompts p
                                WHERE p.conversation_id IN (SELECT id FROM _slice_conv_ids))
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.response_attributes
        SELECT ra.* FROM response_attributes ra
        WHERE ra.response_id IN (SELECT r.id FROM responses r
                                  WHERE r.conversation_id IN (SELECT id FROM _slice_conv_ids))
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.tool_call_attributes
        SELECT tca.* FROM tool_call_attributes tca
        WHERE tca.tool_call_id IN (SELECT tc.id FROM tool_calls tc
                                    WHERE tc.conversation_id IN (SELECT id FROM _slice_conv_ids))
    """)

    # --- Tag tables ---

    # Tags referenced by any junction table we'll copy
    conn.execute("""
        INSERT OR IGNORE INTO slice.tags
        SELECT t.* FROM tags t
        WHERE t.id IN (
            SELECT ct.tag_id FROM conversation_tags ct
            WHERE ct.conversation_id IN (SELECT id FROM _slice_conv_ids)
            UNION
            SELECT wt.tag_id FROM workspace_tags wt
            WHERE wt.workspace_id IN (SELECT w.id FROM slice.workspaces w)
            UNION
            SELECT tct.tag_id FROM tool_call_tags tct
            WHERE tct.tool_call_id IN (SELECT tc.id FROM tool_calls tc
                                        WHERE tc.conversation_id IN (SELECT id FROM _slice_conv_ids))
        )
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.conversation_tags
        SELECT * FROM conversation_tags
        WHERE conversation_id IN (SELECT id FROM _slice_conv_ids)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.workspace_tags
        SELECT * FROM workspace_tags
        WHERE workspace_id IN (SELECT id FROM slice.workspaces)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO slice.tool_call_tags
        SELECT * FROM tool_call_tags
        WHERE tool_call_id IN (SELECT tc.id FROM tool_calls tc
                                WHERE tc.conversation_id IN (SELECT id FROM _slice_conv_ids))
    """)

    # prompt_tags if it exists in source
    has_prompt_tags = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_tags'"
    ).fetchone()
    if has_prompt_tags:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS slice.prompt_tags (
                id TEXT PRIMARY KEY,
                prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at TEXT NOT NULL,
                UNIQUE (prompt_id, tag_id)
            )
        """)
        # Copy tags referenced by prompt_tags before inserting junction rows (FK)
        conn.execute("""
            INSERT OR IGNORE INTO slice.tags
            SELECT t.* FROM tags t
            WHERE t.id IN (SELECT pt.tag_id FROM prompt_tags pt
                            WHERE pt.prompt_id IN (SELECT p.id FROM prompts p
                                WHERE p.conversation_id IN (SELECT id FROM _slice_conv_ids)))
        """)
        conn.execute("""
            INSERT OR IGNORE INTO slice.prompt_tags
            SELECT pt.* FROM prompt_tags pt
            WHERE pt.prompt_id IN (SELECT p.id FROM prompts p
                                    WHERE p.conversation_id IN (SELECT id FROM _slice_conv_ids))
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
