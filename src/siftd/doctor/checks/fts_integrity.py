import sqlite3

from siftd.doctor.checks import CheckContext, CheckCost, Finding


class FtsIntegrityCheck:
    """Checks FTS5 table integrity for corruption."""

    name = "fts-integrity"
    description = "FTS5 search index integrity"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sqlite import open_database

        conn = ctx.get_db_conn()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
        )
        if not cur.fetchone():
            return []

        try:
            write_conn = open_database(ctx.db_path, read_only=False)
        except Exception as e:
            return [
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Cannot check FTS integrity (read-only): {e}",
                    fix_available=False,
                    context={"error": str(e)},
                )
            ]

        try:
            write_conn.execute("INSERT INTO content_fts(content_fts) VALUES('integrity-check')")
            return []
        except sqlite3.IntegrityError as e:
            return [
                Finding(
                    check=self.name,
                    severity="error",
                    message=f"FTS5 index corruption detected: {e}",
                    fix_available=True,
                    fix_command="siftd ingest --rebuild-fts",
                    context={"error": str(e)},
                )
            ]
        finally:
            write_conn.close()
