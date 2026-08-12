"""Embeddings storage for semantic search.

Separate SQLite DB from the main siftd.db — embeddings are derived data
that can be rebuilt from the main DB at any time.
"""

import json
import sqlite3
import struct
import time
from pathlib import Path

import numpy as np

from siftd.errors import DriftError
from siftd.ids import ulid as _ulid
from siftd.storage.sql_helpers import batched_execute
from siftd.storage.sqlite import connect_read_only


def open_embeddings_db(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open embeddings database.

    Args:
        db_path: Path to embeddings DB.
        read_only: If True, open without forcing WAL or creating/migrating schema.
            This allows read-only operations in restricted environments.
    """
    if read_only and not db_path.exists():
        raise FileNotFoundError(f"Embeddings database not found: {db_path}")

    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        conn = connect_read_only(db_path)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")

        try:
            _create_schema(conn)
        except Exception:
            conn.close()
            raise

    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create the embeddings schema (v2).

    Derived data: there is no ALTER migration. A v1 (or version-less) index is
    detected on read/build and rebuilt from scratch — see
    ``embeddings.indexer._validate_incremental_compat`` and ``validate_index_compat``.
    ``CREATE TABLE IF NOT EXISTS`` on a v1 file only adds the missing ``indexed_state``
    table (empty); the version check still forces the rebuild.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            chunk_type TEXT NOT NULL,  -- 'exchange' | 'tool_summary'
            text TEXT NOT NULL,
            embedding BLOB,
            token_count INTEGER,
            source_ids TEXT,  -- JSON array of constituent event IDs (prompt + response)
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_conversation
            ON chunks(conversation_id);

        CREATE INDEX IF NOT EXISTS idx_chunks_type
            ON chunks(chunk_type);

        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Per-conversation staleness state (schema v2). The fingerprint lets the
        -- indexer detect appends to an already-indexed conversation (the v1 append
        -- bug: any chunk row marked a conversation permanently "indexed").
        CREATE TABLE IF NOT EXISTS indexed_state (
            conversation_id TEXT PRIMARY KEY,
            fingerprint TEXT,
            chunk_count INTEGER,
            indexed_at TEXT
        );
    """)
    conn.commit()


def store_chunk(
    conn: sqlite3.Connection,
    conversation_id: str,
    chunk_type: str,
    text: str,
    embedding: list[float],
    *,
    token_count: int | None = None,
    source_ids: list[str] | None = None,
    commit: bool = False,
) -> str:
    """Store a text chunk with its embedding vector."""
    chunk_id = _ulid()
    embedding_blob = _encode_embedding(embedding)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    actual_token_count = token_count if token_count is not None else len(text.split())
    source_ids_json = json.dumps(source_ids) if source_ids else None

    conn.execute(
        """INSERT INTO chunks (id, conversation_id, chunk_type, text, embedding, token_count, source_ids, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, conversation_id, chunk_type, text, embedding_blob, actual_token_count, source_ids_json, created_at),
    )
    if commit:
        conn.commit()
    _embedding_cache.invalidate()
    return chunk_id


def get_indexed_conversation_ids(conn: sqlite3.Connection) -> set[str]:
    """Return set of conversation IDs that already have embeddings."""
    cur = conn.execute("SELECT DISTINCT conversation_id FROM chunks")
    return {row["conversation_id"] for row in cur.fetchall()}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def get_indexed_state(conn: sqlite3.Connection) -> dict[str, tuple[str | None, int]]:
    """Return ``{conversation_id: (fingerprint, chunk_count)}`` from indexed_state.

    Empty when the table is absent (a v1 index that predates schema v2), so a
    consumer treats every conversation as un-indexed and forces a rebuild.
    """
    if not _has_table(conn, "indexed_state"):
        return {}
    return {
        row["conversation_id"]: (row["fingerprint"], row["chunk_count"] or 0)
        for row in conn.execute(
            "SELECT conversation_id, fingerprint, chunk_count FROM indexed_state"
        ).fetchall()
    }


def get_indexed_state_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the conversation IDs recorded as indexed in ``indexed_state``.

    Distinct from :func:`get_indexed_conversation_ids` (which reads ``chunks``): a
    conversation counts as indexed only once its ``indexed_state`` row is written —
    the marker the fingerprint lifecycle sets after a conversation is fully stored.
    Empty when the table is absent (v1 index).
    """
    if not _has_table(conn, "indexed_state"):
        return set()
    return {
        row["conversation_id"]
        for row in conn.execute("SELECT conversation_id FROM indexed_state").fetchall()
    }


def upsert_indexed_state(
    conn: sqlite3.Connection,
    conversation_id: str,
    fingerprint: str,
    chunk_count: int,
    *,
    commit: bool = False,
) -> None:
    """Record (or refresh) a conversation's indexed fingerprint + chunk count."""
    indexed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT OR REPLACE INTO indexed_state "
        "(conversation_id, fingerprint, chunk_count, indexed_at) VALUES (?, ?, ?, ?)",
        (conversation_id, fingerprint, chunk_count, indexed_at),
    )
    if commit:
        conn.commit()


def delete_conversations(
    conn: sqlite3.Connection, conversation_ids: set[str], *, commit: bool = False
) -> int:
    """Delete chunks + indexed_state for the given conversations (batched).

    The single sweep the incremental indexer runs before re-indexing changed
    conversations and pruning removed ones. Returns the number of chunk rows deleted.
    A brand-new conversation (no chunks yet) is a harmless no-op.
    """
    if not conversation_ids:
        return 0
    deleted = batched_execute(
        conn, "DELETE FROM chunks WHERE conversation_id IN ({placeholders})", conversation_ids
    )
    if _has_table(conn, "indexed_state"):
        batched_execute(
            conn,
            "DELETE FROM indexed_state WHERE conversation_id IN ({placeholders})",
            conversation_ids,
        )
    if commit:
        conn.commit()
    if deleted:
        _embedding_cache.invalidate()
    return deleted


def chunk_counts_by_type(conn: sqlite3.Connection) -> dict[str, int]:
    """Return chunk counts grouped by ``chunk_type`` (e.g. exchange, tool_summary)."""
    return {
        row["chunk_type"]: row["cnt"]
        for row in conn.execute(
            "SELECT chunk_type, COUNT(*) AS cnt FROM chunks GROUP BY chunk_type"
        ).fetchall()
    }


def clear_all(conn: sqlite3.Connection) -> None:
    """Drop chunks, index_meta, and indexed_state, then recreate (full rebuild).

    A rebuild starts from a clean slate: stale identity metadata and per-conversation
    state must not survive, or an interrupted rebuild could leave the index describing
    a backend it no longer holds.
    """
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute("DROP TABLE IF EXISTS index_meta")
    conn.execute("DROP TABLE IF EXISTS indexed_state")
    _create_schema(conn)
    conn.commit()
    _embedding_cache.invalidate()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a metadata key-value pair."""
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a metadata value by key."""
    cur = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None


class _EmbeddingCache:
    """In-memory cache of all embedding data for fast repeated searches.

    Holds a pre-decoded, L2-normalized numpy matrix and row metadata so
    search_similar() can skip SQLite fetch + blob decode on repeated calls.
    """

    def __init__(self):
        self._db_path: str | None = None
        self._db_mtime: float = 0.0
        self._chunk_count: int = 0
        # Keep only the normalized matrix to avoid doubling resident memory.
        # (MMR reranking normalizes again, so returning normalized vectors is fine.)
        self.embeddings: np.ndarray | None = None  # legacy / unused (kept for compatibility)
        self.embeddings_normalized: np.ndarray | None = None  # (n, dim) float32, L2-normalized
        self.chunk_ids: list[str] = []
        self.conversation_ids: list[str] = []
        self.chunk_types: list[str] = []
        self.texts: list[str] = []
        self.source_ids_raw: list[str | None] = []
        # Lookup: conversation_id -> list of row indices
        self.conv_id_to_indices: dict[str, list[int]] = {}
        # Lazy lookup: event_id -> row indices of chunks whose source_ids cover it.
        # Built on first bridge (chunks_for_events) so pure semantic/fts searches
        # never pay the per-chunk source_ids JSON decode. None ⇒ not yet built.
        self.event_id_to_indices: dict[str, list[int]] | None = None

    def is_valid(self, db_path_hint: str) -> bool:
        """Check if cache is loaded and current for this DB path.

        Uses path identity + file mtime to detect external updates
        (e.g., another process rebuilding the index). A single stat()
        call (~0.01ms) instead of a SQL COUNT(*) query.
        """
        if self._db_path != db_path_hint or self.embeddings_normalized is None:
            return False
        try:
            current_mtime = Path(db_path_hint).stat().st_mtime
            return current_mtime == self._db_mtime
        except OSError:
            return False

    def invalidate(self) -> None:
        """Force cache reload on next access (e.g., after ingest)."""
        self._db_path = None

    def load(self, conn: sqlite3.Connection, db_path_hint: str) -> None:
        """Load all chunk data from DB into memory."""
        rows = conn.execute(
            "SELECT id, conversation_id, chunk_type, text, embedding, source_ids FROM chunks"
        ).fetchall()

        if not rows:
            self._db_path = db_path_hint
            self._chunk_count = 0
            try:
                self._db_mtime = Path(db_path_hint).stat().st_mtime
            except OSError:
                self._db_mtime = 0.0
            self.embeddings = None
            self.embeddings_normalized = np.empty((0, 0), dtype=np.float32)
            self.chunk_ids = []
            self.conversation_ids = []
            self.chunk_types = []
            self.texts = []
            self.source_ids_raw = []
            self.conv_id_to_indices = {}
            self.event_id_to_indices = None
            return

        embedding_dim = len(rows[0]["embedding"]) // 4
        blob_buffer = b"".join(row["embedding"] for row in rows)
        embeddings = np.frombuffer(blob_buffer, dtype=np.float32).reshape(
            len(rows), embedding_dim
        ).copy()  # copy to own the memory after blob_buffer is freed

        # Pre-normalize for fast cosine similarity (skip per-query normalization).
        # Do this in-place to avoid keeping both raw + normalized matrices.
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        embeddings /= norms
        self.embeddings_normalized = embeddings
        self.embeddings = None

        self.chunk_ids = [row["id"] for row in rows]
        self.conversation_ids = [row["conversation_id"] for row in rows]
        self.chunk_types = [row["chunk_type"] for row in rows]
        self.texts = [row["text"] for row in rows]
        self.source_ids_raw = [row["source_ids"] for row in rows]

        self.conv_id_to_indices = {}
        for i, cid in enumerate(self.conversation_ids):
            self.conv_id_to_indices.setdefault(cid, []).append(i)
        self.event_id_to_indices = None

        self._db_path = db_path_hint
        self._chunk_count = len(rows)
        try:
            self._db_mtime = Path(db_path_hint).stat().st_mtime
        except OSError:
            self._db_mtime = 0.0

    def ensure_event_map(self) -> dict[str, list[int]]:
        """Build (once) and return the event_id -> covering-chunk-index map.

        Decodes every chunk's ``source_ids`` — deferred to the first bridge so
        pure semantic/fts searches don't pay it. Rebuilt on cache reload.
        """
        if self.event_id_to_indices is None:
            event_map: dict[str, list[int]] = {}
            for i, raw in enumerate(self.source_ids_raw):
                if not raw:
                    continue
                for eid in json.loads(raw):
                    event_map.setdefault(eid, []).append(i)
            self.event_id_to_indices = event_map
        return self.event_id_to_indices


_embedding_cache = _EmbeddingCache()


def _ensure_cache_loaded(conn: sqlite3.Connection) -> str:
    """Load (or reuse) the in-memory embedding cache for ``conn``'s DB.

    Returns the DB path hint used as the cache key. The caller's connection is
    always the one read from: read-only opens now derive immutability rather
    than asserting it, and neither outcome leaves a connection that could serve
    a stale snapshot. A plain ``mode=ro`` connection takes WAL read marks and
    sees every commit; the ``immutable=1`` fallback is reached only when the
    ``-shm`` sidecar cannot be created, which is a medium no writer can reach —
    so its pinned snapshot cannot go out of date in the first place.

    Reloading through a second connection was the workaround for the case that
    no longer exists: an immutable open over a *writable* file.
    """
    db_path_hint = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    cache = _embedding_cache
    if not cache.is_valid(db_path_hint):
        cache.load(conn, db_path_hint)
    return db_path_hint


def search_similar(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    limit: int = 10,
    conversation_ids: set[str] | None = None,
    include_embeddings: bool = False,
    exclude_conversation_ids: set[str] | None = None,
) -> list[dict]:
    """Find chunks most similar to the query embedding (cosine similarity).

    If conversation_ids is provided, only search within those conversations.
    If exclude_conversation_ids is provided, mask those conversations from
    results (scores set to -inf before top-k selection, so they never appear
    regardless of how many high-scoring chunks they have).
    If include_embeddings is True, each result dict includes an 'embedding' key
    with the decoded float list (used by MMR reranking).
    Returns list of dicts: conversation_id, chunk_type, text, score, source_ids.
    """
    if conversation_ids is not None and not conversation_ids:
        return []

    _ensure_cache_loaded(conn)
    cache = _embedding_cache

    if cache.embeddings_normalized is None or cache._chunk_count == 0:
        return []

    embedding_dim = cache.embeddings_normalized.shape[1]

    # Validate query embedding dimension matches index
    if len(query_embedding) != embedding_dim:
        raise ValueError(
            f"Query embedding dimension ({len(query_embedding)}) does not match index dimension ({embedding_dim}). "
            f"Rebuild the index with 'siftd embed --rebuild' using the same embedding backend."
        )

    # Filter to candidate conversation IDs if provided
    if conversation_ids is not None:
        indices = []
        for cid in conversation_ids:
            indices.extend(cache.conv_id_to_indices.get(cid, []))
        if not indices:
            return []
        indices_arr = np.array(indices, dtype=np.intp)
        candidate_norm = cache.embeddings_normalized[indices_arr]
    else:
        indices_arr = None
        candidate_norm = cache.embeddings_normalized

    # Compute similarities using pre-normalized embeddings (only normalize query)
    query_array = np.asarray(query_embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query_array)
    if query_norm == 0:
        scores = np.zeros(candidate_norm.shape[0], dtype=np.float32)
    else:
        scores = candidate_norm @ (query_array / query_norm)

    # Mask excluded conversations so they never appear in results
    if exclude_conversation_ids:
        for local_i in range(len(scores)):
            global_i = int(indices_arr[local_i]) if indices_arr is not None else local_i
            if cache.conversation_ids[global_i] in exclude_conversation_ids:
                scores[local_i] = -np.inf

    # Find top-k using argpartition (O(n) vs O(n log n) for full sort)
    n = len(scores)
    k = min(limit, n)
    if n <= limit:
        top_local = np.argsort(-scores)
    else:
        partitioned = np.argpartition(-scores, k)[:k]
        top_local = partitioned[np.argsort(-scores[partitioned])]

    # Build results only for top-k, skipping any masked (-inf) scores
    from siftd.domain.search_types import ScoreBreakdown

    results = []
    for local_idx in top_local:
        local_i = int(local_idx)
        score_val = float(scores[local_i])
        if score_val == float("-inf"):
            continue  # excluded conversation leaked through argpartition

        # Map back to global index
        global_i = int(indices_arr[local_i]) if indices_arr is not None else local_i

        raw_source = cache.source_ids_raw[global_i]
        source_ids_val = json.loads(raw_source) if raw_source else []
        embedding_sim = score_val
        result = {
            "chunk_id": cache.chunk_ids[global_i],
            "conversation_id": cache.conversation_ids[global_i],
            "chunk_type": cache.chunk_types[global_i],
            "text": cache.texts[global_i],
            "score": embedding_sim,
            "source_ids": source_ids_val,
            "breakdown": ScoreBreakdown(embedding_sim=embedding_sim),
        }
        if include_embeddings:
            result["embedding"] = cache.embeddings_normalized[global_i]
        results.append(result)

    return results


def chunks_for_events(
    conn: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, list[dict]]:
    """Map event ids to the chunk(s) whose ``source_ids`` cover them.

    The FTS→embed bridge for RRF hybrid fusion: a keyword hit on a prompt/response
    event resolves to its covering exchange chunk(s). Returns ``{event_id: [chunk
    dicts]}`` including only events with at least one covering chunk (callers treat
    an absent/empty entry as "no covering chunk" → an FTS-only fusion entrant).
    Each chunk dict carries ``chunk_id``/``conversation_id``/``chunk_type``/``text``/
    ``source_ids`` — no cosine (these chunks were not scored against the query); the
    engine leaves their ``embedding_sim`` absent, exempting them from the
    similarity threshold.
    """
    if not event_ids:
        return {}

    _ensure_cache_loaded(conn)
    cache = _embedding_cache
    if cache._chunk_count == 0:
        return {}

    event_map = cache.ensure_event_map()
    out: dict[str, list[dict]] = {}
    for eid in dict.fromkeys(event_ids):  # de-dup, preserve order
        indices = event_map.get(eid)
        if not indices:
            continue
        chunks: list[dict] = []
        for i in indices:
            raw_source = cache.source_ids_raw[i]
            chunks.append({
                "chunk_id": cache.chunk_ids[i],
                "conversation_id": cache.conversation_ids[i],
                "chunk_type": cache.chunk_types[i],
                "text": cache.texts[i],
                "source_ids": json.loads(raw_source) if raw_source else [],
            })
        out[eid] = chunks
    return out


def chunk_count(conn: sqlite3.Connection) -> int:
    """Return total number of chunks in the index."""
    cur = conn.execute("SELECT COUNT(*) as cnt FROM chunks")
    return cur.fetchone()["cnt"]


def _encode_embedding(embedding: list[float]) -> bytes:
    """Encode embedding as packed float32 blob."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _decode_embedding(blob: bytes) -> list[float]:
    """Decode packed float32 blob to list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _decode_embedding_numpy(blob: bytes) -> np.ndarray:
    """Decode packed float32 blob to numpy array (zero-copy)."""
    return np.frombuffer(blob, dtype=np.float32)


class IndexCompatError(DriftError):
    """Raised when index metadata is incompatible with current backend configuration."""


def validate_index_compat(
    conn: sqlite3.Connection,
    backend_name: str,
    backend_model: str,
    backend_dimension: int,
    current_schema_version: int,
) -> None:
    """Validate that stored index metadata is compatible with the current backend.

    Args:
        conn: Embeddings database connection.
        backend_name: Current backend name (e.g., "fastembed", "ollama").
        backend_model: Current backend model (e.g., "BAAI/bge-small-en-v1.5").
        backend_dimension: Current embedding dimension.
        current_schema_version: Current schema version constant.

    Raises:
        IndexCompatError: If metadata indicates incompatibility with actionable message.

    Note:
        The active backend is config-driven (``embed.backend``), so remediations name
        a config change (or a rebuild) rather than a search flag. A version-less index
        is a v1 (pre-schema-v2) index and is treated as rebuild-required.
    """
    # An empty index (no chunks) has nothing to be incompatible with: a freshly
    # created v2 DB carries no metadata yet (backend identity + schema_version are
    # written at build time, not schema-creation time). Only a *populated* index is
    # validated — a populated version-less index is the real v1 → rebuild.
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if chunk_count == 0:
        return

    stored_backend = get_meta(conn, "backend")
    stored_model = get_meta(conn, "model")
    stored_dimension = get_meta(conn, "dimension")
    stored_schema = get_meta(conn, "schema_version")

    # Schema version mismatch — a v1 (or version-less) index is derived data with no
    # migration path; rebuild is the only remediation.
    if stored_schema is None or int(stored_schema) != current_schema_version:
        stored_ver = stored_schema or "1 (pre-versioning)"
        raise IndexCompatError(
            f"Embeddings index needs rebuilding.\n\n"
            f"  Index schema version:   {stored_ver}\n"
            f"  Current schema version: {current_schema_version}\n\n"
            f"The index format changed; embeddings are derived data (no migration):\n"
            f"  siftd embed --rebuild"
        )

    # Backend mismatch — the active backend is config, so the fix is a config change
    # (point embed.backend back at the stored backend) or a rebuild.
    if stored_backend is not None and stored_backend != backend_name:
        stored_model_display = f" ({stored_model})" if stored_model else ""
        raise IndexCompatError(
            f"Embedding backend mismatch.\n\n"
            f"  Index backend:    {stored_backend}{stored_model_display}\n"
            f"  Current backend:  {backend_name} ({backend_model})\n\n"
            f"To search the existing index, restore the matching backend in config:\n"
            f"  embed.backend = {config_backend_name(stored_backend)}\n\n"
            f"Or rebuild with the current backend:\n"
            f"  siftd embed --rebuild"
        )

    # Model mismatch (same backend, different model)
    if stored_model is not None and stored_model != backend_model:
        stored_dim = int(stored_dimension) if stored_dimension else "?"
        raise IndexCompatError(
            f"Embedding model mismatch.\n\n"
            f"  Index model:    {stored_model} ({stored_dim} dims)\n"
            f"  Current model:  {backend_model} ({backend_dimension} dims)\n\n"
            f"Different models produce incompatible embeddings; rebuild to switch:\n"
            f"  siftd embed --rebuild"
        )

    # Dimension mismatch (covers cases where model isn't stored but dimensions differ)
    if stored_dimension is not None:
        stored_dim = int(stored_dimension)
        if stored_dim != backend_dimension:
            stored_backend_display = stored_backend or "unknown"
            stored_model_display = stored_model or "unknown"
            raise IndexCompatError(
                f"Embedding dimension mismatch.\n\n"
                f"  Index dimension:   {stored_dim} ({stored_backend_display}/{stored_model_display})\n"
                f"  Current backend:   {backend_dimension} ({backend_name}/{backend_model})\n\n"
                f"The index was built with a different embedding model. To search:\n"
                f"  1. Restore the matching backend:  embed.backend = {config_backend_name(stored_backend_display)}\n"
                f"  2. Or rebuild the index:          siftd embed --rebuild"
            )


def config_backend_name(stored_backend: str | None) -> str:
    """Turn a stored backend name into the value the user would set in config.

    Stored names carry the ``remote:`` prefix (e.g. ``remote:voyage``); ``embed.backend``
    takes the bare preset name (``voyage``). ``fastembed`` and unknown names pass through.
    """
    if stored_backend and stored_backend.startswith("remote:"):
        return stored_backend[len("remote:"):]
    return stored_backend or "<backend>"


def prune_orphaned_chunks(
    main_conn: sqlite3.Connection,
    embeddings_conn: sqlite3.Connection,
) -> int:
    """Delete chunks whose conversation_id no longer exists in the main DB.

    Cross-database: no FK between embeddings DB and main DB, so orphans
    accumulate when conversations are deleted from main.

    Returns count of pruned chunks.
    """
    # Conversation IDs present in main DB
    main_ids = {
        row[0]
        for row in main_conn.execute("SELECT id FROM conversations").fetchall()
    }

    # Conversation IDs referenced by chunks in embeddings DB
    embed_ids = get_indexed_conversation_ids(embeddings_conn)

    orphaned_ids = embed_ids - main_ids
    if not orphaned_ids:
        return 0

    deleted = batched_execute(
        embeddings_conn,
        "DELETE FROM chunks WHERE conversation_id IN ({placeholders})",
        orphaned_ids,
    )
    embeddings_conn.commit()
    if deleted:
        _embedding_cache.invalidate()
    return deleted
