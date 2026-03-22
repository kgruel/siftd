from siftd.doctor.checks import CheckContext, CheckCost, Finding


class EmbeddingsAvailableCheck:
    """Reports embedding support installation status (informational only)."""

    name = "embeddings-available"
    description = "Embedding support installation status"
    has_fix = False
    requires_db = False
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        if embeddings_available():
            return []

        if ctx.embed_db_path.exists():
            return [
                Finding(
                    check=self.name,
                    severity="info",
                    message="Embeddings database exists but embedding support not installed",
                    fix_available=False,
                    context={"install_hint": "siftd install embed"},
                )
            ]

        return []
