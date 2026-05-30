"""Regression test for C02 (comprehensive-review 2026-05-28).

The vocabulary caches (`_model_cache`/`_provider_cache`) are populated with the
ULID of an uncommitted INSERT. The ingest loop holds one connection and calls
`conn.rollback()` on every IntegrityError/Exception. Rollback erases the new
vocab row from the DB but NOT from the cache, so a later file in the same run
would reuse the cached, now-dangling ULID as a foreign key and fail spuriously.

The fix clears the vocab caches in the rollback handlers. This test reproduces
the cross-file poisoning: source 1 seeds a brand-new model then fails its write
(forcing a rollback); source 2 reuses that model and must still ingest cleanly.
"""

from __future__ import annotations

from conftest import make_conversation

import siftd.ingestion.orchestration as orch
from siftd.domain.source import Source
from siftd.storage.sqlite import create_database, get_or_create_model

SHARED_MODEL = "shared-model-c02-rollback"


def _two_source_adapter(file_a, file_b, conv_a, conv_b):
    """Adapter whose discover yields two file sources, parsed independently."""
    mapping = {str(file_a): conv_a, str(file_b): conv_b}

    class _Adapter:
        NAME = "test_harness"
        DEDUP_STRATEGY = "file"
        HARNESS_SOURCE = "test"

        @staticmethod
        def can_handle(source):
            return True

        @staticmethod
        def parse(source):
            return [mapping[str(source.location)]]

        @staticmethod
        def discover():
            yield Source(kind="file", location=str(file_a))
            yield Source(kind="file", location=str(file_b))

    return _Adapter


def test_rollback_does_not_strand_vocab_cache_for_next_file(tmp_path, monkeypatch):
    db_path = tmp_path / "c02.db"
    conn = create_database(db_path)

    file_a = tmp_path / "a.jsonl"
    file_b = tmp_path / "b.jsonl"
    file_a.write_text("{}")
    file_b.write_text("{}")

    # Both conversations reference the same brand-new model. Source A fails its
    # write after the model is created; source B reuses it.
    conv_a = make_conversation(external_id="conv-a", model=SHARED_MODEL)
    conv_b = make_conversation(external_id="conv-b", model=SHARED_MODEL)
    adapter = _two_source_adapter(file_a, file_b, conv_a, conv_b)

    real_store = orch.store_conversation
    calls = {"n": 0}

    def flaky_store(conn_, conversation, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Seed the model cache with an uncommitted ULID, then die before
            # commit — exactly the window the cache-poisoning bug lives in.
            get_or_create_model(conn_, SHARED_MODEL)
            raise RuntimeError("simulated mid-write failure")
        return real_store(conn_, conversation, **kwargs)

    monkeypatch.setattr(orch, "store_conversation", flaky_store)

    stats = orch.ingest_all(conn, [adapter])

    # Source B must ingest despite source A poisoning the cache pre-fix.
    assert stats.files_ingested == 1, "second file should ingest after the first's rollback"

    # And the database must be FK-clean — the dangling-ULID symptom.
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert dangling == [], f"dangling foreign keys after rollback: {dangling}"

    # The reused model resolves to a real, committed row.
    row = conn.execute("SELECT id FROM models WHERE name = ?", (SHARED_MODEL,)).fetchone()
    assert row is not None
    conn.close()
