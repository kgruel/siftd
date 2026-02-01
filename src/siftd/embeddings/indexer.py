"""Embeddings index builder.

Builds and maintains the embeddings index for semantic search.
"""

import time
from dataclasses import dataclass
from pathlib import Path

from siftd.embeddings import get_backend
from siftd.embeddings.chunker import extract_exchange_window_chunks
from siftd.paths import db_path as default_db_path
from siftd.paths import embeddings_db_path as default_embed_path
from siftd.storage.embeddings import (
    chunk_count,
    clear_all,
    get_indexed_conversation_ids,
    get_meta,
    open_embeddings_db,
    set_meta,
    store_chunk,
)
from siftd.storage.sqlite import open_database

# Bump when index_meta keys or chunks table structure changes incompatibly.
# Version 1: Initial version with model tracking
SCHEMA_VERSION = 1


class IncrementalCompatError(Exception):
    """Raised when incremental indexing would mix incompatible backends."""

    pass


@dataclass
class IndexStats:
    """Statistics from an index build operation."""

    chunks_added: int
    total_chunks: int
    backend_name: str
    dimension: int


def build_embeddings_index(
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
    rebuild: bool = False,
    backend_name: str | None = None,
    verbose: bool = False,
) -> IndexStats:
    """Build or update the embeddings index.

    Args:
        db_path: Path to main database. Uses default if not specified.
        embed_db_path: Path to embeddings database. Uses default if not specified.
        rebuild: If True, clear and rebuild from scratch.
        backend_name: Preferred embedding backend name.
        verbose: Print progress messages.

    Returns:
        IndexStats with counts and backend info.

    Raises:
        FileNotFoundError: If main database doesn't exist.
        RuntimeError: If no embedding backend is available.
    """
    db = db_path or default_db_path()
    embed_db = embed_db_path or default_embed_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    backend = get_backend(preferred=backend_name, verbose=verbose)
    embed_conn = open_embeddings_db(embed_db)

    if rebuild:
        if verbose:
            print("Clearing existing index...")
        clear_all(embed_conn)
    else:
        # For incremental indexing, validate compatibility with existing index
        _validate_incremental_compat(embed_conn, backend)

    # Determine which conversations need indexing
    already_indexed = get_indexed_conversation_ids(embed_conn)

    # Get exchange-window chunks from main DB
    main_conn = open_database(db, read_only=True)

    tokenizer = _get_tokenizer()
    target_tokens = 256
    max_tokens = 512
    overlap_tokens = 25

    chunks = extract_exchange_window_chunks(
        main_conn,
        tokenizer,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        exclude_conversation_ids=already_indexed,
    )
    main_conn.close()

    if not chunks:
        total = chunk_count(embed_conn)
        embed_conn.close()
        return IndexStats(
            chunks_added=0,
            total_chunks=total,
            backend_name=backend.name,
            dimension=backend.dimension,
        )

    if verbose:
        print(f"Embedding {len(chunks)} new chunks...")

    # Batch embed
    texts = [c["text"] for c in chunks]
    batch_size = 64
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        all_embeddings.extend(backend.embed(batch))
        if verbose and len(texts) > batch_size:
            done = min(i + batch_size, len(texts))
            print(f"  {done}/{len(texts)}")

    # Store with real token counts
    for chunk, embedding in zip(chunks, all_embeddings):
        store_chunk(
            embed_conn,
            conversation_id=chunk["conversation_id"],
            chunk_type=chunk["chunk_type"],
            text=chunk["text"],
            embedding=embedding,
            token_count=chunk["token_count"],
            source_ids=chunk.get("source_ids"),
        )
    embed_conn.commit()

    # Record strategy metadata
    set_meta(embed_conn, "schema_version", str(SCHEMA_VERSION))
    set_meta(embed_conn, "backend", backend.name)
    set_meta(embed_conn, "model", backend.model)
    set_meta(embed_conn, "dimension", str(backend.dimension))
    set_meta(embed_conn, "strategy", "exchange-window")
    set_meta(embed_conn, "target_tokens", str(target_tokens))
    set_meta(embed_conn, "max_tokens", str(max_tokens))
    set_meta(embed_conn, "overlap_tokens", str(overlap_tokens))
    set_meta(embed_conn, "built_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    total = chunk_count(embed_conn)
    chunks_added = len(chunks)

    if verbose:
        print(f"Done. Index has {total} chunks ({backend.name}, dim={backend.dimension}).")

    embed_conn.close()

    return IndexStats(
        chunks_added=chunks_added,
        total_chunks=total,
        backend_name=backend.name,
        dimension=backend.dimension,
    )


def _get_tokenizer():
    """Get the fastembed tokenizer for token counting."""
    from fastembed import TextEmbedding

    emb = TextEmbedding("BAAI/bge-small-en-v1.5")
    return emb.model.tokenizer


def _validate_incremental_compat(conn, backend) -> None:
    """Validate that incremental indexing is compatible with existing index.

    Args:
        conn: Embeddings database connection.
        backend: Current embedding backend instance.

    Raises:
        IncrementalCompatError: If adding would mix incompatible embeddings.

    Note:
        Empty indexes (first build) always pass validation.
    """
    # Check if index has any chunks
    total = chunk_count(conn)
    if total == 0:
        # First build, no compatibility check needed
        return

    stored_backend = get_meta(conn, "backend")
    stored_model = get_meta(conn, "model")

    # Backend mismatch
    if stored_backend is not None and stored_backend != backend.name:
        stored_model_display = f" ({stored_model})" if stored_model else ""
        raise IncrementalCompatError(
            f"Cannot add to index with different backend.\n\n"
            f"  Index backend:    {stored_backend}{stored_model_display}\n"
            f"  Current backend:  {backend.name} ({backend.model})\n\n"
            f"Options:\n"
            f"  1. Use matching backend:  siftd search --index --backend {stored_backend}\n"
            f"  2. Rebuild from scratch:  siftd search --rebuild"
        )

    # Model mismatch (same backend, different model)
    if stored_model is not None and stored_model != backend.model:
        raise IncrementalCompatError(
            f"Cannot add to index with different model.\n\n"
            f"  Index model:    {stored_model}\n"
            f"  Current model:  {backend.model}\n\n"
            f"Rebuild required to switch models:\n"
            f"  siftd search --rebuild"
        )
