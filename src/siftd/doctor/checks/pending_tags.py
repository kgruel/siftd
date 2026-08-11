from siftd.doctor.checks import CheckContext, CheckCost, Finding


class PendingTagsCheck:
    """Detects queued session tags that are waiting to be applied.

    ``siftd tag --session`` queues a tag to be applied when that session is
    next ingested. A session whose transcript has settled never re-ingests,
    so its queued tags stay queued — this check surfaces them, and the fix
    applies them to the conversation the session became.
    """

    name = "pending-tags"
    description = "Queued session tags waiting to be applied"
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
                    message=(
                        f"{orphaned} queued tag(s) not yet applied — the fix applies "
                        "the ones whose session has been ingested"
                    ),
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
                    message=f"{stale} session registration(s) idle for over 48 hours — the fix prunes them",
                    fix_available=True,
                    fix_command="siftd doctor fix --pending-tags",
                    context={"stale_count": stale},
                )
            )

        return findings
