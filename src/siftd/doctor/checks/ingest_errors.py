from siftd.doctor.checks import CheckContext, CheckCost, Finding


class IngestErrorsCheck:
    """Reports files that failed ingestion."""

    name = "ingest-errors"
    description = "Files that failed ingestion (recorded with error)"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sqlite import get_ingest_errors

        errors = get_ingest_errors(ctx.get_db_conn())
        if not errors:
            return []

        by_harness: dict[str, list[str]] = {}
        for e in errors:
            by_harness.setdefault(e["harness_name"], []).append(e["error"])

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"Adapter '{name}': {len(errs)} file(s) failed ingestion",
                fix_available=False,
                context={"adapter": name, "count": len(errs), "errors": errs[:5]},
            )
            for name, errs in by_harness.items()
        ]
