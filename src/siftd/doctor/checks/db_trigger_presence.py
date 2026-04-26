from siftd.doctor.checks import CheckContext, CheckCost, Finding

_REQUIRED_TRIGGERS = (
    "tr_tool_calls_delete_release_blob",
    "tr_tool_calls_update_release_blob",
)


class DbTriggerPresenceCheck:
    """Asserts that blob ref-count triggers exist in sqlite_master."""

    name = "db-trigger-presence"
    description = "Blob ref-count triggers present in sqlite_master"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "deep"

    def run(self, ctx: CheckContext) -> list[Finding]:
        conn = ctx.get_db_conn()
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        missing = [t for t in _REQUIRED_TRIGGERS if t not in existing]
        if not missing:
            return []

        return [
            Finding(
                check=self.name,
                severity="error",
                message=f"Missing trigger(s): {', '.join(missing)}",
                fix_available=True,
                fix_command="siftd doctor fix --triggers",
                context={"missing": missing},
            )
        ]
