"""Merge an external SQLite database (slice) into the main siftd database.

Uses ATTACH DATABASE for efficient cross-DB INSERT...SELECT. Vocabulary
tables are matched by natural key and remapped — the same harness/model/
workspace may have different ULIDs on each machine. Core tables use
INSERT OR IGNORE so UNIQUE constraints handle dedup naturally.
"""

from __future__ import annotations

from pathlib import Path

from siftd.storage.sqlite import open_database


def merge_database(
    target_db: Path,
    source_path: Path,
    *,
    rebuild_fts: bool = True,
    dry_run: bool = False,
    replace: bool = True,
) -> dict:
    """Merge a source database (slice) into the target database.

    Args:
        target_db: Path to the main siftd database.
        source_path: Path to the source database to merge in.
        rebuild_fts: Whether to rebuild the FTS5 index after merge.
        dry_run: If True, compute counts but roll back all changes.
        replace: If True (default), replace stale conversations with newer
            versions from the source. If False, keep existing versions.

    Returns:
        Dict with counts of merged entities.

    Raises:
        FileNotFoundError: If either database does not exist.
    """
    if not target_db.exists():
        raise FileNotFoundError(f"Target database not found: {target_db}")
    if not source_path.exists():
        raise FileNotFoundError(f"Source database not found: {source_path}")

    conn = open_database(target_db)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(source_path),))

        # Reject schema version mismatch — merge is for same-version slices.
        target_ver = conn.execute("PRAGMA main.user_version").fetchone()[0]
        source_ver = conn.execute("PRAGMA src.user_version").fetchone()[0]
        if target_ver != source_ver:
            conn.execute("DETACH DATABASE src")
            raise RuntimeError(
                f"Schema version mismatch: target is v{target_ver}, "
                f"source is v{source_ver}. Both databases must be the same "
                f"schema version to merge."
            )

        conn.execute("PRAGMA foreign_keys = OFF")

        if dry_run:
            conn.execute("SAVEPOINT merge_dry_run")

        stats = _merge_attached(conn, replace=replace)

        # Validate FK integrity before committing (so failures are atomic)
        if not dry_run:
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                conn.rollback()
                conn.execute("PRAGMA foreign_keys = ON")
                tables = {v[0] for v in violations}
                raise RuntimeError(
                    f"Foreign key violations after merge (tables: {', '.join(sorted(tables))}). "
                    "This may indicate a schema mismatch — please report this bug."
                )

        if dry_run:
            conn.execute("ROLLBACK TO merge_dry_run")
            conn.execute("RELEASE merge_dry_run")
        else:
            conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute("DETACH DATABASE src")
    finally:
        conn.close()

    # Rebuild FTS in target if requested and not dry_run
    if rebuild_fts and not dry_run:
        fts_conn = open_database(target_db)
        try:
            from siftd.storage.fts import rebuild_fts_index

            rebuild_fts_index(fts_conn)
        finally:
            fts_conn.close()

    return stats


def _merge_attached(conn, *, replace: bool = True) -> dict:
    """Perform the merge with source attached as 'src'. Returns stats dict."""

    # --- Step 1: Vocabulary ID mapping ---
    conn.execute("""
        CREATE TEMP TABLE _id_map (
            table_name TEXT NOT NULL,
            source_id  TEXT NOT NULL,
            target_id  TEXT NOT NULL,
            PRIMARY KEY (table_name, source_id)
        )
    """)

    workspaces_matched = 0

    # 1. harnesses — match on name
    _map_vocabulary(conn, "harnesses", "name")

    # 2. models — match on raw_name
    _map_vocabulary(conn, "models", "raw_name")

    # 3. providers — match on name
    _map_vocabulary(conn, "providers", "name")

    # 4. tools — match on name
    _map_vocabulary(conn, "tools", "name")

    # 5. workspaces — special: match on git_remote first, fall back to path
    workspaces_matched = _map_workspaces(conn)

    # 6. tags — match on name
    _map_vocabulary(conn, "tags", "name")

    # 7. tool_aliases — match on (raw_name, remapped harness_id), remap both FKs
    _map_tool_aliases(conn)

    # 8. pricing — match on (remapped model_id, remapped provider_id), remap both FKs
    _map_pricing(conn)

    # --- Step 1b: Replace stale conversations with newer source versions ---
    replaced_conversations = 0
    if replace:
        replaced_conversations = _replace_stale_conversations(conn)

    # --- Step 2: Core tables with FK remapping ---

    # Snapshot counts before inserts for delta stats
    conv_before = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    prompt_before = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]

    # conversations — remap harness_id, workspace_id
    conn.execute("""
        INSERT OR IGNORE INTO conversations
            (id, external_id, harness_id, workspace_id, branch, started_at, ended_at)
        SELECT
            sc.id,
            sc.external_id,
            COALESCE(hm.target_id, sc.harness_id),
            COALESCE(wm.target_id, sc.workspace_id),
            sc.branch,
            sc.started_at,
            sc.ended_at
        FROM src.conversations sc
        LEFT JOIN _id_map hm ON hm.table_name = 'harnesses' AND hm.source_id = sc.harness_id
        LEFT JOIN _id_map wm ON wm.table_name = 'workspaces' AND wm.source_id = sc.workspace_id
    """)

    conv_after = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    new_conversations = conv_after - conv_before

    src_conv_count = conn.execute("SELECT COUNT(*) FROM src.conversations").fetchone()[0]
    skipped_conversations = src_conv_count - new_conversations

    # prompts — no FK remapping needed, filter by conversation existence
    conn.execute("""
        INSERT OR IGNORE INTO prompts
        SELECT sp.* FROM src.prompts sp
        WHERE sp.conversation_id IN (SELECT id FROM main.conversations)
    """)
    prompt_after = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]

    # responses — remap model_id, provider_id
    resp_before = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
    conn.execute("""
        INSERT OR IGNORE INTO responses
            (id, conversation_id, prompt_id, model_id, provider_id,
             external_id, timestamp, input_tokens, output_tokens)
        SELECT
            sr.id,
            sr.conversation_id,
            sr.prompt_id,
            COALESCE(mm.target_id, sr.model_id),
            COALESCE(pm.target_id, sr.provider_id),
            sr.external_id,
            sr.timestamp,
            sr.input_tokens,
            sr.output_tokens
        FROM src.responses sr
        LEFT JOIN _id_map mm ON mm.table_name = 'models' AND mm.source_id = sr.model_id
        LEFT JOIN _id_map pm ON pm.table_name = 'providers' AND pm.source_id = sr.provider_id
        WHERE sr.conversation_id IN (SELECT id FROM main.conversations)
    """)
    resp_after = conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]

    # content_blobs — SHA256 PK, INSERT OR IGNORE handles dedup
    blob_before = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]
    conn.execute("""
        INSERT OR IGNORE INTO content_blobs
        SELECT * FROM src.content_blobs
    """)
    blob_after = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]

    # tool_calls — remap tool_id, explicit column list for ALTER TABLE ordering
    tc_before = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    conn.execute("""
        INSERT OR IGNORE INTO tool_calls
            (id, response_id, conversation_id, tool_id, external_id,
             input, result, result_hash, status, timestamp)
        SELECT
            stc.id,
            stc.response_id,
            stc.conversation_id,
            COALESCE(tm.target_id, stc.tool_id),
            stc.external_id,
            stc.input,
            stc.result,
            stc.result_hash,
            stc.status,
            stc.timestamp
        FROM src.tool_calls stc
        LEFT JOIN _id_map tm ON tm.table_name = 'tools' AND tm.source_id = stc.tool_id
        WHERE stc.conversation_id IN (SELECT id FROM main.conversations)
    """)
    tc_after = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]

    # --- Step 3: Content + Attribute tables ---

    conn.execute("""
        INSERT OR IGNORE INTO prompt_content
        SELECT spc.* FROM src.prompt_content spc
        WHERE spc.prompt_id IN (SELECT id FROM main.prompts)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO response_content
        SELECT src.* FROM src.response_content src
        WHERE src.response_id IN (SELECT id FROM main.responses)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO conversation_attributes
        SELECT * FROM src.conversation_attributes
        WHERE conversation_id IN (SELECT id FROM main.conversations)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO prompt_attributes
        SELECT spa.* FROM src.prompt_attributes spa
        WHERE spa.prompt_id IN (SELECT id FROM main.prompts)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO response_attributes
        SELECT sra.* FROM src.response_attributes sra
        WHERE sra.response_id IN (SELECT id FROM main.responses)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO tool_call_attributes
        SELECT stca.* FROM src.tool_call_attributes stca
        WHERE stca.tool_call_id IN (SELECT id FROM main.tool_calls)
    """)

    # --- Step 4: Tag junction tables (remap tag_id) ---

    # New tags = source rows inserted as-is (identity mapping: source_id kept as target_id).
    # Existing tags get remapped to target's ULID, so source_id != target_id.
    new_tags = conn.execute("""
        SELECT COUNT(*) FROM _id_map
        WHERE table_name = 'tags' AND source_id = target_id
    """).fetchone()[0]

    conn.execute("""
        INSERT OR IGNORE INTO conversation_tags
            (id, conversation_id, tag_id, applied_at)
        SELECT
            sct.id,
            sct.conversation_id,
            COALESCE(tm.target_id, sct.tag_id),
            sct.applied_at
        FROM src.conversation_tags sct
        LEFT JOIN _id_map tm ON tm.table_name = 'tags' AND tm.source_id = sct.tag_id
        WHERE sct.conversation_id IN (SELECT id FROM main.conversations)
    """)

    conn.execute("""
        INSERT OR IGNORE INTO workspace_tags
            (id, workspace_id, tag_id, applied_at)
        SELECT
            swt.id,
            COALESCE(wm.target_id, swt.workspace_id),
            COALESCE(tm.target_id, swt.tag_id),
            swt.applied_at
        FROM src.workspace_tags swt
        LEFT JOIN _id_map wm ON wm.table_name = 'workspaces' AND wm.source_id = swt.workspace_id
        LEFT JOIN _id_map tm ON tm.table_name = 'tags' AND tm.source_id = swt.tag_id
    """)

    conn.execute("""
        INSERT OR IGNORE INTO tool_call_tags
            (id, tool_call_id, tag_id, applied_at)
        SELECT
            stct.id,
            stct.tool_call_id,
            COALESCE(tm.target_id, stct.tag_id),
            stct.applied_at
        FROM src.tool_call_tags stct
        LEFT JOIN _id_map tm ON tm.table_name = 'tags' AND tm.source_id = stct.tag_id
        WHERE stct.tool_call_id IN (SELECT id FROM main.tool_calls)
    """)

    # prompt_tags — conditional (may not exist in source)
    _has_src_prompt_tags = conn.execute(
        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='prompt_tags'"
    ).fetchone()
    if _has_src_prompt_tags:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_tags (
                id TEXT PRIMARY KEY,
                prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at TEXT NOT NULL,
                UNIQUE (prompt_id, tag_id)
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO prompt_tags
                (id, prompt_id, tag_id, applied_at)
            SELECT
                spt.id,
                spt.prompt_id,
                COALESCE(tm.target_id, spt.tag_id),
                spt.applied_at
            FROM src.prompt_tags spt
            LEFT JOIN _id_map tm ON tm.table_name = 'tags' AND tm.source_id = spt.tag_id
            WHERE spt.prompt_id IN (SELECT id FROM main.prompts)
        """)

    # --- Step 5: content_blobs ref_count ---

    conn.execute("""
        UPDATE content_blobs SET ref_count = (
            SELECT COUNT(*) FROM tool_calls WHERE result_hash = content_blobs.hash
        ) WHERE hash IN (SELECT hash FROM src.content_blobs)
    """)

    # Clean up
    conn.execute("DROP TABLE _id_map")

    return {
        "conversations": new_conversations,
        "replaced_conversations": replaced_conversations,
        "skipped_conversations": skipped_conversations,
        "prompts": prompt_after - prompt_before,
        "responses": resp_after - resp_before,
        "tool_calls": tc_after - tc_before,
        "content_blobs": blob_after - blob_before,
        "tags": new_tags,
        "workspaces_matched": workspaces_matched,
    }


def _replace_stale_conversations(conn) -> int:
    """Delete target conversations that have a newer version in source.

    A source conversation is "newer" when it shares the same
    (harness_id, external_id) but has a later ULID (= later ingest time).
    This happens when a conversation is re-ingested after growing or after
    a parser fix.

    Deletes the stale conversation and all its children so the subsequent
    INSERT OR IGNORE picks up the source version instead of skipping it.

    Returns the number of conversations replaced.
    """
    # Find stale target conversations: same natural key, source ULID is newer.
    # ULIDs are lexicographically time-ordered, so id comparison works.
    stale_rows = conn.execute("""
        SELECT m.id AS target_id
        FROM src.conversations s
        JOIN main.conversations m
            ON m.harness_id = COALESCE(
                (SELECT im.target_id FROM _id_map im
                 WHERE im.table_name = 'harnesses' AND im.source_id = s.harness_id),
                s.harness_id)
            AND m.external_id = s.external_id
        WHERE s.id > m.id
    """).fetchall()

    if not stale_rows:
        return 0

    stale_ids = [r[0] for r in stale_rows]

    # Build a temp table for efficient joins
    conn.execute("CREATE TEMP TABLE _stale_convs (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO _stale_convs VALUES (?)", [(cid,) for cid in stale_ids])

    # Delete grandchildren first (tables referencing prompts/responses/tool_calls)
    conn.execute("""
        DELETE FROM prompt_content
        WHERE prompt_id IN (SELECT id FROM prompts WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    conn.execute("""
        DELETE FROM prompt_attributes
        WHERE prompt_id IN (SELECT id FROM prompts WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    # prompt_tags may not exist
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_tags'").fetchone():
        conn.execute("""
            DELETE FROM prompt_tags
            WHERE prompt_id IN (SELECT id FROM prompts WHERE conversation_id IN (SELECT id FROM _stale_convs))
        """)

    conn.execute("""
        DELETE FROM response_content
        WHERE response_id IN (SELECT id FROM responses WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    conn.execute("""
        DELETE FROM response_attributes
        WHERE response_id IN (SELECT id FROM responses WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)

    conn.execute("""
        DELETE FROM tool_call_attributes
        WHERE tool_call_id IN (SELECT id FROM tool_calls WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    conn.execute("""
        DELETE FROM tool_call_tags
        WHERE tool_call_id IN (SELECT id FROM tool_calls WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)

    # Delete children
    conn.execute("DELETE FROM prompts WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    conn.execute("DELETE FROM responses WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    conn.execute("DELETE FROM tool_calls WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    conn.execute("DELETE FROM conversation_attributes WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    conn.execute("DELETE FROM conversation_tags WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    conn.execute("DELETE FROM ingested_files WHERE conversation_id IN (SELECT id FROM _stale_convs)")

    # Delete the stale conversations themselves
    conn.execute("DELETE FROM conversations WHERE id IN (SELECT id FROM _stale_convs)")

    conn.execute("DROP TABLE _stale_convs")

    return len(stale_ids)


def _map_vocabulary(conn, table: str, natural_key: str) -> None:
    """Map source vocabulary IDs to target IDs via natural key match.

    For rows that exist in target: map source_id → target_id.
    For rows new to target: INSERT them and map source_id → source_id (identity).
    """
    # Insert new rows from source that have no natural-key match in target
    conn.execute(f"""
        INSERT OR IGNORE INTO {table}
        SELECT s.* FROM src.{table} s
        WHERE NOT EXISTS (
            SELECT 1 FROM main.{table} t WHERE t.{natural_key} = s.{natural_key}
        )
    """)

    # Map ALL source IDs → target equivalents via natural key JOIN
    conn.execute(f"""
        INSERT OR IGNORE INTO _id_map (table_name, source_id, target_id)
        SELECT '{table}', s.id, t.id
        FROM src.{table} s
        JOIN main.{table} t ON t.{natural_key} = s.{natural_key}
    """)


def _map_workspaces(conn) -> int:
    """Map workspace IDs with git_remote priority, path fallback. Returns matched-by-remote count."""

    # Phase 1: Match by git_remote (non-NULL on both sides)
    conn.execute("""
        INSERT OR IGNORE INTO _id_map (table_name, source_id, target_id)
        SELECT 'workspaces', s.id, t.id
        FROM src.workspaces s
        JOIN main.workspaces t ON t.git_remote = s.git_remote
        WHERE s.git_remote IS NOT NULL AND t.git_remote IS NOT NULL
    """)
    matched_by_remote = conn.execute(
        "SELECT COUNT(*) FROM _id_map WHERE table_name = 'workspaces'"
    ).fetchone()[0]

    # Phase 2: For unmapped source workspaces, try path match
    conn.execute("""
        INSERT OR IGNORE INTO _id_map (table_name, source_id, target_id)
        SELECT 'workspaces', s.id, t.id
        FROM src.workspaces s
        JOIN main.workspaces t ON t.path = s.path
        WHERE NOT EXISTS (
            SELECT 1 FROM _id_map m
            WHERE m.table_name = 'workspaces' AND m.source_id = s.id
        )
    """)

    # Phase 3: Insert genuinely new workspaces
    conn.execute("""
        INSERT OR IGNORE INTO workspaces
        SELECT s.* FROM src.workspaces s
        WHERE NOT EXISTS (
            SELECT 1 FROM _id_map m
            WHERE m.table_name = 'workspaces' AND m.source_id = s.id
        )
    """)

    # Map the newly inserted workspaces (identity mapping)
    conn.execute("""
        INSERT OR IGNORE INTO _id_map (table_name, source_id, target_id)
        SELECT 'workspaces', s.id, s.id
        FROM src.workspaces s
        WHERE NOT EXISTS (
            SELECT 1 FROM _id_map m
            WHERE m.table_name = 'workspaces' AND m.source_id = s.id
        )
    """)

    return matched_by_remote


def _map_tool_aliases(conn) -> None:
    """Map tool_aliases: match on (raw_name, remapped harness_id), remap both FKs."""

    # Insert new aliases with remapped FKs
    conn.execute("""
        INSERT OR IGNORE INTO tool_aliases (id, raw_name, harness_id, tool_id)
        SELECT
            s.id,
            s.raw_name,
            COALESCE(hm.target_id, s.harness_id),
            COALESCE(tm.target_id, s.tool_id)
        FROM src.tool_aliases s
        LEFT JOIN _id_map hm ON hm.table_name = 'harnesses' AND hm.source_id = s.harness_id
        LEFT JOIN _id_map tm ON tm.table_name = 'tools' AND tm.source_id = s.tool_id
        WHERE NOT EXISTS (
            SELECT 1 FROM main.tool_aliases t
            WHERE t.raw_name = s.raw_name
              AND t.harness_id = COALESCE(hm.target_id, s.harness_id)
        )
    """)


def _map_pricing(conn) -> None:
    """Map pricing: match on (remapped model_id, remapped provider_id), remap both FKs."""

    conn.execute("""
        INSERT OR IGNORE INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok)
        SELECT
            s.id,
            COALESCE(mm.target_id, s.model_id),
            COALESCE(pm.target_id, s.provider_id),
            s.input_per_mtok,
            s.output_per_mtok
        FROM src.pricing s
        LEFT JOIN _id_map mm ON mm.table_name = 'models' AND mm.source_id = s.model_id
        LEFT JOIN _id_map pm ON pm.table_name = 'providers' AND pm.source_id = s.provider_id
        WHERE NOT EXISTS (
            SELECT 1 FROM main.pricing t
            WHERE t.model_id = COALESCE(mm.target_id, s.model_id)
              AND t.provider_id = COALESCE(pm.target_id, s.provider_id)
        )
    """)
