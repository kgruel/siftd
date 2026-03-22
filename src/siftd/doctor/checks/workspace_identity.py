from siftd.doctor.checks import CheckContext, CheckCost, Finding


class WorkspaceIdentityCheck:
    """Detects workspaces without git remote identity or potential duplicates."""

    name = "workspace-identity"
    description = "Workspace identity via git remote (dedup detection)"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.migrate_workspaces import verify_workspace_identity

        info = verify_workspace_identity(ctx.get_db_conn())
        findings = []

        if info["without_remote"] > 0:
            findings.append(
                Finding(
                    check=self.name,
                    severity="info",
                    message=f"{info['without_remote']} workspace(s) without git remote identity",
                    fix_available=True,
                    fix_command="siftd backfill git-remote",
                    context=info,
                )
            )

        if info["duplicate_groups"] > 0:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"{info['duplicate_groups']} workspace group(s) may be duplicates (same git remote)",
                    fix_available=True,
                    fix_command="siftd migrate merge-workspaces",
                    context=info,
                )
            )

        return findings
