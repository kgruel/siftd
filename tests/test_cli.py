"""CLI smoke tests — verify commands parse and run without import errors."""

import pytest

from strata.cli import main


def test_help_exits_zero():
    """strata --help exits with code 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_status_with_db(test_db):
    """strata --db <path> status runs successfully."""
    rc = main(["--db", str(test_db), "status"])
    assert rc == 0


def test_query_with_db(test_db):
    """strata --db <path> query lists conversations."""
    rc = main(["--db", str(test_db), "query"])
    assert rc == 0


def test_unknown_subcommand():
    """Unknown subcommand prints help and exits non-zero."""
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-command"])
    assert exc_info.value.code != 0
