#!/usr/bin/env python3
"""Slice-4 RRF gate: A/B narrow-then-rank vs RRF hybrid on the real corpus.

LOCAL experiment (not a test lane). Reads the main siftd.db READ-ONLY and builds an
A/B embeddings index with fastembed into a SCRATCH file — it never touches the user's
production ~/.local/share/siftd/embeddings.db. Compares the two hybrid strategies
through the LIVE engine (api.search.hybrid_search): the RRF default and the dormant
narrow-then-rank path (SIFTD_HYBRID_STRATEGY=narrow).

Usage:
    UV_NO_SYNC=1 uv run --no-sync python bench/ab_rrf.py [--sample N] [--embed-db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from siftd.api.search import hybrid_search  # noqa: E402
from siftd.embeddings.chunker import (  # noqa: E402
    extract_exchange_window_chunks,
    extract_tool_summary_chunks,
)
from siftd.embeddings.fastembed_backend import FastEmbedBackend  # noqa: E402
from siftd.storage.embeddings import (  # noqa: E402
    open_embeddings_db,
    set_meta,
    store_chunk,
    upsert_indexed_state,
)

MAIN_DB = Path.home() / ".local/share/siftd/siftd.db"
SCHEMA_VERSION = 2


def sample_conversation_ids(conn: sqlite3.Connection, n: int) -> list[str]:
    """A deterministic, workspace-spread sample of substantive conversations."""
    rows = conn.execute(
        """
        SELECT c.id
        FROM conversations c
        JOIN (SELECT conversation_id, COUNT(*) k FROM events WHERE kind='prompt'
              GROUP BY conversation_id HAVING k >= 2) e ON e.conversation_id = c.id
        ORDER BY c.started_at DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [r[0] for r in rows]


def build_index(main_conn, embed_db: Path, conv_ids: list[str], backend, max_chunks: int) -> set[str]:
    """Chunk + embed conversations into a fresh scratch embed DB until the chunk
    budget is hit. Returns the set of conversations that were actually indexed."""
    if embed_db.exists():
        embed_db.unlink()
    econn = open_embeddings_db(embed_db)
    set_meta(econn, "backend", backend.name)
    set_meta(econn, "model", backend.model)
    set_meta(econn, "dimension", str(backend.dimension))
    set_meta(econn, "schema_version", str(SCHEMA_VERSION))

    # Collect exchange chunks conversation-by-conversation until the budget is hit,
    # so the indexed set is a clean prefix of the (recent-ordered) sample.
    pending: list[dict] = []
    indexed: list[str] = []
    for i, cid in enumerate(conv_ids):
        cchunks = extract_exchange_window_chunks(main_conn, conversation_id=cid)
        if not cchunks:
            continue
        pending.extend(cchunks)
        indexed.append(cid)
        if len(pending) >= max_chunks:
            break
    indexed_set = set(indexed)
    pending.extend(extract_tool_summary_chunks(main_conn, conversation_ids=indexed_set))
    print(f"  {len(indexed)} conversations -> {len(pending)} chunks to embed", file=sys.stderr)

    total = 0
    B = 256
    for start in range(0, len(pending), B):
        batch = pending[start : start + B]
        vecs = backend.embed_documents([c["text"] for c in batch])
        for c, v in zip(batch, vecs):
            store_chunk(
                econn, c["conversation_id"], c["chunk_type"], c["text"], v,
                token_count=c.get("token_count"), source_ids=c.get("source_ids"),
            )
        total += len(batch)
        print(f"  embedded {total}/{len(pending)}", file=sys.stderr)
    for cid in indexed:
        upsert_indexed_state(econn, cid, "sample", 0)
    econn.commit()
    econn.close()
    return indexed_set


def top_cosine(chunks) -> float:
    """The top result's cosine (embedding_sim); 0.0 for a keyword-only entrant."""
    if not chunks:
        return 0.0
    bd = chunks[0].breakdown
    if bd is None or bd.vector_rank is None:
        return 0.0
    return bd.embedding_sim


def run_strategy(strategy: str, embed_db: Path, queries: list[str], since: str | None) -> dict:
    """Run all queries through the live engine under one hybrid strategy.

    ``since`` bounds both the vector candidate set and the keyword list to the sampled
    (recent) window so the two strategies compare over the same conversation universe —
    the index only holds the sample, so an unbounded keyword list would let old,
    unindexed conversations enter as entrants and skew the comparison."""
    # Set the knob explicitly BOTH ways. After the F3 flip the engine DEFAULT is
    # narrow, so popping the var (the original pre-flip idiom, back when unset => rrf)
    # now resolves to narrow too — a re-run would silently compare narrow vs narrow.
    # The engine opts into RRF only on ``=rrf``.
    os.environ["SIFTD_HYBRID_STRATEGY"] = "rrf" if strategy == "rrf" else "narrow"
    per_query = {}
    for q in queries:
        chunks = hybrid_search(
            q, db_path=MAIN_DB, embed_db=embed_db, mode="hybrid", n=10,
            since=since, exclude_active=False, include_derivative=True,
        )
        per_query[q] = chunks
    return per_query


def tool_ground_truth(conn, sample: set[str], max_queries: int = 6) -> list[tuple[str, set[str]]]:
    """Derive tool-usage recall queries from the sample (findability pattern).

    Ground truth = sample conversations whose event_tool_call input read/edited a file
    with a distinctive basename that appears in a small number of conversations.
    """
    ph = ",".join("?" * len(sample))
    rows = conn.execute(
        f"""
        SELECT etc.input, e.conversation_id
        FROM events e JOIN event_tool_call etc ON etc.event_id = e.id
        LEFT JOIN tools t ON t.id = etc.tool_id
        WHERE e.kind='tool_call' AND (t.category='file' OR t.name LIKE 'file.%')
          AND e.conversation_id IN ({ph})
        """,
        tuple(sample),
    ).fetchall()
    by_base: dict[str, set[str]] = {}
    for inp, cid in rows:
        try:
            path = (json.loads(inp) or {}).get("file_path") or ""
        except (ValueError, TypeError):
            path = ""
        if not path:
            continue
        base = os.path.basename(path)
        if not base or "." not in base:
            continue
        by_base.setdefault(base, set()).add(cid)

    # Distinctive: files touched in 2..12 conversations (specific, but recall-able).
    candidates = sorted(
        ((b, cids) for b, cids in by_base.items() if 2 <= len(cids) <= 12),
        key=lambda kv: -len(kv[1]),
    )
    out = []
    for base, cids in candidates:
        out.append((f"the conversation where {base} was edited", cids))
        if len(out) >= max_queries:
            break
    return out


def recall_at_10(chunks, ground: set[str]) -> float:
    got = {c.conversation_id for c in chunks[:10]}
    denom = min(10, len(ground)) or 1
    return len(got & ground) / denom


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=800)
    ap.add_argument("--max-chunks", type=int, default=12000)
    ap.add_argument("--embed-db", type=Path,
                    default=Path(tempfile.gettempdir()) / "ab_rrf_scratch.db")  # sibling ab_rrf_indexed.json follows
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    if not MAIN_DB.exists():
        print("main db not found", file=sys.stderr)
        raise SystemExit(1)

    conn = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print(f"Sampling up to {args.sample} recent conversations...", file=sys.stderr)
    conv_ids = sample_conversation_ids(conn, args.sample)
    print(f"  candidate pool: {len(conv_ids)}", file=sys.stderr)

    backend = FastEmbedBackend()
    print(f"backend: {backend.name} {backend.model} dim={backend.dimension}", file=sys.stderr)

    if not args.skip_build:
        t0 = time.time()
        indexed_set = build_index(conn, args.embed_db, conv_ids, backend, args.max_chunks)
        print(f"built index for {len(indexed_set)} conversations in {time.time() - t0:.1f}s -> {args.embed_db}", file=sys.stderr)
        (args.embed_db.parent / "ab_rrf_indexed.json").write_text(json.dumps(sorted(indexed_set)))
    else:
        indexed_set = set(json.loads((args.embed_db.parent / "ab_rrf_indexed.json").read_text()))

    sample = indexed_set
    since_bound = conn.execute(
        "SELECT MIN(started_at) FROM conversations WHERE id IN (%s)"
        % ",".join("?" * len(sample)),
        sorted(sample),
    ).fetchone()[0]
    print(f"  since bound: {since_bound}  (indexed convs: {len(sample)})", file=sys.stderr)

    qdata = json.loads((Path(__file__).parent / "queries.json").read_text())
    conceptual = []
    for g in qdata["groups"]:
        if g["name"] in ("conceptual", "philosophical", "technical", "specific"):
            conceptual.extend(g["queries"])

    identifier_queries = [
        "hybrid_search", "EmbeddingTransientError", "fts5_recall_conversations",
        "MAX_MMR_CANDIDATES", "reciprocal rank fusion",
    ]

    tool_queries = tool_ground_truth(conn, sample)
    conn.close()

    print("\nRunning A/B through the live engine...", file=sys.stderr)
    all_queries = conceptual + identifier_queries + [q for q, _ in tool_queries]
    res_narrow = run_strategy("narrow", args.embed_db, all_queries, since_bound)
    res_rrf = run_strategy("rrf", args.embed_db, all_queries, since_bound)

    def avg_top1(res):
        vals = [top_cosine(res[q]) for q in conceptual]
        return sum(vals) / len(vals) if vals else 0.0

    a_narrow = avg_top1(res_narrow)
    a_rrf = avg_top1(res_rrf)

    print("\n" + "=" * 72)
    print("AVG TOP-1 COSINE (conceptual/philosophical/technical/specific)")
    print("=" * 72)
    print(f"  narrow-then-rank : {a_narrow:.4f}")
    print(f"  RRF              : {a_rrf:.4f}")
    rel = (a_rrf - a_narrow) / a_narrow * 100 if a_narrow else 0.0
    print(f"  relative delta   : {rel:+.2f}%")

    print("\n" + "=" * 72)
    print("RECALL@10 on tool-usage ground truth (findability pattern)")
    print("=" * 72)
    rn = rr = 0.0
    for q, ground in tool_queries:
        r1 = recall_at_10(res_narrow[q], ground)
        r2 = recall_at_10(res_rrf[q], ground)
        rn += r1
        rr += r2
        print(f"  |gt|={len(ground):2d}  narrow={r1:.2f} rrf={r2:.2f}  {q}")
    if tool_queries:
        print(f"  MEAN recall@10: narrow={rn / len(tool_queries):.3f}  rrf={rr / len(tool_queries):.3f}")

    print("\n" + "=" * 72)
    print("EXACT-IDENTIFIER SPOT CHECK (keyword-surfacing win RRF exists for)")
    print("=" * 72)
    for q in identifier_queries:
        def hit_rank(chunks):
            for i, c in enumerate(chunks):
                if q.lower() in (c.text or "").lower():
                    return i + 1
            return None
        rn_ = hit_rank(res_narrow[q])
        rr_ = hit_rank(res_rrf[q])
        kw_rrf = sum(1 for c in res_rrf[q] if c.breakdown and c.breakdown.keyword_rank is not None)
        print(f"  narrow_hit@={str(rn_):>4}  rrf_hit@={str(rr_):>4}  rrf_kwhits={kw_rrf:2d}  '{q}'")


if __name__ == "__main__":
    main()
