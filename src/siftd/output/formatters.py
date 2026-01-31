"""Output formatters for semantic search results.

Each formatter implements a specific output mode for cmd_ask results.
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean as _mean
from typing import Protocol

from siftd.storage.queries import fetch_prompt_response_texts


@dataclass
class FormatterContext:
    """Everything a formatter needs to render output."""

    query: str
    results: list[dict]  # Raw dicts from embeddings search, enriched with file_refs
    conn: sqlite3.Connection  # For metadata lookups (read-only)
    args: argparse.Namespace  # For mode-specific options


class OutputFormatter(Protocol):
    """Protocol for search result formatters."""

    def format(self, ctx: FormatterContext) -> None:
        """Render results to stdout."""
        ...


def _get_conversation_metadata(
    conn: sqlite3.Connection, conv_ids: list[str]
) -> dict[str, dict]:
    """Fetch conversation metadata (started_at, workspace) for given IDs."""
    if not conv_ids:
        return {}
    placeholders = ",".join("?" * len(conv_ids))
    meta_rows = conn.execute(
        f"""
        SELECT c.id, c.started_at, w.path AS workspace
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE c.id IN ({placeholders})
    """,
        conv_ids,
    ).fetchall()
    return {row["id"]: dict(row) for row in meta_rows}


def _format_workspace(path: str | None) -> str:
    """Format workspace path to just the directory name."""
    if not path:
        return ""
    return Path(path).name


def format_refs_annotation(refs: list, *, max_shown: int = 5) -> str:
    """Compact one-liner: 'refs: file(r) file(w) +N more'."""
    if not refs:
        return ""

    # Deduplicate: same basename+op shown once
    seen = set()
    unique = []
    for ref in refs:
        key = (ref.basename, ref.op)
        if key not in seen:
            seen.add(key)
            unique.append(ref)

    shown = unique[:max_shown]
    parts = [f"{r.basename}({r.op})" for r in shown]
    overflow = len(unique) - max_shown
    if overflow > 0:
        parts.append(f"+{overflow} more")

    return "refs: " + " ".join(parts)


def print_refs_content(
    all_refs: list, filter_basenames: list[str] | None = None
) -> None:
    """Print file reference content dump section."""
    if not all_refs:
        return

    # Deduplicate by path+op (keep first occurrence for point-in-time snapshot)
    seen = set()
    unique = []
    for ref in all_refs:
        key = (ref.path, ref.op)
        if key not in seen:
            seen.add(key)
            unique.append(ref)

    # Apply basename filter if provided
    if filter_basenames:
        filter_set = {b.lower() for b in filter_basenames}
        unique = [r for r in unique if r.basename.lower() in filter_set]
        if not unique:
            names = ", ".join(filter_basenames)
            print(f"No file references matching: {names}")
            return

    op_labels = {"r": "read", "w": "write", "e": "edit"}

    print(f"\n{'─── File References ─' * 1}{'─' * 30}")
    print()

    for i, ref in enumerate(unique, 1):
        op_label = op_labels.get(ref.op, ref.op)
        print(f"[{i}] {ref.basename} ({op_label})")
        print(f"    {ref.path}")
        print("────")
        if ref.content:
            print(ref.content)
        else:
            print("(no content available)")
        print("────")
        print()


class ChunkListFormatter:
    """Default formatter: score-sorted chunks with truncated snippets."""

    def format(self, ctx: FormatterContext) -> None:
        meta = _get_conversation_metadata(
            ctx.conn, list({r["conversation_id"] for r in ctx.results})
        )

        results = ctx.results
        chrono = getattr(ctx.args, "chrono", False)
        if chrono:
            results = sorted(
                results,
                key=lambda r: (
                    meta.get(r["conversation_id"], {}).get("started_at") or "",
                    r["chunk_id"],
                ),
            )

        print(f"Results for: {ctx.query}\n")
        for r in results:
            conv_id = r["conversation_id"]
            m = meta.get(conv_id, {})
            short_id = conv_id[:12]
            workspace = _format_workspace(m.get("workspace"))
            started = (m.get("started_at") or "")[:10]
            side = r["chunk_type"].upper()
            score = r["score"]

            print(f"  {short_id}  {score:.3f}  [{side:8s}]  {started}  {workspace}")

            # Default: truncated snippet
            snippet = r["text"][:200].replace("\n", " ")
            if len(r["text"]) > 200:
                snippet += "..."
            print(f"    {snippet}")

            # File refs annotation
            file_refs = r.get("file_refs")
            if file_refs:
                annotation = format_refs_annotation(file_refs)
                print(f"    {annotation}")

            print()


class VerboseFormatter:
    """Verbose formatter: full chunk text, no truncation."""

    def format(self, ctx: FormatterContext) -> None:
        meta = _get_conversation_metadata(
            ctx.conn, list({r["conversation_id"] for r in ctx.results})
        )

        results = ctx.results
        chrono = getattr(ctx.args, "chrono", False)
        if chrono:
            results = sorted(
                results,
                key=lambda r: (
                    meta.get(r["conversation_id"], {}).get("started_at") or "",
                    r["chunk_id"],
                ),
            )

        print(f"Results for: {ctx.query}\n")
        for r in results:
            conv_id = r["conversation_id"]
            m = meta.get(conv_id, {})
            short_id = conv_id[:12]
            workspace = _format_workspace(m.get("workspace"))
            started = (m.get("started_at") or "")[:10]
            side = r["chunk_type"].upper()
            score = r["score"]

            print(f"  {short_id}  {score:.3f}  [{side:8s}]  {started}  {workspace}")

            # Full chunk text
            for line in r["text"].splitlines():
                print(f"    {line}")

            # File refs annotation
            file_refs = r.get("file_refs")
            if file_refs:
                annotation = format_refs_annotation(file_refs)
                print(f"    {annotation}")

            print()


class FullExchangeFormatter:
    """Show complete prompt+response exchange from main DB."""

    def format(self, ctx: FormatterContext) -> None:
        meta = _get_conversation_metadata(
            ctx.conn, list({r["conversation_id"] for r in ctx.results})
        )

        results = ctx.results
        chrono = getattr(ctx.args, "chrono", False)
        if chrono:
            results = sorted(
                results,
                key=lambda r: (
                    meta.get(r["conversation_id"], {}).get("started_at") or "",
                    r["chunk_id"],
                ),
            )

        print(f"Results for: {ctx.query}\n")
        for r in results:
            conv_id = r["conversation_id"]
            m = meta.get(conv_id, {})
            short_id = conv_id[:12]
            workspace = _format_workspace(m.get("workspace"))
            started = (m.get("started_at") or "")[:10]
            side = r["chunk_type"].upper()
            score = r["score"]

            print(f"  {short_id}  {score:.3f}  [{side:8s}]  {started}  {workspace}")

            self._print_exchange(ctx.conn, r)

            # File refs annotation
            file_refs = r.get("file_refs")
            if file_refs:
                annotation = format_refs_annotation(file_refs)
                print(f"    {annotation}")

            print()

    def _print_exchange(self, conn: sqlite3.Connection, result: dict) -> None:
        """Print complete prompt+response text for the source exchanges."""
        source_ids = result.get("source_ids", [])
        if not source_ids:
            # Fallback: show chunk text
            for line in result["text"].splitlines():
                print(f"    {line}")
            return

        exchanges = fetch_prompt_response_texts(conn, source_ids)

        for _pid, prompt_text, response_text in exchanges:
            if prompt_text:
                print(f"    > {prompt_text.splitlines()[0]}")
                for line in prompt_text.splitlines()[1:]:
                    print(f"    > {line}")
            if response_text:
                for line in response_text.splitlines():
                    print(f"    {line}")
            if prompt_text or response_text:
                print("    ---")


class ContextFormatter:
    """Show ±N exchanges around the match."""

    def __init__(self, n: int):
        self.n = n

    def format(self, ctx: FormatterContext) -> None:
        meta = _get_conversation_metadata(
            ctx.conn, list({r["conversation_id"] for r in ctx.results})
        )

        results = ctx.results
        chrono = getattr(ctx.args, "chrono", False)
        if chrono:
            results = sorted(
                results,
                key=lambda r: (
                    meta.get(r["conversation_id"], {}).get("started_at") or "",
                    r["chunk_id"],
                ),
            )

        print(f"Results for: {ctx.query}\n")
        for r in results:
            conv_id = r["conversation_id"]
            m = meta.get(conv_id, {})
            short_id = conv_id[:12]
            workspace = _format_workspace(m.get("workspace"))
            started = (m.get("started_at") or "")[:10]
            side = r["chunk_type"].upper()
            score = r["score"]

            print(f"  {short_id}  {score:.3f}  [{side:8s}]  {started}  {workspace}")

            self._print_context(ctx.conn, r)

            # File refs annotation
            file_refs = r.get("file_refs")
            if file_refs:
                annotation = format_refs_annotation(file_refs)
                print(f"    {annotation}")

            print()

    def _print_context(self, conn: sqlite3.Connection, result: dict) -> None:
        """Print ±N exchanges around the matched source exchanges."""
        source_ids = result.get("source_ids", [])
        conv_id = result["conversation_id"]

        if not source_ids:
            for line in result["text"].splitlines():
                print(f"    {line}")
            return

        # Get all prompts in this conversation, ordered by timestamp
        all_prompts = conn.execute(
            """
            SELECT p.id, p.timestamp
            FROM prompts p
            WHERE p.conversation_id = ?
            ORDER BY p.timestamp
        """,
            (conv_id,),
        ).fetchall()

        prompt_order = [row[0] for row in all_prompts]

        # Find the index range of source prompts
        source_set = set(source_ids)
        source_indices = [i for i, pid in enumerate(prompt_order) if pid in source_set]
        if not source_indices:
            for line in result["text"].splitlines():
                print(f"    {line}")
            return

        start = max(0, min(source_indices) - self.n)
        end = min(len(prompt_order), max(source_indices) + self.n + 1)
        context_ids = prompt_order[start:end]

        exchanges = fetch_prompt_response_texts(conn, context_ids)

        for pid, prompt_text, response_text in exchanges:
            marker = ">>>" if pid in source_set else "   "
            if prompt_text:
                print(f"    {marker} > {prompt_text.splitlines()[0]}")
                for line in prompt_text.splitlines()[1:]:
                    print(f"    {marker} > {line}")
            if response_text:
                for line in response_text.splitlines():
                    print(f"    {marker} {line}")
            print(f"    {marker} ---")


class ThreadFormatter:
    """Two-tier narrative thread: top conversations expanded, rest as shortlist."""

    def format(self, ctx: FormatterContext) -> None:
        # Aggregate chunk scores per conversation (max score per conversation)
        conv_scores: dict[str, float] = {}
        conv_chunks: dict[str, list[dict]] = defaultdict(list)
        for r in ctx.results:
            cid = r["conversation_id"]
            conv_chunks[cid].append(r)
            if cid not in conv_scores or r["score"] > conv_scores[cid]:
                conv_scores[cid] = r["score"]

        meta = _get_conversation_metadata(ctx.conn, list(conv_scores.keys()))

        # Partition: tier 1 = conversations with max_score > mean of all max_scores
        scores = list(conv_scores.values())
        mean_score = sum(scores) / len(scores) if scores else 0.0
        tier1_ids = [cid for cid, s in conv_scores.items() if s > mean_score]
        tier2_ids = [cid for cid in conv_scores if cid not in set(tier1_ids)]

        # Sort tier 1 chronologically
        tier1_ids.sort(key=lambda cid: meta.get(cid, {}).get("started_at") or "")
        # Sort tier 2 by score descending
        tier2_ids.sort(key=lambda cid: conv_scores[cid], reverse=True)

        print(f"Results for: {ctx.query}\n")

        # --- Tier 1: Narrative thread ---
        for cid in tier1_ids:
            m = meta.get(cid, {})
            workspace = _format_workspace(m.get("workspace"))
            started = (m.get("started_at") or "")[:10]

            print(
                f"\u2500\u2500\u2500 {workspace}  {started} \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            )

            # Best-matching chunk for this conversation
            best = max(conv_chunks[cid], key=lambda c: c["score"])
            source_ids = best.get("source_ids", [])

            if source_ids:
                self._print_exchange(ctx.conn, source_ids)
            else:
                # Fallback: show chunk text with type label
                side = "[user]" if best["chunk_type"] == "prompt" else "[asst]"
                text = best["text"].strip()
                if len(text) > 600:
                    text = text[:600] + "..."
                print(f"  {side} {text}")

            # File refs annotation
            file_refs = best.get("file_refs")
            if file_refs:
                annotation = format_refs_annotation(file_refs)
                print(f"  {annotation}")

            print()

        # --- Tier 2: Compact shortlist ---
        if tier2_ids:
            print(f"  {'─' * 50}")
            print("  More results:\n")
            for cid in tier2_ids:
                m = meta.get(cid, {})
                short_id = cid[:12]
                workspace = _format_workspace(m.get("workspace"))
                started = (m.get("started_at") or "")[:10]
                score = conv_scores[cid]

                # Snippet from best chunk
                best = max(conv_chunks[cid], key=lambda c: c["score"])
                snippet = best["text"][:120].replace("\n", " ")
                if len(best["text"]) > 120:
                    snippet += "..."

                # File count tag
                file_refs = best.get("file_refs", [])
                files_tag = f"  [{len(file_refs)} files]" if file_refs else ""

                print(
                    f"  {short_id}  {score:.3f}  {workspace:20s}  {started}{files_tag}  {snippet}"
                )
            print()

    def _print_exchange(self, conn: sqlite3.Connection, source_ids: list[str]) -> None:
        """Print role-labeled exchange text for tier 1 thread output."""
        exchanges = fetch_prompt_response_texts(conn, source_ids)

        for _pid, prompt_text, response_text in exchanges:
            if prompt_text:
                # Truncate very long prompts sensibly
                if len(prompt_text) > 500:
                    prompt_text = prompt_text[:500] + "..."
                print(f"  [user] {prompt_text}")
            if response_text:
                # Truncate very long responses
                if len(response_text) > 800:
                    response_text = response_text[:800] + "..."
                print(f"  [asst] {response_text}")

        if not exchanges:
            print("  (no exchange text available)")


class ConversationFormatter:
    """Aggregate scores per conversation, return ranked conversations."""

    def format(self, ctx: FormatterContext) -> None:
        limit = getattr(ctx.args, "limit", 10)

        # Group by conversation
        by_conv: dict[str, list[dict]] = {}
        for r in ctx.results:
            by_conv.setdefault(r["conversation_id"], []).append(r)

        # Score each conversation: max score, with best excerpt
        conv_scores = []
        for conv_id, chunks in by_conv.items():
            max_score = max(c["score"] for c in chunks)
            mean_score = _mean(c["score"] for c in chunks)
            best_chunk = max(chunks, key=lambda c: c["score"])
            conv_scores.append(
                {
                    "conversation_id": conv_id,
                    "max_score": max_score,
                    "mean_score": mean_score,
                    "chunk_count": len(chunks),
                    "best_excerpt": best_chunk["text"],
                    "best_chunk": best_chunk,
                }
            )

        conv_scores.sort(key=lambda x: x["max_score"], reverse=True)
        conv_scores = conv_scores[:limit]

        # Enrich with metadata
        meta = _get_conversation_metadata(
            ctx.conn, [c["conversation_id"] for c in conv_scores]
        )

        print(f"Conversations for: {ctx.query}\n")
        for c in conv_scores:
            conv_id = c["conversation_id"]
            m = meta.get(conv_id, {})
            short_id = conv_id[:12]
            workspace = _format_workspace(m.get("workspace"))
            started = (m.get("started_at") or "")[:10]
            max_s = c["max_score"]
            mean_s = c["mean_score"]
            n_chunks = c["chunk_count"]

            print(
                f"  {short_id}  max={max_s:.3f}  mean={mean_s:.3f}  [{n_chunks} chunks]  {started}  {workspace}"
            )
            snippet = c["best_excerpt"][:200].replace("\n", " ")
            if len(c["best_excerpt"]) > 200:
                snippet += "..."
            print(f"    {snippet}")
            print()


class JsonFormatter:
    """Structured JSON output for machine consumption.

    Includes all data: chunks, scores, conversation metadata, timestamps, file refs.
    Works with all retrieval modes (default, --conversations, --thread, etc.).
    """

    def format(self, ctx: FormatterContext) -> None:
        # Fetch conversation metadata for all results
        conv_ids = list({r["conversation_id"] for r in ctx.results})
        meta = _get_conversation_metadata(ctx.conn, conv_ids)

        # Build structured output
        output = {
            "query": ctx.query,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "result_count": len(ctx.results),
            "results": [],
        }

        # Check if --conversations mode (aggregate by conversation)
        if getattr(ctx.args, "conversations", False):
            output["mode"] = "conversations"
            output["results"] = self._format_conversation_results(ctx, meta)
        else:
            output["mode"] = "chunks"
            output["results"] = self._format_chunk_results(ctx, meta)

        print(json.dumps(output, indent=2, default=str))

    def _format_chunk_results(
        self, ctx: FormatterContext, meta: dict[str, dict]
    ) -> list[dict]:
        """Format results as individual chunks."""
        results = []
        for r in ctx.results:
            conv_id = r["conversation_id"]
            m = meta.get(conv_id, {})

            chunk = {
                "chunk_id": r.get("chunk_id"),
                "conversation_id": conv_id,
                "score": round(r["score"], 4),
                "chunk_type": r["chunk_type"],
                "text": r["text"],
                "source_ids": r.get("source_ids", []),
                "conversation": {
                    "started_at": m.get("started_at"),
                    "workspace": m.get("workspace"),
                },
            }

            # Include file refs if present
            file_refs = r.get("file_refs")
            if file_refs:
                chunk["file_refs"] = [
                    {
                        "basename": ref.basename,
                        "path": ref.path,
                        "op": ref.op,
                        "content_length": len(ref.content) if ref.content else 0,
                    }
                    for ref in file_refs
                ]

            results.append(chunk)

        return results

    def _format_conversation_results(
        self, ctx: FormatterContext, meta: dict[str, dict]
    ) -> list[dict]:
        """Format results aggregated by conversation."""
        limit = getattr(ctx.args, "limit", 10)

        # Group by conversation
        by_conv: dict[str, list[dict]] = {}
        for r in ctx.results:
            by_conv.setdefault(r["conversation_id"], []).append(r)

        # Score each conversation
        conv_scores = []
        for conv_id, chunks in by_conv.items():
            max_score = max(c["score"] for c in chunks)
            mean_score = _mean(c["score"] for c in chunks)
            best_chunk = max(chunks, key=lambda c: c["score"])
            m = meta.get(conv_id, {})

            conv_scores.append(
                {
                    "conversation_id": conv_id,
                    "max_score": round(max_score, 4),
                    "mean_score": round(mean_score, 4),
                    "chunk_count": len(chunks),
                    "started_at": m.get("started_at"),
                    "workspace": m.get("workspace"),
                    "best_chunk": {
                        "chunk_id": best_chunk.get("chunk_id"),
                        "score": round(best_chunk["score"], 4),
                        "chunk_type": best_chunk["chunk_type"],
                        "text": best_chunk["text"],
                    },
                }
            )

        conv_scores.sort(key=lambda x: x["max_score"], reverse=True)
        return conv_scores[:limit]


def select_formatter(args: argparse.Namespace) -> OutputFormatter:
    """Select appropriate formatter based on command-line arguments.

    Checks for explicit --format NAME first (supports drop-in formatters),
    then falls back to flag-based selection for built-in formatters.
    """
    # Check for explicit formatter name (supports plugins)
    format_name = getattr(args, "format", None)
    if format_name:
        from siftd.output.registry import get_formatter, get_registry

        formatter = get_formatter(format_name)
        if formatter:
            return formatter
        # Unknown format name: error with available options
        available = get_registry().list_names()
        raise ValueError(
            f"Unknown format '{format_name}'. Available: {', '.join(available)}"
        )

    # --json is shorthand for --format json
    if getattr(args, "json", False):
        return JsonFormatter()

    # Built-in formatters based on flags
    if getattr(args, "conversations", False):
        return ConversationFormatter()
    if getattr(args, "thread", False):
        return ThreadFormatter()
    context_n = getattr(args, "context", None)
    if context_n is not None:
        return ContextFormatter(n=context_n)
    if getattr(args, "full", False):
        return FullExchangeFormatter()
    if getattr(args, "verbose", False):
        return VerboseFormatter()
    return ChunkListFormatter()
