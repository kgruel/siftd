from siftd.doctor.checks import CheckContext, CheckCost, Finding


class PendingTagsCheck:
    """Detects orphaned pending tags for sessions that may never be ingested."""

    name = "pending-tags"
    description = "Pending tags for sessions that may never be ingested"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sessions import (
            get_orphaned_pending_tags_count,
            get_stale_sessions_count,
        )

        findings = []
        conn = ctx.get_db_conn()

        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_tags'"
        )
        if not cur.fetchone():
            return []

        orphaned = get_orphaned_pending_tags_count(conn)
        if orphaned > 0:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"{orphaned} pending tag(s) for unregistered sessions",
                    fix_available=True,
                    fix_command="siftd doctor fix --pending-tags",
                    context={"orphaned_count": orphaned},
                )
            )

        stale = get_stale_sessions_count(conn, max_age_hours=48)
        if stale > 0:
            findings.append(
                Finding(
                    check=self.name,
                    severity="info",
                    message=f"{stale} active session(s) older than 48 hours",
                    fix_available=True,
                    fix_command="siftd doctor fix --pending-tags",
                    context={"stale_count": stale},
                )
            )

        return findings
