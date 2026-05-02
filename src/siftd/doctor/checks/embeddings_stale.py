from siftd.doctor.checks import CheckContext, CheckCost, Finding


class EmbeddingsStaleCheck:
    """Detects conversations not indexed in embeddings database."""

    name = "embeddings-stale"
    description = "Conversations not indexed in embeddings database"
    has_fix = True
    requires_db = True
    requires_embed_db = True
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        if not embeddings_available():
            return []

        if not ctx.embed_db_path.exists():
            return [
                Finding(
                    check=self.name,
                    severity="info",
                    message="Embeddings database not found (not yet created)",
                    fix_available=True,
                    fix_command="siftd search --index",
                )
            ]

        conn = ctx.get_db_conn()
        embed_conn = ctx.get_embed_conn()

        cur = conn.execute("SELECT DISTINCT conversation_id FROM events WHERE kind = 'prompt'")
        main_ids = {row[0] for row in cur.fetchall()}

        from siftd.storage.embeddings import get_indexed_conversation_ids

        indexed_ids = get_indexed_conversation_ids(embed_conn)
        stale_ids = main_ids - indexed_ids

        if stale_ids:
            return [
                Finding(
                    check=self.name,
                    severity="info",
                    message=f"{len(stale_ids)} conversation(s) not indexed in embeddings",
                    fix_available=True,
                    fix_command="siftd search --index",
                    context={"count": len(stale_ids)},
                )
            ]

        return []
