"""Corpus token-length analysis against the fastembed tokenizer.

Profiles chunks in an embeddings DB to understand token distribution
relative to the model's max_seq_length (512 for bge-small-en-v1.5).

Usage:
    python bench/corpus_analysis.py
    python bench/corpus_analysis.py --embed-db /path/to/embeddings.db
"""

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

# bench/ is not a package — add src/ to path so siftd imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from siftd.paths import data_dir


MODEL_MAX_TOKENS = 512
TARGET_MAX_TOKENS = 256

HISTOGRAM_BUCKETS = [
    (0, 64),
    (64, 128),
    (128, 256),
    (256, 512),
    (512, 1024),
    (1024, float("inf")),
]


def get_tokenizer():
    """Load the fastembed tokenizer for bge-small-en-v1.5.

    Truncation is disabled so we get true token counts — the model truncates
    at 512, but we want to see how much content is lost.
    """
    from fastembed import TextEmbedding

    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    tokenizer = model.model.tokenizer
    tokenizer.no_truncation()
    return tokenizer


def load_chunks(db_path: Path) -> list[dict]:
    """Load all chunks from the embeddings DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT text, chunk_type FROM chunks").fetchall()
    conn.close()
    return [{"text": row["text"], "chunk_type": row["chunk_type"]} for row in rows]


def tokenize_texts(tokenizer, texts: list[str]) -> list[int]:
    """Tokenize a list of texts and return token counts."""
    return [len(tokenizer.encode(text).ids) for text in texts]


def compute_stats(token_counts: list[int]) -> dict:
    """Compute distribution statistics for a list of token counts."""
    if not token_counts:
        return {}
    sorted_counts = sorted(token_counts)
    n = len(sorted_counts)
    return {
        "count": n,
        "min": sorted_counts[0],
        "max": sorted_counts[-1],
        "mean": statistics.mean(sorted_counts),
        "median": statistics.median(sorted_counts),
        "p5": sorted_counts[int(n * 0.05)],
        "p25": sorted_counts[int(n * 0.25)],
        "p75": sorted_counts[int(n * 0.75)],
        "p95": sorted_counts[min(int(n * 0.95), n - 1)],
        "exceeds_512": sum(1 for c in sorted_counts if c > MODEL_MAX_TOKENS),
        "exceeds_256": sum(1 for c in sorted_counts if c > TARGET_MAX_TOKENS),
    }


def compute_histogram(token_counts: list[int]) -> list[tuple]:
    """Compute histogram bucket counts."""
    results = []
    for low, high in HISTOGRAM_BUCKETS:
        count = sum(1 for c in token_counts if low <= c < high)
        results.append((low, high, count))
    return results


def format_histogram_label(low, high):
    """Format a histogram bucket label."""
    if high == float("inf"):
        return f"{low}+"
    return f"{low}-{high}"


def print_section(title: str, token_counts: list[int], total: int | None = None):
    """Print a stats section with histogram."""
    stats = compute_stats(token_counts)
    if not stats:
        print(f"\n{'=' * 60}")
        print(f"  {title}: (no data)")
        return

    n = stats["count"]
    if total is None:
        total = n

    print(f"\n{'=' * 60}")
    print(f"  {title} (n={n:,})")
    print(f"{'=' * 60}")

    print(f"\n  Token length distribution:")
    print(f"    Min:    {stats['min']:>6,}")
    print(f"    P5:     {stats['p5']:>6,}")
    print(f"    P25:    {stats['p25']:>6,}")
    print(f"    Median: {stats['median']:>6,.0f}")
    print(f"    Mean:   {stats['mean']:>6,.1f}")
    print(f"    P75:    {stats['p75']:>6,}")
    print(f"    P95:    {stats['p95']:>6,}")
    print(f"    Max:    {stats['max']:>6,}")

    exceed_512 = stats["exceeds_512"]
    exceed_256 = stats["exceeds_256"]
    print(f"\n  Exceeds {MODEL_MAX_TOKENS} tokens (model max): {exceed_512:>6,} ({100*exceed_512/n:.1f}%)")
    print(f"  Exceeds {TARGET_MAX_TOKENS} tokens (target):    {exceed_256:>6,} ({100*exceed_256/n:.1f}%)")

    print(f"\n  Histogram:")
    histogram = compute_histogram(token_counts)
    max_count = max(c for _, _, c in histogram)
    bar_width = 30
    for low, high, count in histogram:
        label = format_histogram_label(low, high)
        pct = 100 * count / n if n > 0 else 0
        bar_len = int(bar_width * count / max_count) if max_count > 0 else 0
        bar = "#" * bar_len
        print(f"    {label:>8s}: {count:>6,} ({pct:>5.1f}%) |{bar}")


def print_query_analysis(tokenizer, queries_path: Path):
    """Analyze and print token lengths for benchmark queries."""
    if not queries_path.exists():
        print(f"\n  Queries file not found: {queries_path}")
        return

    data = json.loads(queries_path.read_text())
    all_queries = []
    for group in data["groups"]:
        for q in group["queries"]:
            all_queries.append((group["name"], q))

    print(f"\n{'=' * 60}")
    print(f"  Benchmark Queries (n={len(all_queries)})")
    print(f"{'=' * 60}")

    token_counts = tokenize_texts(tokenizer, [q for _, q in all_queries])

    stats = compute_stats(token_counts)
    print(f"\n  Token length distribution:")
    print(f"    Min:    {stats['min']:>6,}")
    print(f"    Max:    {stats['max']:>6,}")
    print(f"    Mean:   {stats['mean']:>6,.1f}")
    print(f"    Median: {stats['median']:>6,.0f}")

    print(f"\n  Per-group breakdown:")
    groups = {}
    for (group_name, _), tc in zip(all_queries, token_counts):
        groups.setdefault(group_name, []).append(tc)

    for group_name, counts in groups.items():
        mean = statistics.mean(counts)
        print(f"    {group_name:>14s}: mean={mean:.1f}, range=[{min(counts)}, {max(counts)}]")

    print(f"\n  Individual queries:")
    for (group_name, query), tc in zip(all_queries, token_counts):
        print(f"    [{tc:>3}] ({group_name}) {query}")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze token length distribution in embeddings corpus"
    )
    parser.add_argument(
        "--embed-db",
        type=Path,
        default=None,
        help="Path to embeddings DB (default: ~/.local/share/siftd/embeddings.db)",
    )
    args = parser.parse_args()

    embed_db = args.embed_db or (data_dir() / "embeddings.db")
    if not embed_db.exists():
        print(f"Embeddings DB not found: {embed_db}")
        sys.exit(1)

    queries_path = Path(__file__).parent / "queries.json"

    print(f"Loading tokenizer (BAAI/bge-small-en-v1.5)...")
    tokenizer = get_tokenizer()

    print(f"Loading chunks from: {embed_db}")
    chunks = load_chunks(embed_db)
    if not chunks:
        print("No chunks found in DB.")
        sys.exit(1)

    # Tokenize all chunks
    all_texts = [c["text"] for c in chunks]
    print(f"Tokenizing {len(all_texts):,} chunks...")
    all_token_counts = tokenize_texts(tokenizer, all_texts)

    # Split by chunk_type
    prompt_counts = [tc for c, tc in zip(chunks, all_token_counts) if c["chunk_type"] == "prompt"]
    response_counts = [tc for c, tc in zip(chunks, all_token_counts) if c["chunk_type"] == "response"]

    # Print report
    print_section("ALL CHUNKS", all_token_counts)
    print_section("PROMPT CHUNKS", prompt_counts, total=len(all_token_counts))
    print_section("RESPONSE CHUNKS", response_counts, total=len(all_token_counts))

    # Query analysis
    print_query_analysis(tokenizer, queries_path)

    print()


if __name__ == "__main__":
    main()
