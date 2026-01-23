"""Build an embeddings database from a strategy file.

Usage:
    python bench/build.py --strategy bench/strategies/min-100.json
    python bench/build.py --strategy bench/strategies/min-100.json --output /tmp/test.db
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from embeddings.fastembed_backend import FastEmbedBackend
from paths import data_dir
from storage.embeddings import open_embeddings_db, store_chunk, set_meta


def extract_chunks(main_conn: sqlite3.Connection, params: dict) -> list[dict]:
    """Extract chunks from main DB according to strategy params."""
    min_chars = params.get("min_chars", 20)
    chunk_types = params.get("chunk_types", ["prompt", "response"])
    concat = params.get("concat", False)

    chunks = []

    if "prompt" in chunk_types:
        chunks.extend(_extract_prompt_chunks(main_conn, min_chars, concat))

    if "response" in chunk_types:
        chunks.extend(_extract_response_chunks(main_conn, min_chars, concat))

    return chunks


def _extract_prompt_chunks(
    conn: sqlite3.Connection, min_chars: int, concat: bool
) -> list[dict]:
    """Extract prompt chunks, either per-block or concatenated per-turn."""
    if concat:
        rows = conn.execute("""
            SELECT
                p.conversation_id,
                pc.prompt_id,
                json_extract(pc.content, '$.text') AS text,
                pc.block_index
            FROM prompt_content pc
            JOIN prompts p ON p.id = pc.prompt_id
            WHERE pc.block_type = 'text'
              AND json_extract(pc.content, '$.text') IS NOT NULL
            ORDER BY pc.prompt_id, pc.block_index
        """).fetchall()

        groups: dict[str, dict] = {}
        for row in rows:
            pid = row[1]
            if pid not in groups:
                groups[pid] = {"conversation_id": row[0], "texts": []}
            groups[pid]["texts"].append(row[2])

        chunks = []
        for group in groups.values():
            text = "\n".join(group["texts"])
            if len(text) >= min_chars:
                chunks.append({
                    "conversation_id": group["conversation_id"],
                    "chunk_type": "prompt",
                    "text": text,
                })
        return chunks
    else:
        rows = conn.execute("""
            SELECT
                p.conversation_id,
                json_extract(pc.content, '$.text') AS text
            FROM prompt_content pc
            JOIN prompts p ON p.id = pc.prompt_id
            WHERE pc.block_type = 'text'
              AND json_extract(pc.content, '$.text') IS NOT NULL
        """).fetchall()

        return [
            {"conversation_id": row[0], "chunk_type": "prompt", "text": row[1]}
            for row in rows
            if len(row[1]) >= min_chars
        ]


def _extract_response_chunks(
    conn: sqlite3.Connection, min_chars: int, concat: bool
) -> list[dict]:
    """Extract response chunks, either per-block or concatenated per-turn."""
    if concat:
        rows = conn.execute("""
            SELECT
                r.conversation_id,
                rc.response_id,
                json_extract(rc.content, '$.text') AS text,
                rc.block_index
            FROM response_content rc
            JOIN responses r ON r.id = rc.response_id
            WHERE rc.block_type = 'text'
              AND json_extract(rc.content, '$.text') IS NOT NULL
            ORDER BY rc.response_id, rc.block_index
        """).fetchall()

        groups: dict[str, dict] = {}
        for row in rows:
            rid = row[1]
            if rid not in groups:
                groups[rid] = {"conversation_id": row[0], "texts": []}
            groups[rid]["texts"].append(row[2])

        chunks = []
        for group in groups.values():
            text = "\n".join(group["texts"])
            if len(text) >= min_chars:
                chunks.append({
                    "conversation_id": group["conversation_id"],
                    "chunk_type": "response",
                    "text": text,
                })
        return chunks
    else:
        rows = conn.execute("""
            SELECT
                r.conversation_id,
                json_extract(rc.content, '$.text') AS text
            FROM response_content rc
            JOIN responses r ON r.id = rc.response_id
            WHERE rc.block_type = 'text'
              AND json_extract(rc.content, '$.text') IS NOT NULL
        """).fetchall()

        return [
            {"conversation_id": row[0], "chunk_type": "response", "text": row[1]}
            for row in rows
            if len(row[1]) >= min_chars
        ]


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
        )

    embed_conn.commit()
    embed_conn.close()

    print(f"Built {len(chunks)} chunks → {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Build embeddings DB from a strategy file")
    parser.add_argument("--strategy", type=Path, required=True, help="Path to strategy JSON file")
    parser.add_argument("--output", type=Path, default=None, help="Output embeddings DB path")
    parser.add_argument("--db", type=Path, default=None, help="Path to main tbd.db")
    args = parser.parse_args()

    if not args.strategy.exists():
        print(f"Strategy file not found: {args.strategy}")
        sys.exit(1)

    # Resolve main DB path
    db = args.db or (data_dir() / "tbd.db")
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
