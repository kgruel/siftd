from siftd.doctor.checks import CheckContext, CheckCost, Finding


class EmbeddingsCompatCheck:
    """Validates embedding index compatibility with current backend configuration."""

    name = "embeddings-compat"
    description = "Embedding index matches current backend configuration"
    has_fix = True
    requires_db = False
    requires_embed_db = True
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        if not embeddings_available():
            return []

        if not ctx.embed_db_path.exists():
            return []

        from siftd.embeddings import SCHEMA_VERSION, get_backend
        from siftd.storage.embeddings import get_meta

        embed_conn = ctx.get_embed_conn()
        findings = []

        stored_schema = get_meta(embed_conn, "schema_version")
        stored_model = get_meta(embed_conn, "model")

        if stored_schema is None or stored_model is None:
            missing = []
            if stored_schema is None:
                missing.append("schema_version")
            if stored_model is None:
                missing.append("model")
            findings.append(
                Finding(
                    check=self.name,
                    severity="info",
                    message=f"Index missing compatibility metadata: {', '.join(missing)}",
                    fix_available=True,
                    fix_command="siftd search --rebuild",
                    context={"missing_keys": missing},
                )
            )

        try:
            backend = get_backend(verbose=False)
        except RuntimeError:
            return findings

        stored_backend = get_meta(embed_conn, "backend")
        stored_dimension = get_meta(embed_conn, "dimension")

        if stored_schema is not None:
            stored_ver = int(stored_schema)
            if stored_ver != SCHEMA_VERSION:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="info",
                        message=f"Index schema outdated (v{stored_ver} → v{SCHEMA_VERSION})",
                        fix_available=True,
                        fix_command="siftd search --rebuild",
                        context={"stored_version": stored_ver, "current_version": SCHEMA_VERSION},
                    )
                )

        if stored_backend is not None and stored_backend != backend.name:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Backend mismatch: index={stored_backend}, current={backend.name}",
                    fix_available=True,
                    fix_command="siftd search --rebuild",
                    context={"stored_backend": stored_backend, "current_backend": backend.name},
                )
            )
        elif stored_model is not None and stored_model != backend.model:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Model mismatch: index={stored_model}, current={backend.model}",
                    fix_available=True,
                    fix_command="siftd search --rebuild",
                    context={"stored_model": stored_model, "current_model": backend.model},
                )
            )

        if stored_dimension is not None:
            stored_dim = int(stored_dimension)
            if stored_dim != backend.dimension:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Dimension mismatch: index={stored_dim}, current={backend.dimension}",
                        fix_available=True,
                        fix_command="siftd search --rebuild",
                        context={"stored_dimension": stored_dim, "current_dimension": backend.dimension},
                    )
                )

        return findings
