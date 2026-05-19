#!/usr/bin/env python3
"""Build probe databases for the smoke-homelab harness.

Usage:
    _smoke_homelab_fixture.py <output-dir>

Creates three SQLite databases under <output-dir>:
  small.db    — 1 conversation (P2 baseline push test)
  fixture.db  — 20 conversations with anchor phrases for P3-P8 (~2-5 MB)
  large.db    — 3 conversations with ~4 MB content blobs each (~15 MB, P1 size test)

Exits 0 on success, 1 on error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Inline storage helpers (avoid importing siftd.storage directly so this
# script can be run from the uv venv without the full package in sys.path
# for the fixture-build step, but siftd IS installed so it's fine to import).
# ---------------------------------------------------------------------------

try:
    from siftd.api.search import rebuild_fts_index
    from siftd.storage.sqlite import (
        create_database,
        get_or_create_harness,
        get_or_create_model,
        get_or_create_provider,
        get_or_create_workspace,
        insert_conversation,
        insert_prompt,
        insert_prompt_content,
        insert_response,
        insert_response_content,
    )
except ImportError as e:
    print(f"ERROR: siftd not importable: {e}", file=sys.stderr)
    sys.exit(1)


_LABELS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
_WORKSPACES = ["/work/proj-a", "/work/proj-b", "/personal"]

# These phrases land at exact turn positions so probes can assert them.
# Key: (conv_index, turn_index) → phrase inserted into prompt text
#
# Anchor-alpha is placed in the LATEST conversation (highest ci) because
# `siftd query -n 5` returns conversations sorted by started_at DESC. The
# harness resolves FIRST_ID from that listing and uses it for P5; the
# anchor must live in FIRST_ID's conversation for the probe to find it.
_ANCHOR_PHRASES: dict[tuple[int, int], str] = {
    (19, 3): "smoke-test-anchor-alpha",  # latest conv, turn 3 → P5
    (15, 5): "smoke-test-anchor-bravo",  # 5th-most-recent, turn 5 → P7
}


def _build_fixture(path: Path, *, n_convs: int = 20, n_turns: int = 6) -> None:
    """Build the anchor-phrase fixture DB."""
    conn = create_database(path)
    h = get_or_create_harness(conn, "smoke-harness", source="smoke", log_format="jsonl")
    models_providers = [
        (get_or_create_model(conn, "claude-3-5-sonnet"), get_or_create_provider(conn, "anthropic")),
        (get_or_create_model(conn, "gpt-4o"), get_or_create_provider(conn, "openai")),
    ]

    for ci in range(n_convs):
        ws = _WORKSPACES[ci % len(_WORKSPACES)]
        w = get_or_create_workspace(conn, ws, "2024-01-01T00:00:00Z")
        m, p = models_providers[ci % len(models_providers)]
        cid = insert_conversation(
            conn,
            external_id=f"c{ci:03d}",
            harness_id=h,
            workspace_id=w,
            started_at=f"2024-01-{(ci % 28) + 1:02d}T10:00:00Z",
        )
        for ti in range(n_turns):
            ts = f"2024-01-{(ci % 28) + 1:02d}T10:{ti:02d}:00Z"
            label = _LABELS[ti % 6]
            anchor = _ANCHOR_PHRASES.get((ci, ti), "")
            extra = f" {anchor}" if anchor else ""
            pid = insert_prompt(conn, cid, f"p-{ci}-{ti}", ts)
            insert_prompt_content(
                conn, pid, 0, "text",
                json.dumps({"text": f"turn-{ti}-unique-marker-{label}{extra}"}),
            )
            rid = insert_response(
                conn, cid, pid, m, p, f"r-{ci}-{ti}", ts,
                input_tokens=20 + ci, output_tokens=10 + ti,
            )
            insert_response_content(
                conn, rid, 0, "text",
                json.dumps({"text": f"response conv={ci} turn={ti} workspace={ws}"}),
            )

    rebuild_fts_index(conn)
    conn.commit()
    conn.close()


def _build_small(path: Path) -> None:
    """Build a minimal 1-conversation DB for the baseline push probe (P2)."""
    conn = create_database(path)
    h = get_or_create_harness(conn, "smoke-small", source="smoke", log_format="jsonl")
    w = get_or_create_workspace(conn, "/work/small-probe", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-3-5-sonnet")
    p = get_or_create_provider(conn, "anthropic")
    cid = insert_conversation(
        conn, external_id="c-small-baseline",
        harness_id=h, workspace_id=w,
        started_at="2024-01-01T12:00:00Z",
    )
    pid = insert_prompt(conn, cid, "p-small-0", "2024-01-01T12:00:00Z")
    insert_prompt_content(
        conn, pid, 0, "text",
        json.dumps({"text": "small-baseline-probe-conversation"}),
    )
    rid = insert_response(
        conn, cid, pid, m, p, "r-small-0", "2024-01-01T12:00:00Z",
        input_tokens=5, output_tokens=3,
    )
    insert_response_content(conn, rid, 0, "text", json.dumps({"text": "ack"}))
    rebuild_fts_index(conn)
    conn.commit()
    conn.close()


def _build_large(path: Path, *, n_convs: int = 4, blob_mb: int = 3) -> None:
    """Build a DB with large content blobs to push the total over Litestar's 10 MB cap.

    n_convs * blob_mb must exceed ~12 MB to reliably trigger bug #7.
    Default: 4 conversations × 3 MB ≈ 12 MB of content → DB on disk ~15+ MB
    including FTS index and schema overhead.
    """
    conn = create_database(path)
    h = get_or_create_harness(conn, "smoke-large", source="smoke", log_format="jsonl")
    w = get_or_create_workspace(conn, "/work/large-probe", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "gpt-4o")
    p = get_or_create_provider(conn, "openai")

    blob_text = "X" * (blob_mb * 1024 * 1024)

    for ci in range(n_convs):
        cid = insert_conversation(
            conn, external_id=f"c-large-{ci:02d}",
            harness_id=h, workspace_id=w,
            started_at=f"2024-02-{ci + 1:02d}T10:00:00Z",
        )
        pid = insert_prompt(conn, cid, f"p-large-{ci}", f"2024-02-{ci + 1:02d}T10:00:00Z")
        insert_prompt_content(
            conn, pid, 0, "text",
            json.dumps({"text": f"large-content-blob-{ci} {blob_text}"}),
        )
        rid = insert_response(
            conn, cid, pid, m, p, f"r-large-{ci}", f"2024-02-{ci + 1:02d}T10:00:05Z",
            input_tokens=1000, output_tokens=500,
        )
        insert_response_content(
            conn, rid, 0, "text",
            json.dumps({"text": f"large-response-{ci}"}),
        )

    rebuild_fts_index(conn)
    conn.commit()
    conn.close()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output-dir>", file=sys.stderr)
        return 1

    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    small_path = out_dir / "small.db"
    fixture_path = out_dir / "fixture.db"
    large_path = out_dir / "large.db"

    print("Building small.db (1 conversation)...", flush=True)
    _build_small(small_path)
    print(f"  small.db: {small_path.stat().st_size / 1024:.0f} KB")

    print("Building fixture.db (20 conversations, anchor phrases)...", flush=True)
    _build_fixture(fixture_path)
    print(f"  fixture.db: {fixture_path.stat().st_size / 1024:.0f} KB")

    print("Building large.db (large blobs, P1 size test)...", flush=True)
    _build_large(large_path)
    large_mb = large_path.stat().st_size / (1024 * 1024)
    print(f"  large.db: {large_mb:.1f} MB")
    if large_mb < 10:
        print(f"  WARNING: large.db is {large_mb:.1f} MB, may not trigger 10 MB cap", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
