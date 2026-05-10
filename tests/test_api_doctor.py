"""Tests for siftd.api.doctor — wrapper re-exports from siftd.doctor."""

from siftd.api.doctor import CheckInfo, Finding, list_checks, run_checks


def test_re_exports_are_correct_types():
    """Verify api.doctor re-exports the expected symbols from siftd.doctor."""
    from siftd.doctor import CheckInfo as _CI, Finding as _F, list_checks as _lc, run_checks as _rc

    assert CheckInfo is _CI
    assert Finding is _F
    assert list_checks is _lc
    assert run_checks is _rc


def test_list_checks_returns_check_info_list():
    """list_checks returns a non-empty list of CheckInfo objects."""
    checks = list_checks()
    assert isinstance(checks, list)
    assert len(checks) > 0
    for c in checks:
        assert isinstance(c, CheckInfo)
        assert isinstance(c.name, str) and c.name
        assert isinstance(c.description, str)
        assert isinstance(c.has_fix, bool)
        assert isinstance(c.requires_db, bool)
        assert c.cost in ("fast", "slow", "deep")


def test_finding_dataclass_fields():
    """Finding can be constructed with required fields."""
    f = Finding(
        check="test-check",
        severity="warning",
        message="something is off",
        fix_available=True,
        fix_command="siftd ingest",
    )
    assert f.check == "test-check"
    assert f.severity == "warning"
    assert f.fix_available is True
    assert f.fix_command == "siftd ingest"
    assert f.context is None


def test_check_info_dataclass_fields():
    """CheckInfo can be constructed with all fields."""
    ci = CheckInfo(
        name="my-check",
        description="does stuff",
        has_fix=False,
        requires_db=True,
        requires_embed_db=False,
        cost="fast",
    )
    assert ci.name == "my-check"
    assert ci.requires_db is True
    assert ci.requires_embed_db is False


def test_run_checks_invalid_name_raises():
    """run_checks raises ValueError for non-existent check names."""
    import pytest

    with pytest.raises(ValueError, match="Unknown"):
        run_checks(checks=["nonexistent-check-xyz"])


def test_run_checks_fast_flag(test_db):
    """run_checks with fast=True runs only fast checks."""
    findings = run_checks(db_path=test_db, fast=True)
    assert isinstance(findings, list)
    # Verify all findings are from fast checks
    checks = list_checks()
    fast_check_names = {c.name for c in checks if c.cost == "fast"}
    for f in findings:
        assert f.check in fast_check_names, f"Check {f.check} is not a fast check"
    # Verify that slow and deep checks are not included
    all_finding_names = {f.check for f in findings}
    slow_check_names = {c.name for c in checks if c.cost == "slow"}
    deep_check_names = {c.name for c in checks if c.cost == "deep"}
    assert not all_finding_names.intersection(slow_check_names), "Found slow checks when using fast=True"
    assert not all_finding_names.intersection(deep_check_names), "Found deep checks when using fast=True"
