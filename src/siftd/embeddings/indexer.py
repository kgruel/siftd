"""Embeddings index builder.

Builds and maintains the embeddings index for semantic search.
"""

import sqlite3
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
    open_embeddings_db,
    set_meta,
    store_chunk,
)


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

    # Determine which conversations need indexing
    already_indexed = get_indexed_conversation_ids(embed_conn)

    # Get exchange-window chunks from main DB
    main_conn = sqlite3.connect(db)
    main_conn.row_factory = sqlite3.Row

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
    set_meta(embed_conn, "backend", backend.name)
    set_meta(embed_conn, "dimension", str(backend.dimension))
    set_meta(embed_conn, "strategy", "exchange-window")
    set_meta(embed_conn, "target_tokens", str(target_tokens))
    set_meta(embed_conn, "max_tokens", str(max_tokens))

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
    return emb.model.tokenizer  # type: ignore[union-attr]
