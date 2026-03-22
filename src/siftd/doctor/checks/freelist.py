from siftd.doctor.checks import CheckContext, CheckCost, Finding


class FreelistCheck:
    """Reports SQLite freelist pages that could be reclaimed with VACUUM."""

    name = "freelist"
    description = "SQLite freelist pages (reclaimable with VACUUM)"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sqlite import get_freelist_info

        info = get_freelist_info(ctx.get_db_conn())
        if info["freelist_count"] == 0:
            return []

        wasted_bytes = info["freelist_count"] * info["page_size"]
        if wasted_bytes < 1024 * 1024:
            wasted_str = f"{wasted_bytes / 1024:.0f}KB"
        else:
            wasted_str = f"{wasted_bytes / (1024 * 1024):.1f}MB"

        pct = (info["freelist_count"] / info["page_count"] * 100) if info["page_count"] > 0 else 0

        return [
            Finding(
                check=self.name,
                severity="info",
                message=f"{info['freelist_count']} free page(s) ({wasted_str}, {pct:.0f}% of DB) could be reclaimed",
                fix_available=False,
                context={**info, "wasted_bytes": wasted_bytes, "tip": f"sqlite3 {ctx.db_path} 'VACUUM'"},
            )
        ]
