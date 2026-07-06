"""Edge tests for the schema-v2 index lifecycle (base lane — fake backend, no fastembed).

Exercises the fingerprint staleness diff, prune, identity-meta-first, per-batch commit,
v1 detection, and the chunks-without-metadata rebuild guard against real SQLite DBs with
a stubbed embedding backend (so no ONNX model is loaded).
"""

import sqlite3

import pytest

from siftd.embeddings import indexer as ix
from siftd.storage.embeddings import (
    chunk_count,
    get_indexed_conversation_ids,
    get_indexed_state,
    get_meta,
    open_embeddings_db,
)
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)


class FakeBackend:
    name = "fake"
    model = "fake-model"
    dimension = 3

    def __init__(self):
        self.batch_calls = 0

    def embed_documents(self, texts):
        self.batch_calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_query(self, text):
        return [0.1, 0.2, 0.3]


def _make_main_db(path, convs):
    """convs: list of (conv_ext, [(prompt_text, response_text), ...])."""
    conn = create_database(path)
    h = get_or_create_harness(conn, "t", source="t", log_format="jsonl")
    m = get_or_create_model(conn, "test-model")
    w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    ids = {}
    for ci, (ext, exchanges) in enumerate(convs):
        cid = insert_conversation(
            conn, external_id=ext, harness_id=h, workspace_id=w,
            started_at=f"2024-01-0{ci + 1}T00:00:00Z",
        )
        ids[ext] = cid
        for ei, (ptext, rtext) in enumerate(exchanges):
            pid = insert_prompt(conn, cid, f"{ext}-p{ei}", f"2024-01-0{ci + 1}T00:0{ei}:01Z")
            insert_prompt_content(conn, pid, 0, "text", f'{{"text": {ptext!r}}}')
            rid = insert_response(
                conn, cid, pid, m, None, f"{ext}-r{ei}", f"2024-01-0{ci + 1}T00:0{ei}:02Z",
                input_tokens=5, output_tokens=10,
            )
            insert_response_content(conn, rid, 0, "text", f'{{"text": {rtext!r}}}')
    conn.commit()
    conn.close()
    return ids


@pytest.fixture
def fake_backend(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(ix, "get_backend", lambda **_k: backend)
    return backend


def test_missing_db_raises(tmp_path, fake_backend):
    with pytest.raises(FileNotFoundError):
        ix.build_embeddings_index(db_path=tmp_path / "missing.db", embed_db_path=tmp_path / "e.db")


def test_empty_db_returns_zero(tmp_path, fake_backend):
    db = tmp_path / "main.db"
    create_database(db).close()
    stats = ix.build_embeddings_index(db_path=db, embed_db_path=tmp_path / "e.db")
    assert stats.chunks_added == 0 and stats.total_chunks == 0


def test_build_then_incremental_noop(tmp_path, fake_backend):
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello there", "hi back")]), ("c2", [("second one", "reply two")])])

    s1 = ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    assert s1.chunks_added == 2 and s1.conversations_indexed == 2

    s2 = ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    assert s2.chunks_added == 0 and s2.chunks_removed == 0


def test_identity_meta_written_first(tmp_path, fake_backend):
    """After a build, the index describes itself (backend/model/dimension/schema)."""
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("q one", "a one")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    conn = open_embeddings_db(edb, read_only=True)
    try:
        assert get_meta(conn, "schema_version") == str(ix.SCHEMA_VERSION)
        assert get_meta(conn, "backend") == "fake"
        assert get_meta(conn, "model") == "fake-model"
        assert get_meta(conn, "dimension") == "3"
        assert get_meta(conn, "built_at")
    finally:
        conn.close()


def test_append_reindexes_conversation(tmp_path, fake_backend):
    """Appending to an indexed conversation changes its fingerprint → re-index (v1 bug fix)."""
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    ids = _make_main_db(db, [("c1", [("first", "resp")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    # Append a second exchange to c1.
    conn = create_database(db)
    m = get_or_create_model(conn, "test-model")
    pid = insert_prompt(conn, ids["c1"], "c1-p1", "2024-01-01T00:10:00Z")
    insert_prompt_content(conn, pid, 0, "text", '{"text": "a follow up question"}')
    rid = insert_response(conn, ids["c1"], pid, m, None, "c1-r1", "2024-01-01T00:10:02Z", input_tokens=5, output_tokens=10)
    insert_response_content(conn, rid, 0, "text", '{"text": "a follow up answer"}')
    conn.commit()
    conn.close()

    s = ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    assert s.chunks_added >= 1 and s.chunks_removed >= 1 and s.conversations_indexed == 1


def test_prune_removed_conversation(tmp_path, fake_backend):
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    ids = _make_main_db(db, [("c1", [("keep me", "ok")]), ("c2", [("delete me", "ok")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    conn = create_database(db)
    conn.execute("DELETE FROM events WHERE conversation_id=?", (ids["c2"],))
    conn.execute("DELETE FROM conversations WHERE id=?", (ids["c2"],))
    conn.commit()
    conn.close()

    s = ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    assert s.conversations_pruned == 1 and s.chunks_removed >= 1
    conn = open_embeddings_db(edb, read_only=True)
    try:
        assert ids["c2"] not in get_indexed_state(conn)
        assert ids["c1"] in get_indexed_state(conn)
    finally:
        conn.close()


def test_per_batch_commit_marks_each_conversation(tmp_path, fake_backend, monkeypatch):
    """With batch size 1, each conversation's state commits as its chunk stores."""
    monkeypatch.setattr(ix, "_BATCH_SIZE", 1)
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("aa", "bb")]), ("c2", [("cc", "dd")]), ("c3", [("ee", "ff")])])
    s = ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    assert s.conversations_indexed == 3
    assert fake_backend.batch_calls == 3  # one embed call per single-chunk batch


def test_v1_index_detected_rebuild_required(tmp_path, fake_backend):
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    raw = sqlite3.connect(edb)
    raw.execute("UPDATE index_meta SET value='1' WHERE key='schema_version'")
    raw.commit()
    raw.close()

    with pytest.raises(ix.IncrementalCompatError, match="rebuild"):
        ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    # Rebuild recovers.
    s = ix.build_embeddings_index(db_path=db, embed_db_path=edb, rebuild=True)
    assert s.chunks_added >= 1


def test_chunks_without_identity_meta_rebuild_required(tmp_path, fake_backend):
    """Chunks present but backend metadata absent → rebuild-required (no silent mixing)."""
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    raw = sqlite3.connect(edb)
    raw.execute("DELETE FROM index_meta WHERE key='backend'")
    raw.commit()
    raw.close()

    with pytest.raises(ix.IncrementalCompatError, match="identity metadata"):
        ix.build_embeddings_index(db_path=db, embed_db_path=edb)


def test_backend_mismatch_incremental_refused(tmp_path, fake_backend, monkeypatch):
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    other = FakeBackend()
    other.name = "remote:voyage"
    other.model = "voyage-4-lite"
    monkeypatch.setattr(ix, "get_backend", lambda **_k: other)

    with pytest.raises(ix.IncrementalCompatError, match="different backend"):
        ix.build_embeddings_index(db_path=db, embed_db_path=edb)


def test_dimension_mismatch_incremental_refused(tmp_path, fake_backend, monkeypatch):
    """Same backend + model but a different width (e.g. embed.dimensions narrowed via
    matryoshka truncation) must refuse the incremental rather than mix vector widths (F1)."""
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)  # stored dimension = 3

    narrowed = FakeBackend()
    narrowed.dimension = 5  # same name/model, different width
    monkeypatch.setattr(ix, "get_backend", lambda **_k: narrowed)

    with pytest.raises(ix.IncrementalCompatError, match="different embedding dimension"):
        ix.build_embeddings_index(db_path=db, embed_db_path=edb)


def test_incremental_seeds_backend_dimension_from_stored(tmp_path, fake_backend, monkeypatch):
    """A remote backend that hasn't learned its width (dimension None) is seeded from the
    index's stored dimension while an incremental run is live — so its response validation
    enforces the index's width — then restored: the backend object is process-cached, and a
    stale seed must not leak into a later build in the same process (F1)."""
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    ids = _make_main_db(db, [("c1", [("hello", "world")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)  # stores dimension = 3

    # Append a second exchange so the incremental run has real embedding work.
    conn = create_database(db)
    m = get_or_create_model(conn, "test-model")
    pid = insert_prompt(conn, ids["c1"], "c1-p1", "2024-01-01T00:10:00Z")
    insert_prompt_content(conn, pid, 0, "text", '{"text": "a follow up question"}')
    rid = insert_response(conn, ids["c1"], pid, m, None, "c1-r1", "2024-01-01T00:10:02Z", input_tokens=5, output_tokens=10)
    insert_response_content(conn, rid, 0, "text", '{"text": "a follow up answer"}')
    conn.commit()
    conn.close()

    seen_at_embed: list[int | None] = []

    class UnlearnedBackend(FakeBackend):
        dimension = None  # not yet learned from a first response

        def embed_documents(self, texts):
            seen_at_embed.append(self.dimension)
            return super().embed_documents(texts)

    seeded = UnlearnedBackend()
    monkeypatch.setattr(ix, "get_backend", lambda **_k: seeded)

    ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    assert seen_at_embed and all(d == 3 for d in seen_at_embed)  # seed live during the run
    assert seeded.dimension is None  # restored — the cached backend isn't poisoned


def test_rebuild_clears_and_rewrites(tmp_path, fake_backend):
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")]), ("c2", [("second", "reply")])])
    s1 = ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    s2 = ix.build_embeddings_index(db_path=db, embed_db_path=edb, rebuild=True)
    assert s2.chunks_added == s1.total_chunks and s2.total_chunks == s1.total_chunks


def test_filter_conversations_with_tool_calls_batches(tmp_path):
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT NOT NULL, conversation_id TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO events (id, kind, conversation_id) VALUES (?, 'tool_call', ?)",
        [(f"e{i}", f"c{i}") for i in range(1005)],
    )
    conn.commit()
    try:
        keep = ix._filter_conversations_with_tool_calls(conn, {f"c{i}" for i in range(1200)})
        assert len(keep) == 1005
        assert ix._filter_conversations_with_tool_calls(conn, set()) == set()
    finally:
        conn.close()


def test_status_reports_coverage_and_size(tmp_path, fake_backend):
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")]), ("c2", [("second", "reply")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    rep = ix.embed_index_status(db_path=db, embed_db_path=edb)
    assert rep.index_exists and not rep.needs_rebuild
    assert rep.stored_backend == "fake" and rep.stored_dimension == 3
    assert rep.conversations_total == 2 and rep.conversations_indexed == 2
    assert rep.conversations_stale == 0 and rep.db_size_bytes > 0
    assert rep.chunk_counts.get("exchange", 0) >= 2


def test_status_no_index(tmp_path, fake_backend):
    db = tmp_path / "main.db"
    _make_main_db(db, [("c1", [("hello", "world")])])
    rep = ix.embed_index_status(db_path=db, embed_db_path=tmp_path / "none.db")
    assert not rep.index_exists and rep.conversations_stale == rep.conversations_total == 1


def test_zero_chunk_build_stamps_identity_and_marks_indexed(tmp_path, fake_backend, monkeypatch):
    """A build whose to_index conversations yield NO chunks still stamps identity metadata
    and marks them indexed — no 'schema vNone / needs_rebuild / indexed-yet-stale' self-
    contradiction (finding 1). Reproduces contentless conversations via an empty chunker."""
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")]), ("c2", [("second", "reply")])])

    monkeypatch.setattr(ix, "extract_exchange_window_chunks", lambda *a, **k: [])
    monkeypatch.setattr(ix, "extract_tool_summary_chunks", lambda *a, **k: [])

    stats = ix.build_embeddings_index(db_path=db, embed_db_path=edb)
    assert stats.chunks_added == 0
    assert stats.total_chunks == 0
    assert stats.conversations_indexed == 2  # both marked so they aren't rescanned

    conn = open_embeddings_db(edb, read_only=True)
    try:
        assert get_meta(conn, "schema_version") == str(ix.SCHEMA_VERSION)
        assert get_meta(conn, "backend") == "fake"
        assert get_meta(conn, "model") == "fake-model"
    finally:
        conn.close()

    rep = ix.embed_index_status(db_path=db, embed_db_path=edb)
    assert rep.schema_version == ix.SCHEMA_VERSION
    assert rep.needs_rebuild is False
    assert rep.total_chunks == 0
    # Indexed AND zero stale — the two counts agree (the bug reported both == total).
    assert rep.conversations_indexed == 2
    assert rep.conversations_stale == 0


def test_status_empty_index_is_unbuilt_not_rebuild(tmp_path, fake_backend):
    """A schema-valid embed DB with no chunks reads as unbuilt: never needs_rebuild, never a
    'vNone' schema — an incremental embed suffices (finding 2)."""
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")])])
    open_embeddings_db(edb).close()  # create the file + schema, but never build

    rep = ix.embed_index_status(db_path=db, embed_db_path=edb)
    assert rep.index_exists is True
    assert rep.total_chunks == 0
    assert rep.needs_rebuild is False
    assert rep.backend_mismatch is False


def test_status_flags_backend_and_model_mismatch(tmp_path, fake_backend, monkeypatch):
    """After building, a configured backend/model that differs from the stored one is flagged
    so --status agrees with the next build's IncrementalCompatError (finding 3)."""
    from siftd.embeddings.availability import EmbedStatus

    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    _make_main_db(db, [("c1", [("hello", "world")])])
    ix.build_embeddings_index(db_path=db, embed_db_path=edb)  # stored backend = "fake"

    # Different backend name.
    monkeypatch.setattr(
        "siftd.embeddings.availability.embedding_status",
        lambda: EmbedStatus("remote:voyage", True, "remote backend (voyage-4)", model="voyage-4"),
    )
    rep = ix.embed_index_status(db_path=db, embed_db_path=edb)
    assert rep.stored_backend == "fake"
    assert rep.configured_backend == "remote:voyage"
    assert rep.backend_mismatch is True
    assert rep.stored_backend_config == "fake"  # config token, no remote: prefix

    # Same backend name, different model — also a mismatch.
    monkeypatch.setattr(
        "siftd.embeddings.availability.embedding_status",
        lambda: EmbedStatus("fake", True, "local", model="fake-model-v2"),
    )
    rep = ix.embed_index_status(db_path=db, embed_db_path=edb)
    assert rep.backend_mismatch is True

    # Matching backend + model — not a mismatch.
    monkeypatch.setattr(
        "siftd.embeddings.availability.embedding_status",
        lambda: EmbedStatus("fake", True, "local", model="fake-model"),
    )
    rep = ix.embed_index_status(db_path=db, embed_db_path=edb)
    assert rep.backend_mismatch is False


def test_interrupt_preserves_prior_coverage_of_uncommitted_conversations(tmp_path, fake_backend, monkeypatch):
    """A mid-build interrupt keeps the PRIOR chunks of conversations whose replacement batch
    never committed — old chunks are replaced per-conversation inside the batch txn, not
    bulk-deleted up front (finding 4)."""
    monkeypatch.setattr(ix, "_BATCH_SIZE", 1)
    db = tmp_path / "main.db"
    edb = tmp_path / "e.db"
    ids = _make_main_db(db, [("c1", [("alpha one", "beta one")]), ("c2", [("gamma two", "delta two")])])

    ix.build_embeddings_index(db_path=db, embed_db_path=edb)  # both indexed, 1 chunk each

    # Change BOTH conversations so both land in to_index next run.
    conn = create_database(db)
    m = get_or_create_model(conn, "test-model")
    for ext in ("c1", "c2"):
        pid = insert_prompt(conn, ids[ext], f"{ext}-p9", "2024-02-01T00:00:00Z")
        insert_prompt_content(conn, pid, 0, "text", '{"text": "a brand new follow up"}')
        rid = insert_response(conn, ids[ext], pid, m, None, f"{ext}-r9", "2024-02-01T00:00:02Z", input_tokens=5, output_tokens=10)
        insert_response_content(conn, rid, 0, "text", '{"text": "a brand new answer"}')
    conn.commit()
    conn.close()

    conn = open_embeddings_db(edb, read_only=True)
    before = get_indexed_state(conn)
    assert chunk_count(conn) == 2
    conn.close()

    class InterruptingBackend(FakeBackend):
        def embed_documents(self, texts):
            self.batch_calls += 1
            if self.batch_calls > 1:  # first conversation commits, then interrupt
                raise RuntimeError("simulated interrupt")
            return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(ix, "get_backend", lambda **_k: InterruptingBackend())

    with pytest.raises(RuntimeError, match="interrupt"):
        ix.build_embeddings_index(db_path=db, embed_db_path=edb)

    conn = open_embeddings_db(edb, read_only=True)
    try:
        after = get_indexed_state(conn)
        changed = [cid for cid in before if after.get(cid) != before.get(cid)]
        unchanged = [cid for cid in before if after.get(cid) == before.get(cid)]
        assert len(changed) == 1 and len(unchanged) == 1
        # The uncommitted conversation kept its prior chunk (was NOT bulk-deleted up front).
        assert unchanged[0] in get_indexed_conversation_ids(conn)
        # 1 replaced (new) + 1 preserved (old) = 2, versus 1 under the old upfront-delete bug.
        assert chunk_count(conn) == 2
    finally:
        conn.close()
