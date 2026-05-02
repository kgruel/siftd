from siftd.doctor.checks import CheckContext, CheckCost, Finding

_DRIFT_SQL = """
SELECT cb.hash, cb.ref_count, COALESCE(actual.cnt, 0) AS actual
FROM content_blobs cb
LEFT JOIN (
    SELECT result_hash, COUNT(*) cnt FROM event_tool_call
    WHERE result_hash IS NOT NULL GROUP BY result_hash
) actual ON actual.result_hash = cb.hash
WHERE cb.ref_count != COALESCE(actual.cnt, 0)
"""


class DbBlobRefcountDriftCheck:
    """Detects content_blobs where ref_count diverges from actual event_tool_call fan-in."""

    name = "db-blob-refcount-drift"
    description = "content_blobs ref_count out of sync with event_tool_call references"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "deep"

    def run(self, ctx: CheckContext) -> list[Finding]:
        conn = ctx.get_db_conn()

        # Check table exists before querying
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_blobs'"
        ).fetchone()
        if not exists:
            return []

        rows = conn.execute(_DRIFT_SQL).fetchmany(21)
        if not rows:
            return []

        total = len(rows)
        if total > 20:
            return [
                Finding(
                    check=self.name,
                    severity="warning",
                    message="More than 20 blobs have drifted ref_count; run fix to repair",
                    fix_available=True,
                    fix_command="siftd doctor fix --blob-refcount",
                    context={"total_ge": 21},
                )
            ]

        findings = []
        for row in rows:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Blob {row[0][:12]}… ref_count={row[1]} but actual={row[2]}",
                    fix_available=True,
                    fix_command="siftd doctor fix --blob-refcount",
                    context={"hash": row[0], "ref_count": row[1], "actual": row[2]},
                )
            )
        return findings
