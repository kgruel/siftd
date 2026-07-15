"""Embeddings index builder — schema-v2 fingerprint lifecycle.

The index is derived data (rebuildable from the main DB), stored in a separate SQLite
file. v2 fixes the v1 append bug (any chunk row marked a conversation permanently
"indexed", so later appends were invisible): each conversation carries a cheap
fingerprint in ``indexed_state``, and a new/changed fingerprint re-chunks + re-embeds
that conversation. Conversations removed from the main DB are pruned on the same diff.

Durability: chunks are embedded and stored in fixed batches, committed per batch, so an
interrupt loses only the in-flight batch. Identity metadata (backend/model/dimension/
schema_version) is stamped once up front, in its own commit before any chunk — so the
index is self-describing even after a zero-chunk build, and a later run can always tell
it apart from a compatible index. A *changed* conversation's old chunks are replaced in
the same transaction that stores its new chunks (never bulk-deleted up front), so an
interrupt keeps the prior coverage of every conversation whose replacement hasn't
committed yet. Removed conversations carry nothing to reinsert, so they are pruned up
front.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from siftd.embeddings.base import get_backend
from siftd.embeddings.chunker import (
    estimate_tokens,
    extract_exchange_window_chunks,
    extract_tool_summary_chunks,
)
from siftd.errors import DriftError
from siftd.paths import db_path as default_db_path
from siftd.paths import embeddings_db_path as default_embed_path
from siftd.storage.embeddings import (
    chunk_count,
    chunk_counts_by_type,
    clear_all,
    config_backend_name,
    delete_conversations,
    get_indexed_state,
    get_meta,
    open_embeddings_db,
    set_meta,
    store_chunk,
    upsert_indexed_state,
)
from siftd.storage.queries import fetch_conversation_fingerprints
from siftd.storage.sqlite import open_database

# Bump when index_meta keys or chunk/state table structure changes incompatibly.
# Version 1: initial (chunks + index_meta; broken append detection).
# Version 2: indexed_state fingerprints + widened source_ids (rebuild-only, no migration).
SCHEMA_VERSION = 2

_BATCH_SIZE = 64


class IncrementalCompatError(DriftError):
    """Raised when an incremental build cannot proceed against the existing index.

    Covers a stale schema (v1), a partial/undescribed index (chunks with no identity
    metadata), and a backend/model mismatch — each remediated by config change or a
    rebuild, never a silent mix.
    """


@dataclass
class IndexStats:
    """Result of an index build."""

    chunks_added: int
    total_chunks: int
    backend_name: str
    dimension: int
    model: str = ""
    chunks_removed: int = 0
    conversations_indexed: int = 0
    conversations_pruned: int = 0


@dataclass
class EmbedIndexStatus:
    """Snapshot for ``siftd embed --status`` — configured backend + built-index stats."""

    configured_backend: str | None
    configured_usable: bool
    configured_reason: str
    index_exists: bool
    needs_rebuild: bool
    stored_backend: str | None
    stored_model: str | None
    stored_dimension: int | None
    schema_version: int | None
    strategy: str | None
    built_at: str | None
    total_chunks: int
    # True when the built index carries chunks whose backend/model differs from the
    # configured one — the next incremental build would raise IncrementalCompatError.
    backend_mismatch: bool = False
    # The ``embed.backend`` config token for the stored backend (``remote:`` stripped),
    # so a mismatch hint can name the exact value to restore.
    stored_backend_config: str | None = None
    chunk_counts: dict[str, int] = field(default_factory=dict)
    conversations_indexed: int = 0
    conversations_total: int = 0
    conversations_stale: int = 0
    db_size_bytes: int = 0


def _chunk_bounds(backend) -> tuple[int, int, int]:
    """(target, max, overlap) estimator-token bounds for the active backend.

    fastembed (bge) has a hard 512-token ceiling and the char-based estimator errs on
    token-dense content, so the max is pulled in to 384 for margin. Remote backends have
    much larger ceilings (8k–32k) and enable provider-side truncation, so 256/512 is safe.
    """
    if backend.name == "fastembed":
        return 256, 384, 25
    return 256, 512, 25


def build_embeddings_index(
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
    rebuild: bool = False,
    verbose: bool = False,
) -> IndexStats:
    """Build or incrementally update the embeddings index.

    Args:
        db_path: Path to the main database. Uses the configured default if omitted.
        embed_db_path: Path to the embeddings database. Uses the default if omitted.
        rebuild: If True, clear everything and rebuild from scratch.
        verbose: Print progress messages.

    Returns:
        IndexStats with add/remove counts and backend identity.

    Raises:
        FileNotFoundError: If the main database doesn't exist.
        IncrementalCompatError: If an incremental build can't proceed (stale schema,
            undescribed index, or backend/model mismatch).
        RuntimeError / EmbeddingConfigError: If no embedding backend is configured.
    """
    db = db_path or default_db_path()
    embed_db = embed_db_path or default_embed_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    backend = get_backend(verbose=verbose)
    target_tokens, max_tokens, overlap_tokens = _chunk_bounds(backend)

    embed_conn = open_embeddings_db(embed_db)
    seeded_dimension = False
    try:
        if rebuild:
            if verbose:
                print("Clearing existing index...")
            clear_all(embed_conn)
        else:
            _validate_incremental_compat(embed_conn, backend)
            # Incremental on an existing index — the index's stored dimension IS the
            # expectation. A remote backend that hasn't learned its width yet (None) is
            # seeded from it, so the RemoteBackend's own response validation enforces the
            # index's width end-to-end and dimension_known stays True below (no re-stamp).
            # Chunks-present guard: a zero-chunk index passes validation unconditionally
            # (nothing to mix), so its meta may describe a PRIOR backend — that stale
            # width must not be enforced on this one.
            if backend.dimension is None and chunk_count(embed_conn) > 0:
                stored_dim = get_meta(embed_conn, "dimension")
                if stored_dim is not None:
                    backend.dimension = int(stored_dim)
                    seeded_dimension = True

        # Identity-meta-first: stamp the index's self-description once, up front, in its own
        # commit — before any chunk. A zero-chunk build (all conversations contentless) is
        # still fully self-describing, and the validators' chunk_count==0 early-outs keep a
        # stamped-but-empty index safe. A remote backend's dimension is unknown until the
        # first embed call, so it is filled in with the first batch below; re-stamping on an
        # incremental run is idempotent and never overwrites a known dimension with None.
        _stamp_identity_meta(embed_conn, backend, target_tokens, max_tokens, overlap_tokens)
        embed_conn.commit()

        # --- Diff main-DB fingerprints against recorded index state ---
        main_conn = open_database(db, read_only=True)
        try:
            fingerprints = fetch_conversation_fingerprints(main_conn)
            stored_state = get_indexed_state(embed_conn)
            main_ids = set(fingerprints)

            to_index = {
                cid for cid, fp in fingerprints.items()
                if stored_state.get(cid, (None, 0))[0] != fp
            }
            to_prune = set(stored_state) - main_ids

            # Prune removed conversations up front: nothing is reinserted for them, so an
            # early sweep only frees space and cannot lose coverage. Changed conversations
            # are NOT swept here — each one's old chunks are replaced inside the same batch
            # transaction that stores its new chunks (below), so an interrupt keeps the
            # prior coverage of every conversation whose replacement hasn't committed.
            n_removed = delete_conversations(embed_conn, to_prune)
            embed_conn.commit()

            if not to_index:
                total = chunk_count(embed_conn)
                if verbose and n_removed:
                    print(f"Pruned {n_removed} chunk(s) from {len(to_prune)} removed conversation(s).")
                return IndexStats(
                    chunks_added=0, total_chunks=total, backend_name=backend.name,
                    dimension=backend.dimension or 0, model=backend.model,
                    chunks_removed=n_removed, conversations_indexed=0,
                    conversations_pruned=len(to_prune),
                )

            exclude = main_ids - to_index
            exchange_chunks = extract_exchange_window_chunks(
                main_conn,
                target_tokens=target_tokens,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                exclude_conversation_ids=exclude,
            )
            tool_targets = _filter_conversations_with_tool_calls(main_conn, to_index)
            tool_chunks: list[dict] = []
            if tool_targets:
                tool_chunks = extract_tool_summary_chunks(main_conn, conversation_ids=tool_targets)
                for c in tool_chunks:
                    c["token_count"] = estimate_tokens(c["text"])
        finally:
            main_conn.close()

        # Group each conversation's chunks contiguously so a conversation's indexed_state
        # is written exactly when its last chunk is committed, and so its old chunks are
        # replaced in the same transaction as its first new one.
        ordered, expected = _order_by_conversation(exchange_chunks, tool_chunks)

        if verbose and ordered:
            print(f"Embedding {len(ordered)} new chunks...")

        dimension_known = backend.dimension is not None
        stored_counts: dict[str, int] = defaultdict(int)
        replaced: set[str] = set()  # changed convs whose prior chunks we've swept
        upserted: set[str] = set()
        n_added = 0

        for i in range(0, len(ordered), _BATCH_SIZE):
            batch = ordered[i : i + _BATCH_SIZE]
            embeddings = backend.embed_documents([c["text"] for c in batch])
            # A remote backend only reveals its dimension once embed_documents returns;
            # fill it in with (atomically inside) this batch's commit.
            if not dimension_known and backend.dimension is not None:
                embed_conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('dimension', ?)",
                    (str(backend.dimension),),
                )
                dimension_known = True
            # strict=True: a short embeddings response must never silently truncate — the
            # post-loop sweep would then stamp the shorted conversation's fingerprint as
            # current, hiding the gap permanently (RemoteBackend already guards this at the
            # response boundary; this is the belt for any backend that doesn't).
            for c, emb in zip(batch, embeddings, strict=True):
                cid = c["conversation_id"]
                # Replace this conversation's prior chunks in the SAME transaction as its
                # first new chunk — an interrupt before this commit leaves the old chunks
                # (and coverage) intact rather than wiping every stale conversation up front.
                if cid not in replaced:
                    n_removed += delete_conversations(embed_conn, {cid})
                    replaced.add(cid)
                store_chunk(
                    embed_conn,
                    conversation_id=cid,
                    chunk_type=c["chunk_type"],
                    text=c["text"],
                    embedding=emb,
                    token_count=c["token_count"],
                    source_ids=c.get("source_ids"),
                )
                stored_counts[cid] += 1
                n_added += 1
            for cid, cnt in stored_counts.items():
                if cid not in upserted and cnt == expected[cid]:
                    upsert_indexed_state(embed_conn, cid, fingerprints[cid], cnt)
                    upserted.add(cid)
            embed_conn.commit()
            if verbose and len(ordered) > _BATCH_SIZE:
                print(f"  {min(i + _BATCH_SIZE, len(ordered))}/{len(ordered)}")

        # Changed conversations that now yield zero chunks (content emptied) were never
        # reached in the loop, so their stale chunks still linger — sweep them now. Then
        # mark every remaining to_index conversation indexed so it isn't rescanned. These
        # rides the built_at commit below.
        zero_chunk = to_index - replaced
        n_removed += delete_conversations(embed_conn, zero_chunk)
        for cid in to_index:
            if cid not in upserted:
                upsert_indexed_state(embed_conn, cid, fingerprints[cid], stored_counts.get(cid, 0))
                upserted.add(cid)
        set_meta(embed_conn, "built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        total = chunk_count(embed_conn)
        if verbose:
            print(f"Done. Index has {total} chunks ({backend.name}, dim={backend.dimension}).")

        return IndexStats(
            chunks_added=n_added,
            total_chunks=total,
            backend_name=backend.name,
            dimension=backend.dimension or 0,
            model=backend.model,
            chunks_removed=n_removed,
            conversations_indexed=len(upserted),
            conversations_pruned=len(to_prune),
        )
    finally:
        # The seed is this RUN's expectation, not the backend's own knowledge: the backend
        # object is process-cached (base.get_backend), so a stale seeded width would leak
        # into a later build in the same process (e.g. a rebuild under serve after a
        # width-mismatch failure) and keep failing it.
        if seeded_dimension:
            backend.dimension = None
        embed_conn.close()


def _order_by_conversation(
    exchange_chunks: list[dict], tool_chunks: list[dict]
) -> tuple[list[dict], dict[str, int]]:
    """Interleave chunks so each conversation's chunks are contiguous.

    Returns (ordered_chunks, expected_count_by_conversation). Contiguity lets the builder
    upsert a conversation's indexed_state precisely when its final chunk commits.
    """
    by_conv: dict[str, list[dict]] = defaultdict(list)
    for c in exchange_chunks:
        by_conv[c["conversation_id"]].append(c)
    for c in tool_chunks:
        by_conv[c["conversation_id"]].append(c)

    ordered: list[dict] = []
    expected: dict[str, int] = {}
    for cid, cchunks in by_conv.items():
        expected[cid] = len(cchunks)
        ordered.extend(cchunks)
    return ordered, expected


def _stamp_identity_meta(conn, backend, target_tokens: int, max_tokens: int, overlap_tokens: int) -> None:
    """Stamp backend identity + strategy metadata (no commit — caller commits).

    Called once up front so the index describes itself before any chunk is written. The
    ``dimension`` key is written only when already known: a remote backend reports None
    until its first embed call, and on an incremental run the stored dimension must not be
    clobbered with None — the batch loop fills it in once the first embed reveals it.
    """
    rows = {
        "schema_version": str(SCHEMA_VERSION),
        "backend": backend.name,
        "model": backend.model,
        "strategy": "exchange-window",
        "include_tool_summaries": "1",
        "target_tokens": str(target_tokens),
        "max_tokens": str(max_tokens),
        "overlap_tokens": str(overlap_tokens),
    }
    if backend.dimension is not None:
        rows["dimension"] = str(backend.dimension)
    conn.executemany(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)", list(rows.items())
    )


def _filter_conversations_with_tool_calls(conn, conversation_ids: set[str]) -> set[str]:
    """Return only conversation_ids that have at least one tool_call row."""
    if not conversation_ids:
        return set()

    keep: set[str] = set()
    ids = list(conversation_ids)
    batch_size = 900  # stay under SQLite variable limits
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        ph = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT DISTINCT conversation_id FROM events"
            f" WHERE kind = 'tool_call' AND conversation_id IN ({ph})",
            batch,
        ).fetchall()
        keep.update(r[0] for r in rows)

    return keep


def _validate_incremental_compat(conn, backend) -> None:
    """Guard an incremental build against a stale/undescribed/mismatched index.

    Empty indexes (first build) always pass. A v1 (or version-less) index is derived
    data with no migration — rebuild. Chunks present with no identity metadata, or a
    backend/model that differs from the stored one, are also rebuild/config situations,
    never a silent mix.
    """
    if chunk_count(conn) == 0:
        return

    stored_schema = get_meta(conn, "schema_version")
    stored_backend = get_meta(conn, "backend")
    stored_model = get_meta(conn, "model")

    if stored_schema is None or int(stored_schema) != SCHEMA_VERSION:
        stored_ver = stored_schema or "1 (pre-versioning)"
        raise IncrementalCompatError(
            f"Embeddings index needs rebuilding (schema v{stored_ver} → v{SCHEMA_VERSION}).\n\n"
            f"Embeddings are derived data with no migration path:\n"
            f"  siftd embed --rebuild"
        )

    if stored_backend is None or stored_model is None:
        raise IncrementalCompatError(
            "Embeddings index is missing identity metadata (partial or corrupt index).\n\n"
            "Rebuild to restore a consistent index:\n"
            "  siftd embed --rebuild"
        )

    if stored_backend != backend.name:
        raise IncrementalCompatError(
            f"Cannot add to an index built with a different backend.\n\n"
            f"  Index backend:    {stored_backend} ({stored_model})\n"
            f"  Current backend:  {backend.name} ({backend.model})\n\n"
            f"Restore the matching backend in config:\n"
            f"  embed.backend = {config_backend_name(stored_backend)}\n\n"
            f"Or rebuild with the current backend:\n"
            f"  siftd embed --rebuild"
        )

    if stored_model != backend.model:
        raise IncrementalCompatError(
            f"Cannot add to an index built with a different model.\n\n"
            f"  Index model:    {stored_model}\n"
            f"  Current model:  {backend.model}\n\n"
            f"Different models produce incompatible embeddings; rebuild to switch:\n"
            f"  siftd embed --rebuild"
        )

    # Dimension: a same-model narrowing (e.g. embed.dimensions 1536 → 512 via matryoshka
    # truncation) would mix vector widths in one index. Checked here, before build stamps
    # the current dimension over the stored one — a remote backend that hasn't learned its
    # dimension yet (None) can't conflict and is seeded from the stored value by the caller.
    stored_dim = get_meta(conn, "dimension")
    if (
        stored_dim is not None
        and backend.dimension is not None
        and int(stored_dim) != backend.dimension
    ):
        raise IncrementalCompatError(
            f"Cannot add to an index with a different embedding dimension.\n\n"
            f"  Index dimension:    {stored_dim}\n"
            f"  Current dimension:  {backend.dimension}\n\n"
            f"Set embed.dimensions to match the index:\n"
            f"  embed.dimensions = {stored_dim}\n\n"
            f"Or rebuild at the current dimension:\n"
            f"  siftd embed --rebuild"
        )


def embed_index_status(
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
) -> EmbedIndexStatus:
    """Report configured-backend + built-index state for ``siftd embed --status``.

    Cheap by construction: the configured backend comes from ``embedding_status()`` (no
    ONNX model load), and index model/dimension are read from stored metadata rather than
    by resolving the live backend. Staleness is the fingerprint diff against indexed_state.
    """
    from siftd.embeddings.availability import embedding_status

    db = db_path or default_db_path()
    embed_db = embed_db_path or default_embed_path()

    st = embedding_status()

    fingerprints: dict[str, str] = {}
    if db.exists():
        main_conn = open_database(db, read_only=True)
        try:
            fingerprints = fetch_conversation_fingerprints(main_conn)
        finally:
            main_conn.close()
    conversations_total = len(fingerprints)

    if not embed_db.exists():
        return EmbedIndexStatus(
            configured_backend=st.backend,
            configured_usable=st.usable,
            configured_reason=st.reason,
            index_exists=False,
            needs_rebuild=False,
            stored_backend=None,
            stored_model=None,
            stored_dimension=None,
            schema_version=None,
            strategy=None,
            built_at=None,
            total_chunks=0,
            backend_mismatch=False,
            chunk_counts={},
            conversations_indexed=0,
            conversations_total=conversations_total,
            conversations_stale=conversations_total,
            db_size_bytes=0,
        )

    embed_conn = open_embeddings_db(embed_db, read_only=True)
    try:
        total_chunks = chunk_count(embed_conn)
        populated = total_chunks > 0
        stored_schema = get_meta(embed_conn, "schema_version")
        schema_version = int(stored_schema) if stored_schema is not None else None
        stored_backend = get_meta(embed_conn, "backend")
        stored_model = get_meta(embed_conn, "model")
        stored_dim = get_meta(embed_conn, "dimension")
        indexed_state = get_indexed_state(embed_conn)

        # Tri-state honesty: an empty index (no chunks) is "unbuilt", never rebuild-worthy —
        # an incremental `siftd embed` suffices. needs_rebuild fires only for a *populated*
        # index whose schema no longer matches. backend_mismatch mirrors the check the next
        # incremental build would hit (name or model differs from the configured backend),
        # so --status can't report "healthy" moments before IncrementalCompatError.
        needs_rebuild = populated and schema_version != SCHEMA_VERSION
        backend_mismatch = bool(
            populated
            and not needs_rebuild
            and stored_backend
            and st.backend
            and (
                stored_backend != st.backend
                or (stored_model and st.model and stored_model != st.model)
            )
        )
        # Staleness is the fingerprint diff against indexed_state — this stays correct for
        # a built-but-empty index (contentless conversations carry an indexed_state row and
        # so read as indexed, not stale). A pending rebuild is the one override: it
        # invalidates the whole index, so every conversation is outstanding.
        stale = sum(
            1 for cid, fp in fingerprints.items()
            if indexed_state.get(cid, (None, 0))[0] != fp
        )
        if needs_rebuild:
            stale = conversations_total
        status = EmbedIndexStatus(
            configured_backend=st.backend,
            configured_usable=st.usable,
            configured_reason=st.reason,
            index_exists=True,
            needs_rebuild=needs_rebuild,
            stored_backend=stored_backend,
            stored_model=stored_model,
            stored_dimension=int(stored_dim) if stored_dim is not None else None,
            schema_version=schema_version,
            strategy=get_meta(embed_conn, "strategy"),
            built_at=get_meta(embed_conn, "built_at"),
            total_chunks=total_chunks,
            backend_mismatch=backend_mismatch,
            stored_backend_config=config_backend_name(stored_backend) if stored_backend else None,
            chunk_counts=chunk_counts_by_type(embed_conn),
            conversations_indexed=len(indexed_state),
            conversations_total=conversations_total,
            conversations_stale=stale,
            db_size_bytes=embed_db.stat().st_size,
        )
    finally:
        embed_conn.close()
    return status
