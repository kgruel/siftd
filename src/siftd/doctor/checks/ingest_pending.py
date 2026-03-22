from siftd.doctor.checks import CheckContext, CheckCost, Finding


class IngestPendingCheck:
    """Detects files discovered by adapters but not yet ingested."""

    name = "ingest-pending"
    description = "Files discovered by adapters but not yet ingested"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "slow"  # Runs discover() on all adapters

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.adapters.registry import load_all_adapters

        findings = []
        plugins = load_all_adapters()
        conn = ctx.get_db_conn()

        # Get all ingested file paths
        cur = conn.execute("SELECT path FROM ingested_files")
        ingested_paths = {row[0] for row in cur.fetchall()}

        for plugin in plugins:
            adapter = plugin.module
            try:
                discovered = list(adapter.discover())
            except Exception as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Adapter '{plugin.name}' discover() failed: {e}",
                        fix_available=False,
                    )
                )
                continue

            # Find files not in ingested_files
            pending = []
            for source in discovered:
                path_str = str(source.location)
                if path_str not in ingested_paths:
                    pending.append(path_str)

            if pending:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="info",
                        message=f"Adapter '{plugin.name}': {len(pending)} file(s) pending ingestion",
                        fix_available=True,
                        fix_command="siftd ingest",
                        context={"adapter": plugin.name, "count": len(pending)},
                    )
                )

        return findings
