"""Build an embeddings database from a strategy file.

Usage:
    python bench/build.py --strategy bench/strategies/exchange-window.json
    python bench/build.py --strategy bench/strategies/exchange-window.json --output /tmp/test.db
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# bench/ is not a package — add src/ to path so siftd imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from siftd.embeddings.fastembed_backend import FastEmbedBackend
from siftd.paths import data_dir
from siftd.storage.embeddings import open_embeddings_db, store_chunk, set_meta


def extract_chunks(main_conn: sqlite3.Connection, params: dict) -> list[dict]:
    """Extract chunks from main DB using the exchange-window chunker."""
    from fastembed import TextEmbedding
    from siftd.embeddings.chunker import extract_exchange_window_chunks

    target_tokens = params.get("target_tokens", 256)
    max_tokens = params.get("max_tokens", 512)
    overlap_tokens = params.get("overlap_tokens", 25)

    emb = TextEmbedding("BAAI/bge-small-en-v1.5")
    tokenizer = emb.model.tokenizer

    return extract_exchange_window_chunks(
        main_conn,
        tokenizer,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )


def build(strategy_path: Path, output_path: Path, db_path: Path) -> None:
    """Build embeddings DB from strategy."""
    strategy = json.loads(strategy_path.read_text())
    params = strategy["params"]

    # Extract chunks from main DB
    main_conn = sqlite3.connect(db_path)
    chunks = extract_chunks(main_conn, params)
    main_conn.close()

    if not chunks:
        print("No chunks extracted. Check strategy params and main DB.")
        return

    print(f"Extracted {len(chunks)} chunks")

    # Embed in batches
    backend = FastEmbedBackend()
    batch_size = 64
    all_embeddings: list[list[float]] = []

    for i in range(0, len(chunks), batch_size):
        batch_texts = [c["text"] for c in chunks[i : i + batch_size]]
        batch_embeddings = backend.embed(batch_texts)
        all_embeddings.extend(batch_embeddings)
        print(f"  Embedded batch {i // batch_size + 1}/{(len(chunks) + batch_size - 1) // batch_size}")

    # Store in embeddings DB
    embed_conn = open_embeddings_db(output_path)
    set_meta(embed_conn, "backend", backend.model)
    set_meta(embed_conn, "dimension", str(backend.dimension))

    for chunk, embedding in zip(chunks, all_embeddings):
        store_chunk(
            embed_conn,
            chunk["conversation_id"],
            chunk["chunk_type"],
            chunk["text"],
            embedding,
            token_count=chunk.get("token_count"),
        )

    embed_conn.commit()
    embed_conn.close()

    print(f"Built {len(chunks)} chunks → {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Build embeddings DB from a strategy file")
    parser.add_argument("--strategy", type=Path, required=True, help="Path to strategy JSON file")
    parser.add_argument("--output", type=Path, default=None, help="Output embeddings DB path")
    parser.add_argument("--db", type=Path, default=None, help="Path to main siftd.db")
    args = parser.parse_args()

    if not args.strategy.exists():
        print(f"Strategy file not found: {args.strategy}")
        sys.exit(1)

    # Resolve main DB path
    db = args.db or (data_dir() / "siftd.db")
    if not db.exists():
        print(f"Main DB not found: {db}")
        sys.exit(1)

    # Resolve output path
    if args.output:
        output = args.output
    else:
        strategy = json.loads(args.strategy.read_text())
        name = strategy.get("name", args.strategy.stem)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = data_dir() / f"embeddings_{name}_{timestamp}.db"

    build(args.strategy, output, db)


if __name__ == "__main__":
    main()
