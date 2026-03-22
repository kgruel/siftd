from siftd.doctor.checks import CheckContext, CheckCost, Finding


class SchemaCurrentCheck:
    """Checks if database schema is up to date with expected migrations."""

    name = "schema-current"
    description = "Database schema migrations are up to date"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sqlite import get_pending_schema_migrations

        pending = get_pending_schema_migrations(ctx.get_db_conn())
        if not pending:
            return []

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"{len(pending)} migration(s) pending: {', '.join(pending[:3])}"
                + ("..." if len(pending) > 3 else ""),
                fix_available=True,
                fix_command="siftd ingest",
                context={"pending": pending},
            )
        ]
