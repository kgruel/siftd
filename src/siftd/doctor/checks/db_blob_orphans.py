from siftd.doctor.checks import CheckContext, CheckCost, Finding


class DbBlobOrphansCheck:
    """Detects content_blobs with ref_count == 0 that were not GC'd by triggers."""

    name = "db-blob-orphans"
    description = "content_blobs with ref_count=0 not garbage-collected by triggers"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "deep"

    def run(self, ctx: CheckContext) -> list[Finding]:
        conn = ctx.get_db_conn()

        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_blobs'"
        ).fetchone()
        if not exists:
            return []

        rows = conn.execute(
            "SELECT hash FROM content_blobs WHERE ref_count = 0"
        ).fetchall()
        if not rows:
            return []

        count = len(rows)
        return [
            Finding(
                check=self.name,
                severity="info",
                message=f"{count} blob(s) with ref_count=0 not yet GC'd",
                fix_available=True,
                fix_command="siftd doctor fix --blob-refcount",
                context={"count": count},
            )
        ]
