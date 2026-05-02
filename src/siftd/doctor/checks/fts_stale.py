from siftd.doctor.checks import CheckContext, CheckCost, Finding


class FtsStaleCheck:
    """Detects FTS5 index out of sync with main content tables."""

    name = "fts-stale"
    description = "FTS5 search index out of sync with content tables"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.fts import get_fts_sync_status

        status = get_fts_sync_status(ctx.get_db_conn())
        total = status["orphaned_count"] + status["missing_count"]
        if total == 0:
            return []

        parts = []
        if status["orphaned_count"] > 0:
            parts.append(f"{status['orphaned_count']} orphaned")
        if status["missing_count"] > 0:
            parts.append(f"{status['missing_count']} missing")

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"FTS index out of sync: {', '.join(parts)} entries",
                fix_available=True,
                fix_command="siftd ingest --rebuild-fts",
                context=status,
            )
        ]
