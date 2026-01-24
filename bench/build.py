"""Build an embeddings database from a strategy file.

Usage:
    python bench/build.py --strategy bench/strategies/min-100.json
    python bench/build.py --strategy bench/strategies/min-100.json --output /tmp/test.db
    python bench/build.py --strategy bench/strategies/baseline.json --sample 500
    python bench/build.py --strategy bench/strategies/baseline.json --dry-run --sample 100
"""

import argparse
import json
import random
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from embeddings.fastembed_backend import FastEmbedBackend
from paths import data_dir
from storage.embeddings import open_embeddings_db, store_chunk, set_meta


def extract_chunks(
    main_conn: sqlite3.Connection, params: dict, conversation_ids: set | None = None
) -> list[dict]:
    """Extract chunks from main DB according to strategy params.

    If conversation_ids is provided, only chunks from those conversations are included.
    """
    min_chars = params.get("min_chars", 20)
    chunk_types = params.get("chunk_types", ["prompt", "response"])
    concat = params.get("concat", False)

    chunks = []

    if "prompt" in chunk_types:
        chunks.extend(_extract_prompt_chunks(main_conn, min_chars, concat))

    if "response" in chunk_types:
        chunks.extend(_extract_response_chunks(main_conn, min_chars, concat))

    if conversation_ids is not None:
        chunks = [c for c in chunks if c["conversation_id"] in conversation_ids]

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


SAMPLE_SEED = 42

HISTOGRAM_BUCKETS = [
    (0, 64),
    (64, 128),
    (128, 256),
    (256, 512),
    (512, 1024),
    (1024, float("inf")),
]

MODEL_MAX_TOKENS = 512


def sample_conversations(conn: sqlite3.Connection, n: int) -> set[str]:
    """Return a deterministic random sample of n conversation IDs."""
    rows = conn.execute(
        "SELECT DISTINCT conversation_id FROM prompts "
        "UNION SELECT DISTINCT conversation_id FROM responses"
    ).fetchall()
    all_ids = [r[0] for r in rows]
    if n >= len(all_ids):
        return set(all_ids)
    rng = random.Random(SAMPLE_SEED)
    return set(rng.sample(all_ids, n))


def get_tokenizer():
    """Load the fastembed tokenizer with truncation disabled."""
    from fastembed import TextEmbedding

    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    tokenizer = model.model.tokenizer
    tokenizer.no_truncation()
    return tokenizer


def print_dry_run_stats(chunks: list[dict]) -> None:
    """Print chunk statistics without embedding."""
    tokenizer = get_tokenizer()
    texts = [c["text"] for c in chunks]
    token_counts = [len(tokenizer.encode(t).ids) for t in texts]

    n = len(token_counts)
    sorted_counts = sorted(token_counts)

    print(f"\n{'=' * 60}")
    print(f"  DRY RUN: Chunk Statistics (n={n:,})")
    print(f"{'=' * 60}")

    # Token distribution
    print(f"\n  Token distribution:")
    print(f"    Min:    {sorted_counts[0]:>6,}")
    print(f"    Max:    {sorted_counts[-1]:>6,}")
    print(f"    Mean:   {statistics.mean(sorted_counts):>6,.1f}")
    print(f"    Median: {statistics.median(sorted_counts):>6,.0f}")
    p95_idx = min(int(n * 0.95), n - 1)
    print(f"    P95:    {sorted_counts[p95_idx]:>6,}")

    # Exceeds max_tokens
    exceeds = sum(1 for c in sorted_counts if c > MODEL_MAX_TOKENS)
    print(f"\n  Exceeds {MODEL_MAX_TOKENS} tokens (model max): {exceeds:,} ({100*exceeds/n:.1f}%)")

    # Histogram
    print(f"\n  Histogram:")
    histogram = []
    for low, high in HISTOGRAM_BUCKETS:
        count = sum(1 for c in token_counts if low <= c < high)
        histogram.append((low, high, count))
    max_count = max(c for _, _, c in histogram) if histogram else 1
    bar_width = 30
    for low, high, count in histogram:
        label = f"{low}+" if high == float("inf") else f"{low}-{high}"
        pct = 100 * count / n
        bar_len = int(bar_width * count / max_count) if max_count > 0 else 0
        bar = "#" * bar_len
        print(f"    {label:>8s}: {count:>6,} ({pct:>5.1f}%) |{bar}")

    # Chunks per conversation
    conv_counts: dict[str, int] = {}
    for c in chunks:
        conv_counts[c["conversation_id"]] = conv_counts.get(c["conversation_id"], 0) + 1
    conv_values = list(conv_counts.values())
    print(f"\n  Chunks per conversation:")
    print(f"    Min:  {min(conv_values):>6,}")
    print(f"    Max:  {max(conv_values):>6,}")
    print(f"    Mean: {statistics.mean(conv_values):>6,.1f}")
    print()


def build(
    strategy_path: Path,
    output_path: Path,
    db_path: Path,
    sample: int | None = None,
    dry_run: bool = False,
) -> None:
    """Build embeddings DB from strategy."""
    strategy = json.loads(strategy_path.read_text())
    params = strategy["params"]

    # Extract chunks from main DB
    main_conn = sqlite3.connect(db_path)

    conversation_ids = None
    if sample is not None:
        conversation_ids = sample_conversations(main_conn, sample)
        print(f"Sampled {len(conversation_ids)} conversations (seed={SAMPLE_SEED})")

    chunks = extract_chunks(main_conn, params, conversation_ids)
    main_conn.close()

    if not chunks:
        print("No chunks extracted. Check strategy params and main DB.")
        return

    print(f"Extracted {len(chunks)} chunks")

    if dry_run:
        print_dry_run_stats(chunks)
        return

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
    if sample is not None:
        set_meta(embed_conn, "sample_size", str(sample))
        set_meta(embed_conn, "sample_seed", str(SAMPLE_SEED))

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
    parser.add_argument("--sample", type=int, default=None, help="Limit to N randomly-sampled conversations (seed=42)")
    parser.add_argument("--dry-run", action="store_true", help="Print chunk stats without embedding")
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
    elif not args.dry_run:
        strategy = json.loads(args.strategy.read_text())
        name = strategy.get("name", args.strategy.stem)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sample_suffix = f"_sample{args.sample}" if args.sample else ""
        output = data_dir() / f"embeddings_{name}_{timestamp}{sample_suffix}.db"
    else:
        output = None  # Not used in dry-run

    build(args.strategy, output, db, sample=args.sample, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
