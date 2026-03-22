from siftd.doctor.checks import CheckContext, CheckCost, Finding


class PricingGapsCheck:
    """Detects models used in responses without pricing data."""

    name = "pricing-gaps"
    description = "Models used in responses without pricing data"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sqlite import get_models_without_pricing

        missing = get_models_without_pricing(ctx.get_db_conn())
        if not missing:
            return []

        model_list = [f"{m['provider_name']}/{m['model_name']}" for m in missing]
        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"{len(missing)} model(s) without pricing: {', '.join(model_list[:5])}"
                + ("..." if len(missing) > 5 else ""),
                fix_available=False,
                context={"models": model_list},
            )
        ]
