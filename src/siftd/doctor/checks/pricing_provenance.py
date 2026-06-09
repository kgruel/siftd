from siftd.doctor.checks import CheckContext, CheckCost, Finding


class PricingProvenanceCheck:
    """Flags priced models whose price has no reference provenance.

    The pricing table is a projection of the version-controlled reference
    (siftd/data/pricing.toml). A priced row with ``source IS NULL`` got its value
    by some other path (historically, sync from another machine) — it is not
    governed by the reference, so it could be wrong and a fresh machine would not
    have it. The fix is to verify the published price and add it to the reference.
    Distinct from cost-coverage, which flags UNPRICED (cost NULL) models.
    """

    name = "pricing-provenance"
    description = "Priced models lacking version-controlled reference provenance"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sqlite import get_priced_models_without_provenance

        rows = get_priced_models_without_provenance(ctx.get_db_conn())
        if not rows:
            return []

        models = ", ".join(f"{r['model_name']} ({r['provider_name']})" for r in rows)
        return [
            Finding(
                check=self.name,
                severity="warning",
                message=(
                    f"{len(rows)} priced model(s) lack reference provenance: {models}. "
                    f"Verify the published price and add it to siftd/data/pricing.toml "
                    f"(or ~/.config/siftd/pricing.toml), then run 'siftd backfill --pricing'."
                ),
                fix_available=False,
                context={"models": rows},
            )
        ]
