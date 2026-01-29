"""Doctor runner: orchestrates health checks."""

from pathlib import Path

from siftd.doctor.checks import (
    BUILTIN_CHECKS,
    CheckContext,
    CheckInfo,
    Finding,
    FixResult,
)


def list_checks() -> list[CheckInfo]:
    """Return metadata about all available checks."""
    return [
        CheckInfo(
            name=check.name,
            description=check.description,
            has_fix=check.has_fix,
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

    # Check main DB exists
    if not actual_db_path.exists():
        raise FileNotFoundError(
            f"Database not found: {actual_db_path}\nRun 'siftd ingest' to create it."
        )

    # Filter checks if specific ones requested
    checks_to_run = BUILTIN_CHECKS
    if checks:
        available_names = {c.name for c in BUILTIN_CHECKS}
        unknown = set(checks) - available_names
        if unknown:
            raise ValueError(f"Unknown check(s): {', '.join(sorted(unknown))}")
        checks_to_run = [c for c in BUILTIN_CHECKS if c.name in checks]

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


def apply_fix(finding: Finding) -> FixResult:
    """Apply fix for a finding (if available).

    Note: v1 fixes are "report the command" not "execute it".

    Args:
        finding: The finding to fix.

    Returns:
        FixResult indicating success/failure.
    """
    if not finding.fix_available:
        return FixResult(success=False, message="No fix available for this finding")

    if not finding.fix_command:
        return FixResult(success=False, message="Fix available but no command specified")

    # v1: Just report the command, don't execute
    return FixResult(
        success=True,
        message=f"To fix: {finding.fix_command}",
    )
