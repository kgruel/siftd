from siftd.doctor.checks import CheckContext, CheckCost, Finding


class CostCoverageCheck:
    """Flags when significant token volume has no cost data."""

    name = "cost-coverage"
    description = "Conversations with tokens but missing cost data"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    _WARNING_THRESHOLD = 25  # warn if fewer than 25% of token-bearing convs have cost

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.conversation_stats import get_cost_coverage

        coverage = get_cost_coverage(ctx.get_db_conn())
        if coverage is None or coverage.total_with_tokens == 0:
            return []

        if coverage.pct_covered >= self._WARNING_THRESHOLD:
            return []

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=(
                    f"Only {coverage.pct_covered:.0f}% of {coverage.total_with_tokens} conversations "
                    f"have cost data ({coverage.with_null_cost} missing pricing, run siftd ingest to rebuild stats)"
                ),
                fix_available=False,
                context={
                    "with_tokens": coverage.total_with_tokens,
                    "with_cost": coverage.with_positive_cost,
                    "null_cost": coverage.with_null_cost,
                    "pct_covered": coverage.pct_covered,
                },
            )
        ]
