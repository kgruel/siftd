"""Tests for graceful degradation when embeddings not installed."""

import pytest


class TestEmbeddingsAvailability:
    """Tests for the embeddings availability detection."""

    def test_embeddings_available_returns_bool(self):
        """embeddings_available() returns a boolean."""
        from siftd.embeddings import embeddings_available

        result = embeddings_available()
        assert isinstance(result, bool)

    def test_availability_is_cached(self, monkeypatch):
        """Result is cached after first check."""
        import siftd.embeddings.availability as avail

        # Reset cached value
        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", None)

        # First call caches the result
        first = avail.embeddings_available()

        # Force a different result in the cache
        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", not first)

        # Second call should return cached value
        second = avail.embeddings_available()
        assert second == (not first)  # Proves it read from cache

    def test_require_embeddings_does_nothing_when_available(self, monkeypatch):
        """require_embeddings() passes silently when deps are installed."""
        import siftd.embeddings.availability as avail

        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", True)

        # Should not raise
        avail.require_embeddings("test operation")

    def test_require_embeddings_raises_when_unavailable(self, monkeypatch):
        """require_embeddings() raises EmbeddingsNotAvailable when deps missing."""
        import siftd.embeddings.availability as avail

        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", False)

        with pytest.raises(avail.EmbeddingsNotAvailable) as exc_info:
            avail.require_embeddings("Semantic search")

        assert "Semantic search" in str(exc_info.value)
        assert "siftd install embed" in str(exc_info.value)
        assert "siftd query -s" in str(exc_info.value)

    def test_exception_message_includes_install_hint(self, monkeypatch):
        """EmbeddingsNotAvailable message tells user how to install."""
        import siftd.embeddings.availability as avail

        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", False)

        try:
            avail.require_embeddings("Building index")
        except avail.EmbeddingsNotAvailable as e:
            assert "siftd install embed" in e.message
            assert "Building index" in e.message


class TestEmbeddingsModuleExports:
    """Tests for conditional exports in embeddings/__init__.py."""

    def test_always_exports_availability_functions(self):
        """Availability functions are always exported."""
        from siftd.embeddings import (
            EmbeddingsNotAvailable,
            embeddings_available,
            require_embeddings,
        )

        assert callable(embeddings_available)
        assert callable(require_embeddings)
        assert issubclass(EmbeddingsNotAvailable, Exception)

    def test_conditional_exports_match_availability(self):
        """Backend exports only present when embeddings available."""
        from siftd.embeddings import embeddings_available

        if embeddings_available():
            # Should be able to import these
            from siftd.embeddings import (
                EmbeddingBackend,
                IndexStats,
                build_embeddings_index,
                get_backend,
            )

            assert callable(get_backend)
            assert callable(build_embeddings_index)
            # Verify types exist (accessing to silence F401 unused import)
            assert EmbeddingBackend is not None
            assert IndexStats is not None
        else:
            # These should not be importable
            import siftd.embeddings as emb

            assert not hasattr(emb, "get_backend")
            assert not hasattr(emb, "EmbeddingBackend")
            assert not hasattr(emb, "build_embeddings_index")


class TestDoctorChecks:
    """Tests for doctor checks with embeddings unavailable."""

    def test_embeddings_stale_check_skips_when_unavailable(self, tmp_path, monkeypatch):
        """EmbeddingsStaleCheck returns no findings when embeddings not installed."""
        import siftd.embeddings.availability as avail
        from siftd.doctor.checks import CheckContext, EmbeddingsStaleCheck

        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", False)

        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        check = EmbeddingsStaleCheck()
        findings = check.run(ctx)

        assert findings == []

    def test_orphaned_chunks_check_skips_when_unavailable(self, tmp_path, monkeypatch):
        """OrphanedChunksCheck returns no findings when embeddings not installed."""
        import siftd.embeddings.availability as avail
        from siftd.doctor.checks import CheckContext, OrphanedChunksCheck

        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", False)

        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        check = OrphanedChunksCheck()
        findings = check.run(ctx)

        assert findings == []

    def test_embeddings_available_check_reports_when_db_exists(self, tmp_path, monkeypatch):
        """EmbeddingsAvailableCheck reports info when DB exists but deps missing."""
        import siftd.embeddings.availability as avail
        from siftd.doctor.checks import CheckContext, EmbeddingsAvailableCheck

        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", False)

        # Create the embed DB file
        embed_db = tmp_path / "embed.db"
        embed_db.touch()

        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=embed_db,
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        check = EmbeddingsAvailableCheck()
        findings = check.run(ctx)

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "not installed" in findings[0].message

    def test_embeddings_available_check_silent_when_no_db(self, tmp_path, monkeypatch):
        """EmbeddingsAvailableCheck returns nothing when no embeddings DB exists."""
        import siftd.embeddings.availability as avail
        from siftd.doctor.checks import CheckContext, EmbeddingsAvailableCheck

        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", False)

        ctx = CheckContext(
            db_path=tmp_path / "main.db",
            embed_db_path=tmp_path / "embed.db",  # Does not exist
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        check = EmbeddingsAvailableCheck()
        findings = check.run(ctx)

        assert findings == []
