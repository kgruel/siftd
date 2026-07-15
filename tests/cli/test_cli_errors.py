"""The main() taxonomy backstop: SiftdError renders clean, never a traceback.

These tests go through the argparse layer (main(argv)) deliberately — the
IndexCompatError bug this guards against lived exactly in the gap between the
api layer raising and per-command catch tuples: only the real dispatch path
exercises the backstop.
"""

import pytest

from siftd.cli import main
from siftd.errors import DriftError, SiftdError, UserInputError


def _raise_from_dispatch(monkeypatch, exc: Exception) -> None:
    """Simulate an error propagating out of Operation execution — the exact
    path the v1-index traceback escaped through (cli/search.py:272)."""

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr("siftd.api.dispatch.execute_for_render", boom)


class TestSiftdErrorBackstop:
    def test_index_compat_error_renders_clean_exit_1(self, monkeypatch, capsys, test_db):
        # The regression that motivated the taxonomy: a stale embeddings index
        # surfacing as a raw traceback from `siftd search`.
        from siftd.storage.embeddings import IndexCompatError

        _raise_from_dispatch(
            monkeypatch,
            IndexCompatError(
                "Embeddings index needs rebuilding.\n\n  siftd embed --rebuild"
            ),
        )
        rc = main(["--db", str(test_db), "search", "product plan diff"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "Embeddings index needs rebuilding" in err
        assert "Traceback" not in err

    def test_user_input_error_exits_2(self, monkeypatch, capsys, test_db):
        _raise_from_dispatch(monkeypatch, UserInputError("bad anchor: turn 99 of 3"))
        rc = main(["--db", str(test_db), "search", "q"])
        assert rc == 2
        assert "bad anchor" in capsys.readouterr().err

    def test_bare_drift_error_exits_1(self, monkeypatch, capsys, test_db):
        _raise_from_dispatch(monkeypatch, DriftError("index drifted; rebuild"))
        rc = main(["--db", str(test_db), "search", "q"])
        assert rc == 1
        assert "rebuild" in capsys.readouterr().err

    def test_schema_upgrade_error_still_caught(self, monkeypatch, capsys, test_db):
        # Regression guard: the old main() special case dissolved into the
        # backstop; the message-not-traceback behavior must survive that.
        from siftd.storage.sqlite import SchemaUpgradeRequiredError

        _raise_from_dispatch(
            monkeypatch, SchemaUpgradeRequiredError("database schema requires upgrade")
        )
        rc = main(["--db", str(test_db), "search", "q"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "schema requires upgrade" in err
        assert "Traceback" not in err

    def test_non_taxonomy_exception_still_propagates(self, monkeypatch, test_db):
        # Invariant violations stay loud: the backstop must not become a
        # catch-all that eats bugs.
        _raise_from_dispatch(monkeypatch, ZeroDivisionError("bug"))
        with pytest.raises(ZeroDivisionError):
            main(["--db", str(test_db), "search", "q"])


class TestTaxonomyShape:
    def test_branches_are_siftd_errors(self):
        assert issubclass(UserInputError, SiftdError)
        assert issubclass(DriftError, SiftdError)

    def test_index_compat_error_joined(self):
        from siftd.storage.embeddings import IndexCompatError

        assert issubclass(IndexCompatError, DriftError)

    def test_migrated_exceptions_shed_builtin_bases(self):
        # Slice 5 shed the transitional RuntimeError/ValueError dual bases:
        # taxonomy membership is now the only routing. A builtin base
        # reappearing here means a catch tuple somewhere is depending on it
        # again — the disease the taxonomy exists to cure.
        from siftd.api.database import PreflightError
        from siftd.api.ingest import AdapterSelectionError
        from siftd.api.search import EmbeddingsRequiredError
        from siftd.credentials import TokenRefError
        from siftd.storage.sqlite import SchemaUpgradeRequiredError

        for exc in (SchemaUpgradeRequiredError, PreflightError):
            assert issubclass(exc, SiftdError)
            assert not issubclass(exc, RuntimeError)
        for exc in (EmbeddingsRequiredError, AdapterSelectionError, TokenRefError):
            assert issubclass(exc, SiftdError)
            assert not issubclass(exc, ValueError)
