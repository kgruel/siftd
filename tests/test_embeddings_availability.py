"""Embedding availability — status-driven (config/installed), not reachability."""

import pytest

import siftd.embeddings.availability as avail
from siftd.embeddings.availability import (
    EmbeddingsNotAvailable,
    EmbedStatus,
    embedding_status,
    embeddings_available,
    require_embeddings,
)


def _cfg(monkeypatch, **values):
    """Point config + resolver at an in-memory [embed] table."""
    import siftd.config as config
    import siftd.embeddings.base as base

    monkeypatch.setattr(config, "get_config", lambda key: values.get(key))
    base.invalidate_backend_cache()


class TestEmbeddingStatus:
    def test_off_is_unusable(self, monkeypatch):
        _cfg(monkeypatch, **{"embed.backend": "off"})
        st = embedding_status()
        assert st.usable is False
        assert st.backend is None
        assert "off" in st.reason

    def test_remote_configured_is_usable_without_fastembed(self, monkeypatch):
        # Configuration alone makes a remote backend available — no fastembed, no network.
        _cfg(monkeypatch, **{"embed.backend": "voyage", "embed.api_key": "sk-literal"})
        monkeypatch.setattr(avail, "_fastembed_importable", lambda: False)
        st = embedding_status()
        assert st.usable is True
        assert st.backend == "remote:voyage"
        assert embeddings_available() is True

    def test_remote_misconfigured_is_unusable_with_reason(self, monkeypatch):
        # ollama preset without embed.model → config error surfaces as an actionable reason.
        _cfg(monkeypatch, **{"embed.backend": "ollama"})
        st = embedding_status()
        assert st.usable is False
        assert "model" in st.reason

    def test_unset_without_fastembed_is_unusable(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.setattr(avail, "_fastembed_importable", lambda: False)
        st = embedding_status()
        assert st.usable is False
        assert st.backend is None
        assert "install" in st.reason.lower()

    def test_unset_with_fastembed_is_usable(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.setattr(avail, "_fastembed_importable", lambda: True)
        st = embedding_status()
        assert st.usable is True
        assert st.backend == "fastembed"

    def test_embeddings_available_returns_bool(self, monkeypatch):
        _cfg(monkeypatch, **{"embed.backend": "off"})
        assert embeddings_available() is False


class TestRequireEmbeddings:
    def test_passes_when_usable(self, monkeypatch):
        monkeypatch.setattr(avail, "embedding_status", lambda: EmbedStatus("fastembed", True, "ok"))
        require_embeddings("test operation")  # no raise

    def test_raises_with_reason_when_unusable(self, monkeypatch):
        monkeypatch.setattr(
            avail, "embedding_status", lambda: EmbedStatus(None, False, "no backend configured")
        )
        with pytest.raises(EmbeddingsNotAvailable) as exc_info:
            require_embeddings("Semantic search")
        assert "Semantic search" in str(exc_info.value)
        assert "no backend configured" in str(exc_info.value)
        assert "siftd install embed" in str(exc_info.value)


class TestEmbeddingsModuleExports:
    def test_availability_names_always_importable(self):
        from siftd.embeddings import (
            EmbeddingsNotAvailable,
            EmbedStatus,
            embedding_status,
            embeddings_available,
            require_embeddings,
        )

        assert callable(embeddings_available)
        assert callable(embedding_status)
        assert callable(require_embeddings)
        assert issubclass(EmbeddingsNotAvailable, Exception)
        assert EmbedStatus is not None

    def test_backend_symbols_live_in_submodules_not_package_root(self):
        import siftd.embeddings as emb

        # Backend/index symbols are imported from concrete submodules to keep numpy off
        # the light CLI paths (see tests/architecture/test_hard_rules.py).
        assert not hasattr(emb, "get_backend")
        assert not hasattr(emb, "build_embeddings_index")
        from siftd.embeddings.base import get_backend  # noqa: F401


class TestDoctorChecks:
    def _force_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            avail, "embedding_status", lambda: EmbedStatus(None, False, "not configured")
        )

    def test_embeddings_stale_check_skips_when_unavailable(self, tmp_path, monkeypatch):
        from siftd.doctor.checks import CheckContext, EmbeddingsStaleCheck

        self._force_unavailable(monkeypatch)
        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        assert EmbeddingsStaleCheck().run(ctx) == []

    def test_orphaned_chunks_check_skips_when_unavailable(self, tmp_path, monkeypatch):
        from siftd.doctor.checks import CheckContext, OrphanedChunksCheck

        self._force_unavailable(monkeypatch)
        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        assert OrphanedChunksCheck().run(ctx) == []

    def test_embeddings_available_check_reports_when_db_exists(self, tmp_path, monkeypatch):
        from siftd.doctor.checks import CheckContext, EmbeddingsAvailableCheck

        self._force_unavailable(monkeypatch)
        embed_db = tmp_path / "embed.db"
        embed_db.touch()
        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=embed_db,
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        findings = EmbeddingsAvailableCheck().run(ctx)
        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "not installed" in findings[0].message

    def test_embeddings_available_check_silent_when_no_db(self, tmp_path, monkeypatch):
        from siftd.doctor.checks import CheckContext, EmbeddingsAvailableCheck

        self._force_unavailable(monkeypatch)
        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=tmp_path / "embed.db",  # does not exist
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        assert EmbeddingsAvailableCheck().run(ctx) == []
