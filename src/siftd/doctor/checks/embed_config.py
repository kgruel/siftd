from siftd.doctor.checks import CheckContext, CheckCost, Finding

# Presets that speak to a self-hosted/local endpoint by design — no bearer key is
# expected for these, unlike the cloud providers (voyage/openai/gemini/jina/mistral).
_NO_AUTH_PRESETS = frozenset({"ollama", "custom"})


class EmbedConfigCheck:
    """Embedding backend usability and pending first-egress disclosure.

    Distinct from ``embeddings-compat`` (which compares a *built* index's stored
    metadata against the current config) and ``config-valid`` (which validates config
    *shape* — known backend name, positive dimensions, a resolvable api_key ref). This
    check catches the gap between the two: a backend that is syntactically valid but
    not actually usable (missing extra, missing key, bad preset requirement), surfaced
    whether or not an embeddings database has ever been built. It also surfaces the
    one-time remote first-egress disclosure so it doesn't go unnoticed on a headless
    ingest run.
    """

    name = "embed-config"
    description = "Embedding backend usability and pending egress disclosure"
    has_fix = True
    requires_db = False
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings.availability import embedding_status

        findings: list[Finding] = []

        status = embedding_status()
        configured = self._configured_backend_name()

        if not status.usable:
            if configured and configured != "off":
                # The reason string already names the fix for a config-shape problem
                # (unknown backend, missing base_url/model); the one case that needs a
                # different command is the local extra not being installed.
                fix_command = (
                    "siftd install embed"
                    if "extra is not installed" in status.reason
                    else "siftd config set embed.backend <value>"
                )
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Configured embedding backend unusable: {status.reason}",
                        fix_available=True,
                        fix_command=fix_command,
                        context={"backend": configured, "reason": status.reason},
                    )
                )
            elif not configured and not ctx.embed_db_path.exists():
                findings.append(
                    Finding(
                        check=self.name,
                        severity="info",
                        message="Semantic search not set up (no embedding backend configured)",
                        fix_available=True,
                        fix_command="siftd install embed",
                        context={"reason": status.reason},
                    )
                )
            return findings

        findings.extend(self._check_missing_api_key(configured))
        findings.extend(self._check_egress_notice(ctx))
        return findings

    def _configured_backend_name(self) -> str:
        from siftd.config import get_config

        return (get_config("embed.backend") or "").strip().lower()

    def _check_missing_api_key(self, configured: str) -> list[Finding]:
        """A cloud remote preset with no api_key set will fail at request time (401),
        not at config-validation time — ``get_preset``/``_build_remote`` allow an empty
        key so self-hosted presets (ollama/custom) keep working unauthenticated."""
        if not configured or configured in ("", "fastembed", "off", *_NO_AUTH_PRESETS):
            return []

        from siftd.embeddings.presets import get_preset

        if get_preset(configured) is None:
            return []  # unknown backend name — config-valid already flags this

        from siftd.config import get_config

        if (get_config("embed.api_key") or "").strip():
            return []

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=(
                    f"embed.backend = '{configured}' has no embed.api_key set; "
                    "requests will fail authentication"
                ),
                fix_available=True,
                fix_command="siftd config set embed.api_key <ref>",
                context={"backend": configured},
            )
        ]

    def _check_egress_notice(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings.egress import egress_notice_pending

        notice = egress_notice_pending(ctx.embed_db_path)
        if not notice:
            return []

        return [
            Finding(
                check=self.name,
                severity="info",
                message=f"Pending first-egress disclosure: {notice}",
                fix_available=True,
                fix_command="siftd embed",
                context={"notice": notice},
            )
        ]
