#!/usr/bin/env python3
"""Stage-1 corpus build: full-corpus scratch embed index over the DB snapshot.

Closed-universe requirement (bench plan 2026-07-05): every conversation FTS can
surface must also be in the vector index, so this indexes EVERY conversation the
chunker yields chunks for — no sampling, no since-bound hacks. Reads the snapshot
read-only; writes a fresh v2-schema embed DB in the run dir. Never touches
production ~/.local/share/siftd/embeddings.db.

BYOK amendment (2026-07-05): the bench gate is cross-backend robustness, so the
index builds once per backend arm — ``--backend local`` (fastembed/bge-small) or
any remote preset name (``gemini``, ``voyage``, ...). Each arm gets its own DB
(``embed-<backend>.db``). Remote arms read the key from $SIFTD_BENCH_EMBED_KEY.

Resume is the default: if the arm's DB exists, conversations already recorded in
indexed_state are skipped (commits are conversation-aligned, so a recorded conv
is fully stored). ``--fresh`` discards the DB and starts over.

Usage:
    UV_NO_SYNC=1 uv run --no-sync python bench/stage1/build_index.py [--backend local]
        [--limit N] [--fresh] [--dimensions D]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from siftd.embeddings.chunker import (  # noqa: E402
    extract_exchange_window_chunks,
    extract_tool_summary_chunks,
    extract_typed_exchange_chunks,
)
from siftd.storage.embeddings import (  # noqa: E402
    open_embeddings_db,
    set_meta,
    store_chunk,
    upsert_indexed_state,
)

RUN_DIR = Path(__file__).parent.parent / "runs" / "stage1-2026-07-05"
SNAPSHOT = RUN_DIR / "siftd-snapshot.db"
SCHEMA_VERSION = 2
GROUP_CHUNKS = 256  # embed+commit granularity (conversation-aligned)
# fastembed/onnxruntime keeps a CPU memory arena sized to the largest (batch x seq)
# tensor it ever sees and never shrinks it, so a 256-wide embed call ratchets RSS up
# unboundedly across a long build (observed 5.2G -> 9.3G -> OOM on an 11G VM). Capping
# the per-inference batch bounds that high-water directly. Remote backends batch
# internally by preset max_batch and rate-limit on request count, so the cap is
# fastembed-only (0 = uncapped, hand the whole group to the backend at once).
LOCAL_EMBED_BATCH = 16


def make_backend(name: str, dimensions: int | None):
    if name == "local":
        from siftd.embeddings.fastembed_backend import FastEmbedBackend

        return FastEmbedBackend()
    from siftd.embeddings.presets import get_preset
    from siftd.embeddings.remote import RemoteBackend

    preset = get_preset(name)
    if preset is None:
        sys.exit(f"unknown backend {name!r} (local or a preset in embed_presets.toml)")
    if not preset.default_model:
        sys.exit(f"preset {name!r} has no default model; bench arms use preset defaults")
    api_key = os.environ.get("SIFTD_BENCH_EMBED_KEY", "")
    if not api_key:
        sys.exit("remote backend needs SIFTD_BENCH_EMBED_KEY in the environment")
    return RemoteBackend(
        preset_name=preset.name,
        base_url=preset.base_url,
        model=preset.default_model,
        intent_style=preset.intent_style,
        max_batch=preset.max_batch,
        api_key=api_key,
        dimension=dimensions or preset.default_dimensions,
        dimensions_param=dimensions,
        dimensions_param_name=preset.dimensions_param,
    )


def flush_group(econn, backend, group: list[tuple[str, list[dict]]], counts: dict) -> None:
    """Embed + store a conversation-aligned group, then commit it atomically.

    indexed_state rows land in the same transaction as their chunks, so a conv
    recorded there is fully stored — the resume-skip invariant.
    """
    texts = [c["text"] for _, chunks in group for c in chunks]
    cap = LOCAL_EMBED_BATCH if backend.name == "fastembed" else 0
    if not texts:
        vecs: list[list[float]] = []
    elif cap and len(texts) > cap:
        vecs = []
        for i in range(0, len(texts), cap):
            vecs.extend(backend.embed_documents(texts[i : i + cap]))
    else:
        vecs = backend.embed_documents(texts)
    if len(vecs) != len(texts):
        raise RuntimeError(f"embed count mismatch: {len(vecs)} != {len(texts)}")
    it = iter(vecs)
    for cid, chunks in group:
        for c in chunks:
            store_chunk(
                econn,
                c["conversation_id"],
                c["chunk_type"],
                c["text"],
                next(it),
                token_count=c.get("token_count"),
                source_ids=c.get("source_ids"),
            )
        upsert_indexed_state(econn, cid, "bench-stage1", len(chunks))
        counts["convs"] += 1
        if chunks:
            counts["convs_indexed"] += 1
        counts["chunks"] += len(chunks)
    econn.commit()
    group.clear()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="local", help="'local' or a remote preset name")
    ap.add_argument(
        "--chunk-strategy",
        default="S0",
        choices=["S0", "S1"],
        help="S0 = blended exchange-window (incumbent); S1 = prompt/response split (stage 2)",
    )
    ap.add_argument("--limit", type=int, default=None, help="conversation cap (smoke runs)")
    ap.add_argument("--fresh", action="store_true", help="discard existing DB instead of resuming")
    ap.add_argument(
        "--dimensions", type=int, default=None, help="matryoshka truncation for remote presets that support it"
    )
    args = ap.parse_args()

    # S0 keeps the incumbent artifact names so existing stage-1 indexes resolve
    # unchanged; S1 carries a suffix so both chunkings sit side-by-side per arm.
    suffix = "" if args.chunk_strategy == "S0" else f"-{args.chunk_strategy}"
    embed_db = RUN_DIR / f"embed-{args.backend}{suffix}.db"
    progress = RUN_DIR / f"build-progress-{args.backend}{suffix}.json"

    conn = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    conv_ids = [r[0] for r in conn.execute("SELECT id FROM conversations ORDER BY started_at").fetchall()]
    if args.limit:
        conv_ids = conv_ids[: args.limit]
    print(f"corpus: {len(conv_ids)} conversations", file=sys.stderr)

    backend = make_backend(args.backend, args.dimensions)
    print(f"backend: {backend.name} {backend.model} dim={backend.dimension}", file=sys.stderr)

    extract_exchanges = (
        extract_exchange_window_chunks
        if args.chunk_strategy == "S0"
        else extract_typed_exchange_chunks
    )
    print(f"chunk strategy: {args.chunk_strategy}", file=sys.stderr)

    if args.fresh and embed_db.exists():
        embed_db.unlink()
    resuming = embed_db.exists()
    econn = open_embeddings_db(embed_db)

    done: set[str] = set()
    counts = {"convs": 0, "convs_indexed": 0, "chunks": 0}
    if resuming:
        done = {r[0] for r in econn.execute("SELECT conversation_id FROM indexed_state")}
        counts["convs"] = len(done)
        counts["convs_indexed"] = econn.execute("SELECT COUNT(*) FROM indexed_state WHERE chunk_count > 0").fetchone()[
            0
        ]
        counts["chunks"] = econn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        print(f"resuming: {len(done)} conversations already indexed", file=sys.stderr)
    else:
        # Identity meta FIRST (slice-2 keystone: zero-chunk builds stay self-describing).
        set_meta(econn, "backend", backend.name)
        set_meta(econn, "model", backend.model)
        set_meta(econn, "dimension", str(backend.dimension))
        set_meta(econn, "schema_version", str(SCHEMA_VERSION))
        econn.commit()

    group: list[tuple[str, list[dict]]] = []
    pending = 0
    t0 = time.time()
    started = counts["convs"]
    last_progress = started
    for cid in conv_ids:
        if cid in done:
            continue
        cchunks = extract_exchanges(conn, conversation_id=cid)
        cchunks.extend(extract_tool_summary_chunks(conn, conversation_ids={cid}))
        group.append((cid, cchunks))
        pending += len(cchunks)
        if pending >= GROUP_CHUNKS:
            flush_group(econn, backend, group, counts)
            pending = 0
        # Counter advances in flush-sized strides, so an exact %100 check can miss
        # every multiple forever — track the delta since the last write instead.
        if counts["convs"] - last_progress >= 100:
            last_progress = counts["convs"]
            elapsed = time.time() - t0
            rate = (counts["convs"] - started) / elapsed if elapsed > 0 else 0.0
            progress.write_text(
                json.dumps(
                    {
                        **counts,
                        "total_convs": len(conv_ids),
                        "elapsed_s": round(elapsed, 1),
                        "rate_convs_per_s": round(rate, 2),
                    }
                )
            )
    if group:
        flush_group(econn, backend, group, counts)
    econn.close()

    elapsed = time.time() - t0
    progress.write_text(
        json.dumps({**counts, "total_convs": len(conv_ids), "elapsed_s": round(elapsed, 1), "done": True})
    )
    print(
        f"done: {counts['convs_indexed']}/{counts['convs']} convs, "
        f"{counts['chunks']} chunks in {elapsed:.0f}s -> {embed_db}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
