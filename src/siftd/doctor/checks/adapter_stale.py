from siftd.doctor.checks import CheckContext, CheckCost, Finding

# Ignore sub-second mtime jitter between filesystems and recorded values.
_MTIME_TOLERANCE_SECONDS = 1.0


class AdapterStaleCheck:
    """Detects adapters whose newest on-disk file is newer than the last ingest.

    For each enabled adapter that has ingested files in the database, compares
    the newest file mtime reported by discover() against the newest recorded
    ingest mtime. A gap means already-ingested logs have grown (or new activity
    happened) since the last `siftd ingest`.

    Complements ingest-pending: that check catches *new* paths never ingested;
    this one catches *modified* files whose path is already recorded. Parse
    failures are reported separately by ingest-errors (the substrate records
    them in ingested_files.error) — not duplicated here.

    Disabled adapters are skipped automatically: load_all_adapters() filters
    them at the single assembly point.
    """

    name = "adapter-stale"
    description = "Adapters with on-disk files newer than the last ingest"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "slow"  # Runs discover() on all adapters

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.adapters.registry import load_all_adapters

        conn = ctx.get_db_conn()

        # file_mtime arrived by migration; older DBs can't compare.
        cur = conn.execute("PRAGMA table_info(ingested_files)")
        columns = {row[1] for row in cur.fetchall()}
        if "file_mtime" not in columns:
            return []

        cur = conn.execute(
            """SELECT h.name AS harness_name, f.path, f.file_mtime
               FROM ingested_files f
               JOIN harnesses h ON h.id = f.harness_id
               WHERE f.file_mtime IS NOT NULL"""
        )
        ingested_mtimes: dict[str, dict[str, float]] = {}
        for row in cur.fetchall():
            ingested_mtimes.setdefault(row["harness_name"], {})[row["path"]] = row[
                "file_mtime"
            ]

        findings = []
        for plugin in load_all_adapters():
            adapter_mtimes = ingested_mtimes.get(plugin.name)
            if not adapter_mtimes:
                continue  # No DB presence for this adapter — nothing to compare.

            try:
                discovered = list(plugin.module.discover())
            except Exception:
                # ingest-pending already surfaces discover() failures; don't
                # double-report from here.
                continue

            stale_paths: list[tuple[str, float, float]] = []
            for source in discovered:
                path = str(source.location)
                ingested_mtime = adapter_mtimes.get(path)
                if ingested_mtime is None:
                    continue  # New paths are reported by ingest-pending.
                try:
                    disk_mtime = source.as_path.stat().st_mtime
                except OSError:
                    continue
                gap = disk_mtime - ingested_mtime
                if gap > _MTIME_TOLERANCE_SECONDS:
                    stale_paths.append((path, disk_mtime, ingested_mtime))

            if stale_paths:
                path, disk_mtime, ingested_mtime = max(
                    stale_paths, key=lambda item: item[1] - item[2]
                )
                gap = disk_mtime - ingested_mtime
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=(
                            f"Adapter '{plugin.name}': newest file on disk is "
                            f"{gap:.0f}s newer than the last ingest"
                        ),
                        fix_available=True,
                        fix_command="siftd ingest",
                        context={
                            "adapter": plugin.name,
                            "path": path,
                            "stale_file_count": len(stale_paths),
                            "newest_file_mtime": disk_mtime,
                            "newest_ingested_mtime": ingested_mtime,
                            "gap_seconds": gap,
                        },
                    )
                )

        return findings
