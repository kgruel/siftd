from siftd.doctor.checks import CheckContext, CheckCost, Finding


class OrphanedChunksCheck:
    """Detects embedding chunks whose conversations no longer exist in the main DB."""

    name = "orphaned-chunks"
    description = "Embedding chunks referencing deleted conversations"
    has_fix = True
    requires_db = True
    requires_embed_db = True
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        if not embeddings_available():
            return []

        if not ctx.embed_db_path.exists():
            return []

        conn = ctx.get_db_conn()
        embed_conn = ctx.get_embed_conn()

        from siftd.storage.embeddings import get_indexed_conversation_ids

        embed_ids = get_indexed_conversation_ids(embed_conn)
        if not embed_ids:
            return []

        main_ids = {
            row[0]
            for row in conn.execute("SELECT id FROM conversations").fetchall()
        }

        orphaned_ids = embed_ids - main_ids
        if not orphaned_ids:
            return []

        placeholders = ",".join("?" * len(orphaned_ids))
        count = embed_conn.execute(
            f"SELECT COUNT(*) FROM chunks WHERE conversation_id IN ({placeholders})",
            list(orphaned_ids),
        ).fetchone()[0]

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"{count} orphaned chunk(s) from {len(orphaned_ids)} deleted conversation(s)",
                fix_available=True,
                fix_command="siftd embed --rebuild",
                context={"chunk_count": count, "conversation_count": len(orphaned_ids)},
            )
        ]
