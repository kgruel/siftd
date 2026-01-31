"""Doctor runner: orchestrates health checks."""

from pathlib import Path

from siftd.doctor.checks import (
    BUILTIN_CHECKS,
    CheckContext,
    CheckInfo,
    Finding,
)


def list_checks() -> list[CheckInfo]:
    """Return metadata about all available checks."""
    return [
        CheckInfo(
            name=check.name,
            description=check.description,
            has_fix=check.has_fix,
            requires_db=check.requires_db,
            requires_embed_db=check.requires_embed_db,
            cost=check.cost,
        )
        for check in BUILTIN_CHECKS
    ]


def run_checks(
    *,
    checks: list[str] | None = None,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
) -> list[Finding]:
    """Run health checks and return findings.

    Args:
        checks: Specific check names to run, or None for all.
        db_path: Main database path. Uses default if not specified.
        embed_db_path: Embeddings database path. Uses default if not specified.

    Returns:
        List of Finding objects from all checks.

    Raises:
        FileNotFoundError: If the main database doesn't exist.
        ValueError: If a specified check name doesn't exist.
    """
    from siftd.paths import (
        adapters_dir,
        formatters_dir,
        queries_dir,
    )
    from siftd.paths import (
        db_path as default_db_path,
    )
    from siftd.paths import (
        embeddings_db_path as default_embed_path,
    )

    # Resolve paths
    actual_db_path = db_path or default_db_path()
    actual_embed_path = embed_db_path or default_embed_path()

    # Filter checks if specific ones requested
    checks_to_run = BUILTIN_CHECKS
    if checks:
        available_names = {c.name for c in BUILTIN_CHECKS}
        unknown = set(checks) - available_names
        if unknown:
            raise ValueError(f"Unknown check(s): {', '.join(sorted(unknown))}")
        checks_to_run = [c for c in BUILTIN_CHECKS if c.name in checks]

    # Only require DB if any requested check needs it
    db_required = any(c.requires_db for c in checks_to_run)
    if db_required and not actual_db_path.exists():
        raise FileNotFoundError(
            f"Database not found: {actual_db_path}\nRun 'siftd ingest' to create it."
        )

    # Create context
    ctx = CheckContext(
        db_path=actual_db_path,
        embed_db_path=actual_embed_path,
        adapters_dir=adapters_dir(),
        formatters_dir=formatters_dir(),
        queries_dir=queries_dir(),
    )

    try:
        # Run all checks, collecting findings
        findings: list[Finding] = []
        for check in checks_to_run:
            try:
                check_findings = check.run(ctx)
                findings.extend(check_findings)
            except Exception as e:
                # Check itself failed - report as error finding
                findings.append(
                    Finding(
                        check=check.name,
                        severity="error",
                        message=f"Check failed to run: {e}",
                        fix_available=False,
                    )
                )

        return findings
    finally:
        ctx.close()
