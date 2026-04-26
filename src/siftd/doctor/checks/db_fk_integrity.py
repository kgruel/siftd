from siftd.doctor.checks import CheckContext, CheckCost, Finding


class DbFkIntegrityCheck:
    """Detects foreign key violations in the main database."""

    name = "db-fk-integrity"
    description = "Foreign key constraint violations in the main database"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "deep"

    def run(self, ctx: CheckContext) -> list[Finding]:
        conn = ctx.get_db_conn()
        rows = conn.execute("PRAGMA foreign_key_check").fetchmany(51)
        if not rows:
            return []

        if len(rows) > 50:
            return [
                Finding(
                    check=self.name,
                    severity="error",
                    message=(
                        "More than 50 FK violations detected; database may be severely "
                        "corrupt. Run `PRAGMA foreign_key_check` directly for the full list."
                    ),
                    fix_available=False,
                    context={"total_ge": 51},
                )
            ]

        total = len(rows)
        capped = rows[:5]
        shown = [
            {"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]}
            for r in capped
        ]
        more = total - len(shown)
        suffix = f" (showing 5 of {total})" if more > 0 else ""
        tables = ", ".join(sorted({r[0] for r in capped}))

        return [
            Finding(
                check=self.name,
                severity="error",
                message=f"{total} FK violation(s) in: {tables}{suffix}",
                fix_available=False,
                context={"violations": shown, "total": total},
            )
        ]
