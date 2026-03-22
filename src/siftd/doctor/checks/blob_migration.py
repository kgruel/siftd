from siftd.doctor.checks import CheckContext, CheckCost, Finding


class BlobMigrationCheck:
    """Detects tool call results pending migration to content blobs."""

    name = "blob-migration"
    description = "Tool call results pending migration to content blobs"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.migrate_blobs import count_pending_migrations

        info = count_pending_migrations(ctx.get_db_conn())
        if info["total"] == 0:
            return []

        size_mb = (info["size_bytes"] or 0) / (1024 * 1024)
        return [
            Finding(
                check=self.name,
                severity="info",
                message=f"{info['total']} tool call result(s) ({size_mb:.1f}MB) pending blob migration",
                fix_available=True,
                fix_command="siftd migrate blobs",
                context=info,
            )
        ]
