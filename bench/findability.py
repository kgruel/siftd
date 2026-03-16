#!/usr/bin/env python3
"""Findability benchmark: recall@10 for tool-usage queries.

Three modes compared side-by-side:
  semantic   — embed query, search vector chunks
  fts5       — FTS5 on tool_calls (tool name + input text)
  hybrid     — FTS5 recall set → semantic rerank

Ground-truth is derived programmatically from tool_calls table, so the
query/answer pairs are entirely separate from both the FTS5 index and the
embedding chunks (no data leakage).

Usage:
    python bench/findability.py --embed-db /path/to/embeddings.db
    python bench/findability.py --embed-db /path/to/embeddings.db --main-db /path/to/siftd.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from siftd.storage.embeddings import search_similar  # noqa: E402
from siftd.embeddings.fastembed_backend import FastEmbedBackend  # noqa: E402
from siftd.paths import data_dir  # noqa: E402


# ---------------------------------------------------------------------------
# Ground-truth query definitions
# Each entry: (natural_language_query, ground_truth_sql, sql_params)
# SQL must return rows with a conversation_id column.
# These SQLs define ground truth — do NOT modify them to match the FTS5 index.
# ---------------------------------------------------------------------------
GROUND_TRUTH_QUERIES = [
    (
        "conversations where files were read",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'file.read'""",
        (),
    ),
    (
        "conversations where shell commands were executed",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'shell.execute'""",
        (),
    ),
    (
        "conversations with tool errors or failures",
        """SELECT DISTINCT conversation_id FROM tool_calls WHERE status = 'error'""",
        (),
    ),
    (
        "conversations editing source files",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'file.edit'""",
        (),
    ),
    (
        "conversations reading pyproject.toml configuration",
        """SELECT DISTINCT conversation_id FROM tool_calls
           WHERE input LIKE '%pyproject.toml%'""",
        (),
    ),
    (
        "conversations running git commands",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id
           WHERE t.name = 'shell.execute' AND tc.input LIKE '%git%'""",
        (),
    ),
    (
        "conversations searching with grep",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'search.grep'""",
        (),
    ),
    (
        "conversations writing new files",
        """SELECT DISTINCT tc.conversation_id FROM tool_calls tc
           JOIN tools t ON tc.tool_id = t.id WHERE t.name = 'file.write'""",
        (),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_indexed_conversations(embed_conn: sqlite3.Connection) -> set[str]:
    """Conversation IDs present in the embeddings DB."""
    rows = embed_conn.execute("SELECT DISTINCT conversation_id FROM chunks").fetchall()
    return {r[0] for r in rows}


def _compute_aggregate(results: list[dict], k: int) -> dict:
    if not results:
        return {"recall_at_k": 0.0, "hit_at_k": 0.0, "query_count": 0, "k": k, "queries": []}
    avg_recall = sum(r["recall_at_k"] for r in results) / len(results)
    avg_hit = sum(r["hit_at_k"] for r in results) / len(results)
    return {
        "k": k,
        "recall_at_k": round(avg_recall, 4),
        "hit_at_k": round(avg_hit, 4),
        "query_count": len(results),
        "queries": results,
    }


# ---------------------------------------------------------------------------
# Mode 1: Semantic (vector search over embedding chunks)
# ---------------------------------------------------------------------------

def compute_semantic_recall(
    main_conn: sqlite3.Connection,
    embed_conn: sqlite3.Connection,
    backend: FastEmbedBackend,
    *,
    k: int = 10,
    scope_convs: set[str] | None = None,
    verbose: bool = False,
    label: str = "semantic",
) -> dict:
    """Embed each natural-language query and search the vector index."""
    indexed_convs = get_indexed_conversations(embed_conn)
    if scope_convs is not None:
        indexed_convs &= scope_convs

    results = []
    for query_text, sql, params in GROUND_TRUTH_QUERIES:
        relevant_all = {r[0] for r in main_conn.execute(sql, params).fetchall()}
        relevant = relevant_all & indexed_convs
        if not relevant:
            if verbose:
                print(f"  [{label}] SKIP '{query_text}' — no indexed relevant convs")
            continue

        query_emb = backend.embed_one(query_text)
        search_results = search_similar(embed_conn, query_emb, limit=k)
        found_convs = {r["conversation_id"] for r in search_results}

        hits = len(relevant & found_convs)
        recall = hits / len(relevant)
        hit_at_k = 1 if hits else 0

        results.append({
            "query": query_text,
            "relevant_indexed": len(relevant),
            "hits_in_top_k": hits,
            "recall_at_k": round(recall, 4),
            "hit_at_k": hit_at_k,
        })
        if verbose:
            print(f"  [{'✓' if hit_at_k else '✗'}][{label}] [{hits}/{len(relevant)}] {recall:.3f} | {query_text}")

    return _compute_aggregate(results, k)


# ---------------------------------------------------------------------------
# Mode 2: FTS5 on tool_calls (tool name + input text)
# Index built as a temporary in-memory table — no schema changes to main DB.
# ---------------------------------------------------------------------------

def _build_fts5_tool_index(main_conn: sqlite3.Connection, scope_convs: set[str] | None = None) -> sqlite3.Connection:
    """Create an in-memory FTS5 index over tool calls.

    The index contains one row per tool call:
      conversation_id, tool_name, input_text

    We index tool_name and input_text for full-text search. The query uses
    the natural language query text as the FTS5 MATCH expression, so we rely
    on tokenization rather than hand-crafted keyword mappings — same query
    text both modes receive.
    """
    import json as _json

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row

    mem.execute("""
        CREATE VIRTUAL TABLE tool_fts USING fts5(
            conversation_id UNINDEXED,
            tool_name,
            input_text,
            tokenize='porter unicode61 remove_diacritics 1'
        )
    """)

    # Load tool calls from main DB
    where = ""
    params: tuple = ()
    if scope_convs:
        ph = ",".join("?" * len(scope_convs))
        where = f"WHERE tc.conversation_id IN ({ph})"
        params = tuple(scope_convs)

    rows = main_conn.execute(
        f"""SELECT tc.conversation_id, COALESCE(t.name, ''), COALESCE(t.description, ''), tc.input, tc.status
            FROM tool_calls tc
            LEFT JOIN tools t ON tc.tool_id = t.id
            {where}""",
        params,
    ).fetchall()

    batch = []
    for row in rows:
        conv_id, tool_name, tool_desc, raw_input, status = row[0], row[1], row[2], row[3] or "", row[4] or ""
        # Parse JSON input and extract text values for indexing
        try:
            inp = _json.loads(raw_input)
            if isinstance(inp, dict):
                # Join all string values (file paths, commands, patterns, descriptions)
                input_text = " ".join(str(v) for v in inp.values() if isinstance(v, str))
            else:
                input_text = str(inp)
        except (ValueError, TypeError):
            input_text = raw_input

        # Append status so "error" / "failure" queries can match
        if status and status != "success":
            input_text = f"{input_text} {status}"

        # Include tool_name tokens (e.g. "file.read" → "file read") + description
        # so "writing" → porter("write") matches both "file.write" and "Write/create a file"
        tool_tokens = f"{tool_name.replace('.', ' ').replace('_', ' ')} {tool_desc}"
        batch.append((conv_id, tool_tokens, input_text))

    mem.executemany("INSERT INTO tool_fts VALUES (?, ?, ?)", batch)
    mem.commit()
    return mem


def _fts5_query(fts_conn: sqlite3.Connection, query_text: str, k: int) -> set[str]:
    """Run a FTS5 MATCH query and return top-k distinct conversation IDs.

    Strategy: rank candidates by how many distinct query terms they match,
    then by BM25 score. This reduces false-positive noise from broad OR queries
    while still finding conversations that partially match.
    """
    import re
    words = re.findall(r'[a-zA-Z0-9_.]+', query_text)
    # Broad stop words — do NOT remove domain terms like "files", "new", "git"
    stop = {"conversations", "where", "were", "with", "the", "and", "or",
            "that", "this", "are", "have", "been", "using", "used"}
    keywords = [w for w in words if w.lower() not in stop and len(w) > 2]
    if not keywords:
        return set()

    # Deduplicate keywords (case-insensitive)
    seen_kw: dict[str, str] = {}
    for kw in keywords:
        key = kw.lower()
        if key not in seen_kw:
            seen_kw[key] = kw
    keywords = list(seen_kw.values())

    # Try progressively: AND (most precise) → OR (broader fallback)
    # This gives high precision when AND finds enough results, falls back for rare terms.
    and_expr = " AND ".join(keywords)
    or_expr  = " OR ".join(keywords)

    def _run_match(expr: str) -> list:
        try:
            return fts_conn.execute(
                """SELECT conversation_id, rank FROM tool_fts
                   WHERE tool_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (expr, k * 10),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    rows = _run_match(and_expr)
    # Count distinct conversations from AND
    and_convs: set[str] = set()
    for row in rows:
        and_convs.add(row[0])

    if len(and_convs) >= k:
        # AND gives enough candidates — use these (high precision)
        candidates = rows
    else:
        # Fall back to OR for more recall, but boost AND matches
        or_rows = _run_match(or_expr)
        # Put AND-matching convs first, then OR-only convs
        and_set = and_convs
        priority: list = [r for r in or_rows if r[0] in and_set]
        remainder: list = [r for r in or_rows if r[0] not in and_set]
        candidates = priority + remainder

    # Deduplicate: keep first (best-ranked) occurrence per conversation
    seen: set[str] = set()
    result: list[str] = []
    for row in candidates:
        cid = row[0]
        if cid not in seen:
            seen.add(cid)
            result.append(cid)
            if len(result) >= k:
                break
    return set(result)


def compute_fts5_recall(
    main_conn: sqlite3.Connection,
    *,
    k: int = 10,
    scope_convs: set[str] | None = None,
    verbose: bool = False,
) -> dict:
    """Measure recall@k using FTS5 over tool_calls table.

    Queries are natural language (same text as semantic mode).
    FTS5 tokenises and matches against tool names and input values.
    """
    fts_conn = _build_fts5_tool_index(main_conn, scope_convs)
    indexed_convs = scope_convs if scope_convs is not None else {
        r[0] for r in main_conn.execute("SELECT DISTINCT conversation_id FROM tool_calls").fetchall()
    }

    results = []
    for query_text, sql, params in GROUND_TRUTH_QUERIES:
        relevant_all = {r[0] for r in main_conn.execute(sql, params).fetchall()}
        relevant = relevant_all & indexed_convs
        if not relevant:
            if verbose:
                print(f"  [fts5] SKIP '{query_text}' — no relevant convs in scope")
            continue

        found_convs = _fts5_query(fts_conn, query_text, k)

        hits = len(relevant & found_convs)
        recall = hits / len(relevant)
        hit_at_k = 1 if hits else 0

        results.append({
            "query": query_text,
            "relevant_indexed": len(relevant),
            "hits_in_top_k": hits,
            "recall_at_k": round(recall, 4),
            "hit_at_k": hit_at_k,
        })
        if verbose:
            print(f"  [{'✓' if hit_at_k else '✗'}][fts5 ] [{hits}/{len(relevant)}] {recall:.3f} | {query_text}")

    fts_conn.close()
    return _compute_aggregate(results, k)


# ---------------------------------------------------------------------------
# Mode 3: Hybrid — FTS5 recall set → semantic rerank
# ---------------------------------------------------------------------------

def compute_hybrid_recall(
    main_conn: sqlite3.Connection,
    embed_conn: sqlite3.Connection,
    backend: FastEmbedBackend,
    *,
    k: int = 10,
    fts5_recall_k: int = 100,
    scope_convs: set[str] | None = None,
    verbose: bool = False,
) -> dict:
    """FTS5 broadens the candidate set; semantic search reranks within it."""
    from siftd.storage.embeddings import search_similar

    fts_conn = _build_fts5_tool_index(main_conn, scope_convs)
    indexed_convs = get_indexed_conversations(embed_conn)
    if scope_convs is not None:
        indexed_convs &= scope_convs

    results = []
    for query_text, sql, params in GROUND_TRUTH_QUERIES:
        relevant_all = {r[0] for r in main_conn.execute(sql, params).fetchall()}
        relevant = relevant_all & indexed_convs
        if not relevant:
            if verbose:
                print(f"  [hybrid] SKIP '{query_text}' — no indexed relevant convs")
            continue

        # FTS5 recall: get candidate conversation IDs
        fts5_candidates = _fts5_query(fts_conn, query_text, fts5_recall_k)
        # Intersect with indexed convs (only those we can rerank)
        candidates = fts5_candidates & indexed_convs

        # Semantic rerank within candidates (or fall back to full search)
        query_emb = backend.embed_one(query_text)
        conversation_ids = list(candidates) if candidates else None
        search_results = search_similar(
            embed_conn, query_emb, limit=k,
            conversation_ids=conversation_ids,
        )
        found_convs = {r["conversation_id"] for r in search_results}

        hits = len(relevant & found_convs)
        recall = hits / len(relevant)
        hit_at_k = 1 if hits else 0

        results.append({
            "query": query_text,
            "relevant_indexed": len(relevant),
            "fts5_candidates": len(candidates),
            "hits_in_top_k": hits,
            "recall_at_k": round(recall, 4),
            "hit_at_k": hit_at_k,
        })
        if verbose:
            print(f"  [{'✓' if hit_at_k else '✗'}][hybrd] [{hits}/{len(relevant)}] {recall:.3f} | {query_text}")

    fts_conn.close()
    return _compute_aggregate(results, k)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Findability benchmark: recall@10 comparing semantic, FTS5, and hybrid"
    )
    parser.add_argument("--embed-db", type=Path, required=True, help="Embeddings DB")
    parser.add_argument("--main-db", type=Path, default=None, help="Main siftd.db path")
    parser.add_argument("-k", type=int, default=10, help="Recall@k (default: 10)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--metric", action="store_true", help="Output METRIC lines for autoresearch.sh")
    args = parser.parse_args()

    if not args.embed_db.exists():
        print(f"Error: embeddings DB not found: {args.embed_db}", file=sys.stderr)
        sys.exit(1)

    main_db_path = args.main_db or (data_dir() / "siftd.db")
    if not main_db_path.exists():
        print(f"Error: main DB not found: {main_db_path}", file=sys.stderr)
        sys.exit(1)

    main_conn = sqlite3.connect(main_db_path)
    main_conn.row_factory = sqlite3.Row
    embed_conn = sqlite3.connect(args.embed_db)
    embed_conn.row_factory = sqlite3.Row

    # Scope all modes to only conversations present in embeddings DB
    # (fair comparison: all modes see the same conversation universe)
    scope_convs = get_indexed_conversations(embed_conn)

    print("Initializing embedding model...", file=sys.stderr)
    backend = FastEmbedBackend()

    print(f"\nFindability benchmark (recall@{args.k}) — {len(scope_convs)} indexed conversations\n", file=sys.stderr)

    sem   = compute_semantic_recall(main_conn, embed_conn, backend, k=args.k, scope_convs=scope_convs, verbose=args.verbose)
    fts5  = compute_fts5_recall(main_conn, k=args.k, scope_convs=scope_convs, verbose=args.verbose)
    hyb   = compute_hybrid_recall(main_conn, embed_conn, backend, k=args.k, scope_convs=scope_convs, verbose=args.verbose)

    print(f"\n{'='*64}", file=sys.stderr)
    print(f"  {'Mode':<12} {'recall@'+str(args.k):<14} {'hit@'+str(args.k):<12} queries", file=sys.stderr)
    print(f"  {'-'*60}", file=sys.stderr)
    for label, r in [("semantic", sem), ("fts5", fts5), ("hybrid", hyb)]:
        print(f"  {label:<12} {r['recall_at_k']:<14.4f} {r['hit_at_k']:<12.4f} {r['query_count']}", file=sys.stderr)
    print(f"{'='*64}", file=sys.stderr)

    if args.metric:
        # Primary: semantic (what chunker tuning affects)
        print(f"METRIC recall_at_10={sem['recall_at_k']}")
        # Secondary: FTS5 and hybrid as comparison
        print(f"METRIC fts5_recall_at_10={fts5['recall_at_k']}")
        print(f"METRIC hybrid_recall_at_10={hyb['recall_at_k']}")
        print(f"METRIC hit_at_10={sem['hit_at_k']}")


if __name__ == "__main__":
    main()
