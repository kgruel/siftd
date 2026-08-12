"""Merge an external SQLite database (slice) into the main siftd database.

Uses ATTACH DATABASE for efficient cross-DB INSERT...SELECT. Vocabulary
tables are matched by natural key and remapped — the same harness/model/
workspace may have different ULIDs on each machine. Core tables use
INSERT OR IGNORE so UNIQUE constraints handle dedup naturally.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from siftd.storage.fts import rebuild_fts_index
from siftd.storage.replacement import (
    ConversationCarryover,
    restore_conversation,
    snapshot_conversation,
)
from siftd.storage.sql_helpers import has_conversation_owners_table
from siftd.storage.sqlite import open_database

logger = logging.getLogger(__name__)


def merge_database(
    target_db: Path,
    source_path: Path,
    *,
    rebuild_fts: bool = True,
    dry_run: bool = False,
    replace: bool = True,
    before_commit: Callable[[sqlite3.Connection, dict], None] | None = None,
    preflight: bool = True,
    user_id: str | None = None,
) -> dict:
    """Merge a source database (slice) into the target database.

    Args:
        target_db: Path to the main siftd database.
        source_path: Path to the source database to merge in.
        rebuild_fts: Accepted and ignored. Merged content is indexed as part
            of the merge (#49), so this no longer decides whether it is
            searchable; it used to trigger a *full* corpus rebuild on top,
            which the scoped write makes redundant. Kept for compatibility —
            removal is tracked in #74.
        dry_run: If True, compute counts but roll back all changes.
        replace: If True (default), replace stale conversations with newer
            versions from the source. If False, keep existing versions.
        before_commit: Optional callback(conn, stats) invoked after merge
            but before commit.  Runs in the same transaction as the merge,
            so any writes are atomic with the merge itself.
        preflight: If True (default), run structural integrity checks on the
            source before merging. Pass False when the caller has already
            run preflight (e.g. receive_database calls merge_database after
            its own preflight check).
        user_id: Pushing identity. When set (and the target has a
            ``conversation_owners`` table), the merge is owner-partitioned: it
            may only insert into / replace conversations the pusher owns or that
            are unowned. None (single-tenant / SSH) merges unrestricted. This is
            the multi-tenant write-IDOR guard — without it one client can forge
            content into, or destroy, another tenant's conversations.

    Returns:
        Dict with counts of merged entities.

    Raises:
        FileNotFoundError: If either database does not exist.
        PreflightError: If preflight=True and source fails integrity checks.
    """
    if not target_db.exists():
        raise FileNotFoundError(f"Target database not found: {target_db}")
    if not source_path.exists():
        raise FileNotFoundError(f"Source database not found: {source_path}")

    if preflight:
        from siftd.api.database import run_preflight
        run_preflight(source_path)

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

        source_missing = _missing_merge_runtime_schema(conn, "src")
        if source_missing:
            conn.execute("DETACH DATABASE src")
            missing = ", ".join(source_missing)
            raise RuntimeError(
                "Source database is missing required runtime schema: "
                f"{missing}. Open it with the current build before merging."
            )

        conn.execute("PRAGMA foreign_keys = OFF")

        if dry_run:
            conn.execute("SAVEPOINT merge_dry_run")

        stats = _merge_attached(conn, replace=replace, user_id=user_id)

        # Index what this merge wrote, in the merge's own transaction —
        # unconditional, never gated on rebuild_fts (#49). In-transaction
        # because `_replace_stale_conversations` already deleted the replaced
        # rows' index entries below; splitting the delete from the insert is
        # what would leave a window where search sees neither version.
        #
        # The scope is derived from the *content* the source carried, not from
        # `new_conversation_ids`/`replaced_conversation_ids`. Those answer a
        # question about conversation rows, and the thing being indexed is
        # content rows: a conversation present in both databases under the same
        # id is neither new nor replaced, yet the `event_content` insert above
        # still adds the source's blocks to it. Scoping by conversation
        # bookkeeping left exactly that case unsearchable.
        if not dry_run:
            rebuild_fts_index(conn, conversation_ids=[
                row[0] for row in conn.execute("""
                    SELECT DISTINCT e.conversation_id
                    FROM main.events e
                    WHERE e.id IN (SELECT event_id FROM src.event_content)
                """).fetchall()
            ])

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

        if before_commit and not dry_run:
            before_commit(conn, stats)

        if dry_run:
            conn.execute("ROLLBACK TO merge_dry_run")
            conn.execute("RELEASE merge_dry_run")
        else:
            conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute("DETACH DATABASE src")
    finally:
        conn.close()

    # Rebuild the derived tier (and FTS, if requested) after a real merge.
    # The merge copies raw rows via INSERT...SELECT but never touches the
    # derived tier, so usage_by_conv_model / conversation_stats would go stale —
    # the live sync/receive path merges new conversations that would otherwise
    # never reach the rollup, silently undercounting server stats.  Mirrors the
    # ingest invariant.  Unconditional, as the FTS write above now is too.
    # Full rebuild — correct but O(corpus); an incremental per-conversation
    # upsert is a deferred optimization.  Post-commit, unlike the FTS write
    # above: a failure here leaves the tier stale rather than aborting the
    # merge, and a rollup is re-derivable where a missing index row is not.
    if not dry_run:
        post_conn = open_database(target_db)
        try:
            from siftd.storage.usage_rollup import rebuild_rollups

            rebuild_rollups(post_conn, commit=True)
        finally:
            post_conn.close()

    return stats


def _merge_attached(conn, *, replace: bool = True, user_id: str | None = None) -> dict:
    """Perform the merge with source attached as 'src'. Returns stats dict.

    When ``user_id`` is set and the target carries ownership data, the merge is
    owner-partitioned (multi-tenant write-IDOR guard): every cross-DB write is
    confined to conversations the pusher owns or that are unowned, so one
    client cannot inject content into, or destroy, another tenant's threads.
    When ``user_id`` is None (single-tenant / SSH) the merge is unrestricted —
    the eligible sets below resolve to "all rows" and behavior is unchanged.
    """
    owner_scoped = bool(user_id) and has_conversation_owners_table(conn)

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

    # NOTE: pricing is NOT merged. It is reference data (a fact about a model,
    # identical on every machine — see siftd.pricing), not per-machine data. Copying
    # it between machines was the contamination vector that froze stale prices via
    # INSERT OR IGNORE. The post-merge open_database() below reprojects the LOCAL
    # pricing reference onto the merged DB (including any newly-merged models), so
    # prices stay sourced from this machine's reference, not the source DB's.

    # --- Step 1b: Replace stale conversations with newer source versions ---
    replaced_conversations = 0
    replaced_conversation_ids: list[str] = []
    carryovers: dict[str, ConversationCarryover] = {}
    if replace:
        replaced_conversations, replaced_conversation_ids, carryovers = (
            _replace_stale_conversations(
                conn, user_id=user_id, owner_scoped=owner_scoped,
            )
        )

    # --- Step 2: Core tables with FK remapping ---

    # Snapshot existing conversation IDs for diff after INSERT
    conn.execute("CREATE TEMP TABLE _pre_merge_conv_ids AS SELECT id FROM conversations")

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

    # Compute new conversation IDs via diff
    new_conv_id_rows = conn.execute("""
        SELECT id FROM conversations
        WHERE id NOT IN (SELECT id FROM _pre_merge_conv_ids)
    """).fetchall()
    new_conversation_ids = [r[0] for r in new_conv_id_rows]
    new_conversations = len(new_conversation_ids)
    conn.execute("DROP TABLE _pre_merge_conv_ids")

    src_conv_count = conn.execute("SELECT COUNT(*) FROM src.conversations").fetchone()[0]
    skipped_conversations = src_conv_count - new_conversations

    # --- Step 1c: Owner-eligible target sets (multi-tenant write-IDOR guard) ---
    # Every child-row insert below confines itself to these sets instead of the
    # whole DB. When owner_scoped, eligible = conversations the pusher owns OR
    # that are unowned (the just-inserted source conversations are unowned here —
    # they're stamped to the pusher in receive's before_commit — so the pusher's
    # own new content lands, while another tenant's owned conversations are
    # excluded). When not scoped, eligible = all rows, so behavior is unchanged.
    if owner_scoped:
        conn.execute(
            "CREATE TEMP TABLE _eligible_convs AS "
            "SELECT id AS conversation_id FROM main.conversations "
            "WHERE id IN (SELECT conversation_id FROM conversation_owners WHERE user_id = ?) "
            "   OR id NOT IN (SELECT conversation_id FROM conversation_owners)",
            (user_id,),
        )
    else:
        conn.execute(
            "CREATE TEMP TABLE _eligible_convs AS "
            "SELECT id AS conversation_id FROM main.conversations"
        )

    # content_blobs — SHA256 PK, INSERT OR IGNORE handles dedup
    blob_before = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]
    conn.execute("""
        INSERT OR IGNORE INTO content_blobs
        SELECT * FROM src.content_blobs
    """)
    blob_after = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]

    # --- Step 2b: Polymorphic event tables (v4 schema, present in all DBs opened since slice 1) ---

    if conn.execute(
        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='events'"
    ).fetchone():
        # events — explicit columns; filter by conversations already in target
        conn.execute("""
            INSERT OR IGNORE INTO events
                (id, kind, conversation_id, parent_id, external_id, timestamp)
            SELECT se.id, se.kind, se.conversation_id, se.parent_id, se.external_id, se.timestamp
            FROM src.events se
            WHERE se.conversation_id IN (SELECT conversation_id FROM _eligible_convs)
        """)
        # event_response — remap model_id, provider_id; filter to events that landed in main
        conn.execute("""
            INSERT OR IGNORE INTO event_response
                (event_id, model_id, provider_id, input_tokens, output_tokens)
            SELECT
                ser.event_id,
                COALESCE(mm.target_id, ser.model_id),
                COALESCE(pm.target_id, ser.provider_id),
                ser.input_tokens,
                ser.output_tokens
            FROM src.event_response ser
            LEFT JOIN _id_map mm ON mm.table_name = 'models' AND mm.source_id = ser.model_id
            LEFT JOIN _id_map pm ON pm.table_name = 'providers' AND pm.source_id = ser.provider_id
            WHERE ser.event_id IN (
                SELECT id FROM main.events
                WHERE conversation_id IN (SELECT conversation_id FROM _eligible_convs)
            )
        """)
        # event_tool_call — remap tool_id; filter to events that landed in main
        conn.execute("""
            INSERT OR IGNORE INTO event_tool_call
                (event_id, tool_id, input, result_hash, status)
            SELECT
                setc.event_id,
                COALESCE(tm.target_id, setc.tool_id),
                setc.input,
                setc.result_hash,
                setc.status
            FROM src.event_tool_call setc
            LEFT JOIN _id_map tm ON tm.table_name = 'tools' AND tm.source_id = setc.tool_id
            WHERE setc.event_id IN (
                SELECT id FROM main.events
                WHERE conversation_id IN (SELECT conversation_id FROM _eligible_convs)
            )
        """)

    # --- Step 3: Content + Attribute tables ---

    if conn.execute(
        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='event_content'"
    ).fetchone():
        conn.execute("""
            INSERT OR IGNORE INTO event_content
                (id, event_id, block_index, block_type, content)
            SELECT sec.id, sec.event_id, sec.block_index, sec.block_type, sec.content
            FROM src.event_content sec
            WHERE sec.event_id IN (
                SELECT id FROM main.events
                WHERE conversation_id IN (SELECT conversation_id FROM _eligible_convs)
            )
        """)

    conn.execute("""
        INSERT OR IGNORE INTO attributes
        SELECT * FROM src.attributes
        WHERE target_id IN (SELECT conversation_id FROM _eligible_convs)
           OR target_id IN (
                SELECT id FROM main.events
                WHERE conversation_id IN (SELECT conversation_id FROM _eligible_convs)
           )
    """)

    # --- Step 4: Tag junction tables (remap tag_id) ---

    # New tags = source rows inserted as-is (identity mapping: source_id kept as target_id).
    # Existing tags get remapped to target's ULID, so source_id != target_id.
    new_tags = conn.execute("""
        SELECT COUNT(*) FROM _id_map
        WHERE table_name = 'tags' AND source_id = target_id
    """).fetchone()[0]

    # --- tag_assignments (polymorphic, added slice 5) ---
    # Workspace target_ids are remapped via _id_map; conversation/event IDs are preserved.
    if conn.execute(
        "SELECT 1 FROM src.sqlite_master WHERE type='table' AND name='tag_assignments'"
    ).fetchone():
        conn.execute("""
            INSERT OR IGNORE INTO tag_assignments (id, target_kind, target_id, tag_id, applied_at)
            SELECT
                sta.id,
                sta.target_kind,
                CASE
                    WHEN sta.target_kind = 'workspace'
                    THEN COALESCE(ws_map.target_id, sta.target_id)
                    ELSE sta.target_id
                END AS target_id,
                COALESCE(tag_map.target_id, sta.tag_id) AS tag_id,
                sta.applied_at
            FROM src.tag_assignments sta
            LEFT JOIN _id_map tag_map ON tag_map.table_name = 'tags'       AND tag_map.source_id = sta.tag_id
            LEFT JOIN _id_map ws_map  ON ws_map.table_name  = 'workspaces' AND ws_map.source_id  = sta.target_id
            WHERE
                (sta.target_kind = 'conversation'
                 AND sta.target_id IN (SELECT conversation_id FROM _eligible_convs))
                OR
                (sta.target_kind = 'workspace'
                 AND COALESCE(ws_map.target_id, sta.target_id) IN (SELECT id FROM main.workspaces))
                OR
                (sta.target_kind IN ('prompt','response','tool_call','exchange')
                 AND sta.target_id IN (
                     SELECT id FROM main.events
                     WHERE conversation_id IN (SELECT conversation_id FROM _eligible_convs)
                 ))
                OR
                (sta.target_kind = 'block'
                 AND sta.target_id IN (
                     SELECT ec.id FROM main.event_content ec
                     JOIN main.events e ON e.id = ec.event_id
                     WHERE e.conversation_id IN (SELECT conversation_id FROM _eligible_convs)
                 ))
        """)

    # --- Step 4b: Re-attach what the replaced conversations carried ---
    # Here, not beside the delete, because a carryover re-points onto rows that
    # only exist once the source's conversations, events and tag vocabulary have
    # landed — steps 2 through 4 above. Ownership needs no rejoin and could have
    # gone earlier (it used to, as `_replaced_owner_map`), but splitting the two
    # halves apart is how one of them comes to be dropped: that split is exactly
    # what #77 was, in mirror image of #54.
    #
    # Deferring ownership past `_eligible_convs` does not widen the merge's
    # owner gate. Under `owner_scoped` the stale match already confined itself
    # to conversations the pusher owns or that are unowned, so the replacement
    # is eligible either way — as the pusher's own row before, as an unowned row
    # now, which is the branch the comment at `_eligible_convs` already
    # describes for every other source conversation.
    replacement_unmatched = 0
    for source_conv_id, carryover in carryovers.items():
        unmatched = restore_conversation(conn, source_conv_id, carryover)
        replacement_unmatched += unmatched + carryover.dropped
        if unmatched or carryover.dropped:
            logger.warning(
                f"{unmatched + carryover.dropped} tag(s) on the conversation "
                f"replaced by {source_conv_id} could not be carried across the "
                "merge (the event is no longer in the source, is synthetic, or "
                "is a block-level assignment)"
            )

    # --- Step 5: content_blobs ref_count ---

    conn.execute("""
        UPDATE content_blobs SET ref_count = (
            SELECT COUNT(*) FROM event_tool_call WHERE result_hash = content_blobs.hash
        ) WHERE hash IN (SELECT hash FROM src.content_blobs)
    """)

    # --- Step 5b: GC orphan blobs introduced by this merge (D2) ---
    # content_blobs are copied wholesale (unfiltered) above, but the
    # event_tool_call rows that reference them are filtered (to landed events,
    # now also owner-gated). So a blob whose only referrers were skipped (a
    # dedup'd duplicate conversation, an owner-excluded tenant, or a
    # non-landing event) ends up with ref_count = 0 and never gets collected:
    # the AFTER DELETE trigger on event_tool_call only fires for rows that
    # existed to be deleted. Left alone this leaks one blob per unreferenced
    # source blob on every overlapping re-push — unbounded growth. Delete the
    # ones this merge introduced that the recompute just proved unreferenced.
    # Scoped to src hashes (don't touch pre-existing orphans) and safe under
    # content-addressing: a future push that needs the blob re-supplies it.
    conn.execute("""
        DELETE FROM content_blobs
        WHERE ref_count = 0
          AND hash IN (SELECT hash FROM src.content_blobs)
    """)
    # Recompute the net blob delta after GC so the stat reflects blobs that
    # actually landed and are referenced, not transient orphans.
    blob_after = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]

    # Clean up
    conn.execute("DROP TABLE _id_map")
    conn.execute("DROP TABLE _eligible_convs")

    return {
        "conversations": new_conversations - replaced_conversations,
        "replaced_conversations": replaced_conversations,
        "skipped_conversations": skipped_conversations,
        "new_conversation_ids": new_conversation_ids,
        "replaced_conversation_ids": replaced_conversation_ids,
        "content_blobs": blob_after - blob_before,
        "tags": new_tags,
        "workspaces_matched": workspaces_matched,
    }


def _replace_stale_conversations(
    conn, *, user_id: str | None = None, owner_scoped: bool = False,
) -> tuple[int, list[str], dict[str, ConversationCarryover]]:
    """Delete target conversations that have a newer version in source.

    A source conversation is "newer" when it shares the same
    (harness_id, external_id) but has a later ULID (= later ingest time).
    This happens when a conversation is re-ingested after growing or after
    a parser fix.

    Deletes the stale conversation and all its children so the subsequent
    INSERT OR IGNORE picks up the source version instead of skipping it.

    When ``owner_scoped`` (multi-tenant push with a known ``user_id``), the
    stale match is confined to conversations the pusher OWNS or that are
    UNOWNED. Without this, the match keys purely on the client-controlled
    ``(harness_id, external_id)``: a hostile client could push a colliding
    natural key with a newer ULID and DELETE another tenant's conversation
    (and have the replacement re-stamped to that tenant) — the most reachable
    write-side multi-tenant IDOR, needing no knowledge of the victim's ULIDs.

    Returns (count_replaced, replacement_source_ids, carryovers) where
    replacement_source_ids are the source conversation IDs that will replace the
    stale target versions, and carryovers maps each of those to what the target
    version held that no source row reproduces — re-attached in step 4b.
    """
    # Find stale target conversations: same natural key, source ULID is newer.
    # ULIDs are lexicographically time-ordered, so id comparison works.
    owner_predicate = ""
    match_params: tuple = ()
    if owner_scoped:
        owner_predicate = (
            " AND (m.id IN (SELECT conversation_id FROM conversation_owners WHERE user_id = ?)"
            "      OR m.id NOT IN (SELECT conversation_id FROM conversation_owners))"
        )
        match_params = (user_id,)
    stale_rows = conn.execute(f"""
        SELECT m.id AS target_id, s.id AS source_id
        FROM src.conversations s
        JOIN main.conversations m
            ON m.harness_id = COALESCE(
                (SELECT im.target_id FROM _id_map im
                 WHERE im.table_name = 'harnesses' AND im.source_id = s.harness_id),
                s.harness_id)
            AND m.external_id = s.external_id
        WHERE s.id > m.id{owner_predicate}
    """, match_params).fetchall()

    if not stale_rows:
        return 0, [], {}

    stale_ids = [r[0] for r in stale_rows]
    replacement_ids = [r[1] for r in stale_rows]

    # Take everything the delete below would destroy that no source row puts
    # back — tags and ownership, through the same primitive ingest's replacement
    # door uses (`storage/replacement.py`). Keyed by the *source* id, because
    # that is the conversation the carryover re-attaches to: merge replaces on
    # the natural key, so the replacement arrives under a different ULID.
    #
    # This has to happen here rather than after the delete, and it is not a
    # matter of taste: the `tr_polymorphic_*_cleanup` triggers are AFTER DELETE
    # triggers, so they fire on merge's top-level deletes whatever the
    # foreign_keys pragma says. Once the delete has run there is nothing left to
    # read. Ownership alone used to be carried here, by a pair of temp tables
    # (`_stale_to_source` / `_replaced_owner_map`) that hand-rolled half of what
    # this primitive does — the mirror image of #54, which carried the other
    # half and dropped this one.
    carryovers = {
        source_id: snapshot_conversation(conn, target_id)
        for target_id, source_id in stale_rows
    }

    # Build a temp table for efficient joins
    conn.execute("CREATE TEMP TABLE _stale_convs (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO _stale_convs VALUES (?)", [(cid,) for cid in stale_ids])

    # foreign_keys=OFF disables cascade, so delete extension rows explicitly before events
    conn.execute("""
        DELETE FROM event_content
        WHERE event_id IN (SELECT id FROM events WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    conn.execute("""
        DELETE FROM event_response
        WHERE event_id IN (SELECT id FROM events WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    conn.execute("""
        DELETE FROM event_tool_call
        WHERE event_id IN (SELECT id FROM events WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    conn.execute("""
        DELETE FROM attributes
        WHERE target_id IN (SELECT id FROM _stale_convs)
           OR target_id IN (SELECT id FROM events WHERE conversation_id IN (SELECT id FROM _stale_convs))
    """)
    # tag_assignments: must delete event-kind rows before events are deleted (no FK
    # cascade). Block rows too — the v12 event_content trigger would catch them on
    # cascade, but only on a v12+ DB with recursive triggers; explicit is uniform.
    conn.execute("""
        DELETE FROM tag_assignments
        WHERE (target_kind = 'conversation' AND target_id IN (SELECT id FROM _stale_convs))
           OR (target_kind IN ('prompt','response','tool_call','exchange')
               AND target_id IN (
                   SELECT id FROM events WHERE conversation_id IN (SELECT id FROM _stale_convs)
               ))
           OR (target_kind = 'block'
               AND target_id IN (
                   SELECT ec.id FROM event_content ec
                   JOIN events e ON e.id = ec.event_id
                   WHERE e.conversation_id IN (SELECT id FROM _stale_convs)
               ))
    """)
    conn.execute("DELETE FROM events WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    conn.execute("DELETE FROM ingested_files WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    # conversation_owners — table may not exist on pre-migration DBs
    if has_conversation_owners_table(conn):
        conn.execute("DELETE FROM conversation_owners WHERE conversation_id IN (SELECT id FROM _stale_convs)")

    # The derived tier is rebuilt post-commit, but foreign_key_check runs before that.
    conn.execute("DELETE FROM usage_by_conv_model WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    conn.execute("DELETE FROM conversation_stats WHERE conversation_id IN (SELECT id FROM _stale_convs)")
    # content_fts is virtual: no FK, invisible to foreign_key_check. Still
    # load-bearing after #49 — these are the *old* ids, and the scoped index
    # write covers only the replacements' (new) ids, so nothing else clears
    # these rows.
    conn.execute("DELETE FROM content_fts WHERE conversation_id IN (SELECT id FROM _stale_convs)")

    # Delete the stale conversations themselves
    conn.execute("DELETE FROM conversations WHERE id IN (SELECT id FROM _stale_convs)")

    conn.execute("DROP TABLE _stale_convs")

    return len(stale_ids), replacement_ids, carryovers


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
        WHERE (s.git_remote IS NULL OR t.git_remote IS NULL)
          AND NOT EXISTS (
            SELECT 1 FROM _id_map m
            WHERE m.table_name = 'workspaces' AND m.source_id = s.id
        )
    """)

    # Phase 3: Insert genuinely new workspaces
    conn.execute("""
        INSERT OR IGNORE INTO workspaces (id, path, git_remote, discovered_at)
        SELECT
            s.id,
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM main.workspaces t
                    WHERE t.path = s.path
                      AND t.git_remote IS NOT NULL
                      AND s.git_remote IS NOT NULL
                      AND t.git_remote != s.git_remote
                )
                THEN s.path || ' [remote=' || s.git_remote || ']'
                ELSE s.path
            END,
            s.git_remote,
            s.discovered_at
        FROM src.workspaces s
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


def _missing_merge_runtime_schema(conn, schema_name: str) -> list[str]:
    """Return runtime schema objects merge depends on but the DB does not have."""
    missing = []

    content_blobs = conn.execute(
        f"SELECT 1 FROM {schema_name}.sqlite_master "
        "WHERE type='table' AND name='content_blobs'"
    ).fetchone()
    if not content_blobs:
        missing.append("content_blobs table")

    for table in ("events", "event_response", "event_tool_call", "event_content"):
        exists = conn.execute(
            f"SELECT 1 FROM {schema_name}.sqlite_master "
            f"WHERE type='table' AND name='{table}'"
        ).fetchone()
        if not exists:
            missing.append(f"{table} table")

    return missing


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


