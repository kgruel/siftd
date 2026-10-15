import os

from siftd.doctor.checks import CheckContext, CheckCost, Finding


class IngestErrorsCheck:
    """Reports files that failed ingestion, with the remedy.

    The remedy is a plain ``siftd ingest``: every run re-examines a row that
    carries an error, whatever its stat says (``_stat_unchanged`` in
    ``ingestion/orchestration.py``), so a failure whose cause has since been
    fixed clears on the next run. A row that survives it is a file the
    adapter still cannot parse.

    That only reaches files still on disk. Nothing prunes the row of a file
    that has since been deleted, so those are counted apart and offered no
    fix: re-ingesting cannot clear them, and ``doctor fix`` would loop.
    """

    name = "ingest-errors"
    description = "Files that failed ingestion (recorded with error)"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sqlite import get_ingest_errors

        errors = get_ingest_errors(ctx.get_db_conn())
        if not errors:
            return []

        by_harness: dict[str, list[dict]] = {}
        for e in errors:
            by_harness.setdefault(e["harness_name"], []).append(e)

        findings = []
        for name, rows in by_harness.items():
            missing = sum(1 for r in rows if not os.path.exists(r["path"]))
            retryable = len(rows) - missing
            parts = []
            if retryable:
                parts.append(f"{retryable} retried on every ingest")
            if missing:
                parts.append(f"{missing} no longer on disk")
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=(
                        f"Adapter '{name}': {len(rows)} file(s) failed ingestion "
                        f"({', '.join(parts)})"
                    ),
                    fix_available=bool(retryable),
                    fix_command="siftd ingest" if retryable else None,
                    context={
                        "adapter": name,
                        "count": len(rows),
                        "missing": missing,
                        "errors": [r["error"] for r in rows[:5]],
                    },
                )
            )
        return findings
