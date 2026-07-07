"""Search API extensions.

Re-exports core search functionality and adds post-processing functions.

Heavy dependencies (numpy via siftd.search, siftd.storage.embeddings) are lazy-imported so
that non-search CLI commands (`siftd query`, `siftd tag`) don't pay numpy's import latency
(tens of ms) on paths that never touch vector search.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean as _mean
from typing import TYPE_CHECKING, Any

from siftd.domain.search_types import (
    ConversationSearchSummary,
    ScoreBreakdown,
    SearchChunk,
    SearchView,
)
from siftd.storage.filters import EVENT_TAG_KINDS
from siftd.storage.queries import (
    fetch_all_conversation_ids,
    fetch_conversation_timestamps,
    fetch_prompt_response_texts,
    fetch_prompt_timestamps,
)

if TYPE_CHECKING:
    from siftd.embeddings.base import EmbeddingBackend, EmbeddingConfigError
    from siftd.embeddings.indexer import EmbedIndexStatus, IncrementalCompatError
    from siftd.search import apply_temporal_weight
    from siftd.storage.embeddings import IndexCompatError

# Lazy re-exports — resolved on first access to keep numpy's import latency off hot
# non-search paths.
_LAZY_IMPORTS = {
    "SearchResult": "siftd.search",
    "apply_temporal_weight": "siftd.search",
    "IndexCompatError": "siftd.storage.embeddings",
    "IncrementalCompatError": "siftd.embeddings.indexer",
    "EmbedIndexStatus": "siftd.embeddings.indexer",
    # Re-exported through the api boundary so the CLI (which must not import
    # siftd.embeddings) can catch a config failure — e.g. a revoked API key.
    "EmbeddingConfigError": "siftd.embeddings.base",
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        import importlib

        mod = importlib.import_module(_LAZY_IMPORTS[name])
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def embeddings_available() -> bool:
    """Return whether optional embedding dependencies are installed."""
    from siftd.embeddings import embeddings_available as _embeddings_available

    return _embeddings_available()


__all__ = [
    "SearchChunk",
    "SearchResult",
    "ScoreBreakdown",
    "ConversationSearchSummary",
    "IncrementalCompatError",
    "EmbeddingConfigError",
    "embeddings_available",
    "search_chunks",
    "hybrid_search",
    "ConversationScore",
    "aggregate_by_conversation",
    "compute_thread_tiers",
    "filter_by_threshold",
    "sort_chunks_by_time",
    "enrich_search_metadata",
    "enrich_file_refs",
    "enrich_exchanges",
    "enrich_context_window",
    "enrich_around_window",
    "first_mention",
    "SearchView",
    "process_search_view",
    "search_view",
    "parse_turns_range",
    "build_index",
    "embed_status",
    "EmbedIndexStatus",
    # Temporal weighting
    "apply_temporal_weight",
    "fetch_conversation_timestamps",
    "list_conversation_ids",
    # Embeddings
    "open_embeddings_db",
    "search_similar",
    "validate_index_compat",
    "IndexCompatError",
    # FTS5
    "fts5_recall_conversations",
    "rebuild_fts_index",
    # Exchange data
    "fetch_prompt_response_texts",
]


def open_embeddings_db(
    db_path: Path,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open the embeddings database.

    Args:
        db_path: Path to the embeddings database file.
        read_only: If True, open in read-only mode.

    Returns:
        An open sqlite3.Connection.
    """
    from siftd.storage.embeddings import open_embeddings_db as _open_embeddings_db

    return _open_embeddings_db(db_path, read_only=read_only)


def search_similar(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    *,
    limit: int = 10,
    conversation_ids: set[str] | None = None,
    include_embeddings: bool = False,
) -> list[dict]:
    """Search for similar chunks in the embeddings database.

    Args:
        conn: Connection to embeddings database.
        query_embedding: The query embedding vector.
        limit: Maximum results to return.
        conversation_ids: Optional set of conversation IDs to filter by.
        include_embeddings: If True, include embedding vectors in results.

    Returns:
        List of result dicts with score, chunk_id, conversation_id, text, etc.
    """
    from siftd.storage.embeddings import search_similar as _search_similar

    return _search_similar(
        conn,
        query_embedding,
        limit=limit,
        conversation_ids=conversation_ids,
        include_embeddings=include_embeddings,
    )


def chunks_for_events(
    conn: sqlite3.Connection,
    event_ids: list[str],
) -> dict[str, list[dict]]:
    """Map event ids to their covering embed chunk(s) — the RRF FTS→vector bridge.

    Thin lazy re-export of :func:`siftd.storage.embeddings.chunks_for_events` so the
    engine (and its tests) reach the bridge through the api search surface.
    """
    from siftd.storage.embeddings import chunks_for_events as _chunks_for_events

    return _chunks_for_events(conn, event_ids)


def validate_index_compat(
    conn: sqlite3.Connection,
    backend_name: str,
    backend_model: str,
    backend_dimension: int,
    current_schema_version: int,
) -> None:
    """Validate that stored index metadata is compatible with the current backend.

    Args:
        conn: Embeddings database connection.
        backend_name: Current backend name (e.g., "fastembed", "ollama").
        backend_model: Current backend model (e.g., "BAAI/bge-small-en-v1.5").
        backend_dimension: Current embedding dimension.
        current_schema_version: Current schema version constant.

    Raises:
        IndexCompatError: If metadata indicates incompatibility with actionable message.

    Note:
        Missing metadata keys (pre-versioning indexes) are allowed with warning-level
        degradation — dimension validation still applies via search_similar().
    """
    from siftd.storage.embeddings import validate_index_compat as _validate

    return _validate(
        conn,
        backend_name,
        backend_model,
        backend_dimension,
        current_schema_version,
    )


def fts5_recall_conversations(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 80,
    raw_fts: bool = False,
) -> tuple[set[str], str]:
    """FTS5 recall to narrow candidate conversations for embedding search.

    Args:
        conn: Connection to main database.
        query: The search query string.
        limit: Maximum conversation IDs to return.
        raw_fts: If True, pass query directly to FTS5 without sanitization.

    Returns:
        Tuple of (conversation_id set, mode string).
        Mode is "and", "or", or "none".
    """
    from siftd.storage.fts import fts5_recall_conversations as _fts5_recall

    return _fts5_recall(conn, query, limit=limit, raw_fts=raw_fts)


def rebuild_fts_index(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index for the main database."""
    from siftd.storage.fts import rebuild_fts_index as _rebuild_fts_index

    _rebuild_fts_index(conn, commit=True)


def fts5_search_content(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    raw_fts: bool = False,
) -> list[dict]:
    """FTS5 keyword search over content.

    Args:
        conn: Connection to main database.
        query: The search query string.
        limit: Maximum results to return.
        raw_fts: If True, pass query directly to FTS5 without sanitization.

    Returns:
        List of dicts with: conversation_id, kind, snippet, rank.
    """
    from siftd.storage.fts import search_content as _search_content

    return _search_content(conn, query, limit=limit, raw_fts=raw_fts)


def list_conversation_ids(conn: sqlite3.Connection) -> set[str]:
    """Return all conversation IDs."""
    return set(fetch_all_conversation_ids(conn))


ConversationScore = ConversationSearchSummary
SearchResult = SearchChunk


def _as_chunk(r: SearchChunk | dict[str, Any]) -> SearchChunk:
    """Normalize mixed search result inputs to SearchChunk."""
    if isinstance(r, SearchChunk):
        return r
    return SearchChunk.from_mapping(r)


def _passes_relevance_threshold(score: float, breakdown: Any, threshold: float) -> bool:
    """Whether a chunk clears a relevance threshold — the rewired ``--threshold`` /
    ``first_mention`` gate, shared by the engine, :func:`filter_by_threshold`, and
    :func:`first_mention` so a threshold means the same thing across engines.

    The signal tested depends on what the chunk carries:

    * A **vector chunk** (breakdown with a ``vector_rank``) is tested on its cosine
      ``embedding_sim`` — NOT ``SearchChunk.score``, which in hybrid mode is the
      ~0.02-scale fused RRF score.
    * A **keyword-only / FTS-only entrant** (breakdown present, ``vector_rank`` None)
      is EXEMPT: it has no cosine, and thresholding it out would delete fusion's
      keyword-recall reason to exist (§5).
    * A chunk with **no breakdown** (an fts-mode result, or a bare programmatic
      result) is tested on its ``score`` — the fts path's bounded 0-1 normalized
      bm25, and the documented ``first_mention`` contract ("minimum score").
    """
    from collections.abc import Mapping

    if threshold <= 0:
        return True
    if isinstance(breakdown, ScoreBreakdown):
        if breakdown.vector_rank is None:
            return True
        return breakdown.embedding_sim >= threshold
    if isinstance(breakdown, Mapping):
        if breakdown.get("vector_rank") is None:
            return True
        return float(breakdown.get("embedding_sim", 0.0)) >= threshold
    return score >= threshold


def aggregate_by_conversation(
    results: list[SearchChunk] | list[dict[str, Any]],
    *,
    limit: int = 10,
) -> list[ConversationScore]:
    """Aggregate chunk results to conversation-level scores.

    Groups results by conversation, computes max/mean scores,
    and returns ranked conversations.

    Args:
        results: List of SearchResult from hybrid_search.
        limit: Maximum conversations to return.

    Returns:
        List of ConversationScore, sorted by max_score descending.
    """
    if not results:
        return []

    chunks = [_as_chunk(r) for r in results]

    # Group by conversation
    by_conv: dict[str, list[SearchChunk]] = {}
    for r in chunks:
        by_conv.setdefault(r.conversation_id, []).append(r)

    # Score each conversation
    conv_scores = []
    for conv_id, chunks in by_conv.items():
        scores = [c.score for c in chunks]
        best_chunk = max(chunks, key=lambda c: c.score)
        conv_scores.append(
            ConversationScore(
                conversation_id=conv_id,
                max_score=max(scores),
                mean_score=_mean(scores),
                chunk_count=len(chunks),
                best_excerpt=best_chunk.text[:500],
                workspace_path=best_chunk.workspace_path,
                started_at=best_chunk.started_at,
                file_refs=best_chunk.file_refs,
            )
        )

    conv_scores.sort(key=lambda x: x.max_score, reverse=True)
    return conv_scores[:limit]


def filter_by_threshold(
    results: list[SearchChunk] | list[dict[str, Any]],
    *,
    threshold: float | None,
) -> list[SearchChunk]:
    """Filter chunk results by *cosine* threshold (keyword-only entrants exempt).

    Tests ``breakdown.embedding_sim`` (not ``SearchChunk.score``, which is the
    ~0.02-scale fused RRF score) so ``--threshold 0.5`` keeps its 0-1 cosine
    meaning across engines; FTS-only entrants have no cosine and are retained."""
    chunks = [_as_chunk(r) for r in results]
    if threshold is None:
        return chunks
    return [r for r in chunks if _passes_relevance_threshold(r.score, r.breakdown, threshold)]


def sort_chunks_by_time(
    results: list[SearchChunk] | list[dict[str, Any]],
) -> list[SearchChunk]:
    """Sort chunks newest-first by date then chunk_id.

    ``--sort=time`` answers "what did I work on most recently that matches",
    so the most recent hit leads — the intuitive reading of a time sort (and
    consistent with the default recency order of the browse list)."""
    chunks = [_as_chunk(r) for r in results]
    return sorted(
        chunks, key=lambda r: ((r.started_at or "")[:10], r.chunk_id or ""), reverse=True
    )


def compute_thread_tiers(
    results: list[SearchChunk] | list[dict[str, Any]],
) -> tuple[list[SearchChunk], list[SearchChunk]]:
    """Split chunks into tier1 (expanded) and tier2 (compact) for thread mode."""
    chunks = [_as_chunk(r) for r in results]
    conv_scores: dict[str, float] = {}
    conv_best: dict[str, SearchChunk] = {}
    for r in chunks:
        cid = r.conversation_id
        if cid not in conv_scores or r.score > conv_scores[cid]:
            conv_scores[cid] = r.score
            conv_best[cid] = r

    scores = list(conv_scores.values())
    mean_score = sum(scores) / len(scores) if scores else 0.0

    tier1_ids = [cid for cid, s in conv_scores.items() if s > mean_score]
    tier2_ids = [cid for cid in conv_scores if cid not in set(tier1_ids)]

    tier1_ids.sort(key=lambda cid: (conv_best[cid].started_at or "")[:10])
    tier2_ids.sort(key=lambda cid: conv_scores[cid], reverse=True)

    return [conv_best[cid] for cid in tier1_ids], [conv_best[cid] for cid in tier2_ids]


def _workspace_label(path: str | None) -> str:
    """Mirror output.common.fmt_workspace without importing output layer."""
    if path is None:
        return ""
    if path in {"", "/"}:
        return "(root)"
    return Path(path).name


def enrich_search_metadata(conn: sqlite3.Connection, results: list[SearchChunk]) -> None:
    """Enrich chunks with workspace and started_at metadata in-place."""
    conv_ids = list({r.conversation_id for r in results})
    if not conv_ids:
        return

    placeholders = ",".join("?" * len(conv_ids))
    rows = conn.execute(
        f"""
        SELECT c.id, c.started_at, w.path AS workspace
        FROM conversations c
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE c.id IN ({placeholders})
    """,
        conv_ids,
    ).fetchall()
    meta = {row["id"]: dict(row) for row in rows}

    for r in results:
        m = meta.get(r.conversation_id, {})
        r.workspace_path = _workspace_label(m.get("workspace"))
        started = m.get("started_at")
        r.started_at = (started or "")[:10] if started else None


def enrich_tags(conn: sqlite3.Connection, results: list[SearchChunk]) -> None:
    """Attach element-level tags to each chunk in-place, batched (no N+1).

    A hit's ``event_id`` is the element address; any ``tag_assignments`` row whose
    ``target_id`` equals it (prompt/response/tool_call, or an ``exchange`` anchored
    on that prompt) contributes a tag chip.
    """
    event_ids = list({r.event_id for r in results if r.event_id})
    if not event_ids:
        return
    placeholders = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"SELECT ta.target_id, tg.name FROM tag_assignments ta "
        f"JOIN tags tg ON tg.id = ta.tag_id "
        f"WHERE ta.target_id IN ({placeholders}) ORDER BY tg.name",
        event_ids,
    ).fetchall()
    by_id: dict[str, list[str]] = {}
    for row in rows:
        by_id.setdefault(row["target_id"], []).append(row["name"])
    for r in results:
        if r.event_id and r.event_id in by_id:
            r.tags = by_id[r.event_id]


def _enrich_block_tags(
    conn: sqlite3.Connection, block_chunk_ids: list[tuple[SearchChunk, str]]
) -> None:
    """Set block chunks' chips from the BLOCK's own tags (target_id = block id).

    Block chunks carry ``event_id`` = the owning event (the folio-jump address),
    so :func:`enrich_tags`' event-keyed pass can't reach a block's tags — and
    would otherwise leak the owning event's unrelated tags onto the hit. This
    overrides that pass, keyed by block id, batched (rows already capped at ``n``).
    """
    if not block_chunk_ids:
        return
    block_ids = list({bid for _, bid in block_chunk_ids})
    placeholders = ",".join("?" * len(block_ids))
    rows = conn.execute(
        f"SELECT ta.target_id, tg.name FROM tag_assignments ta "
        f"JOIN tags tg ON tg.id = ta.tag_id "
        f"WHERE ta.target_id IN ({placeholders}) AND ta.target_kind = 'block' "
        f"ORDER BY tg.name",
        block_ids,
    ).fetchall()
    by_id: dict[str, list[str]] = {}
    for row in rows:
        by_id.setdefault(row["target_id"], []).append(row["name"])
    for chunk, block_id in block_chunk_ids:
        chunk.tags = by_id.get(block_id, [])


def enrich_file_refs(conn: sqlite3.Connection, results: list[SearchChunk]) -> None:
    """Attach file references to each chunk in-place."""
    from siftd.api import fetch_file_refs

    all_source_ids: list[str] = []
    for r in results:
        all_source_ids.extend(r.source_ids or [])
    if not all_source_ids:
        return

    refs_by_prompt = fetch_file_refs(conn, all_source_ids)
    for r in results:
        refs = []
        for sid in (r.source_ids or []):
            refs.extend(refs_by_prompt.get(sid, []))
        r.file_refs = refs


def enrich_exchanges(conn: sqlite3.Connection, results: list[SearchChunk]) -> None:
    """Attach full prompt+response exchanges for each chunk in-place."""
    for r in results:
        source_ids = r.source_ids or []
        if source_ids:
            r.exchanges = fetch_prompt_response_texts(conn, source_ids)


def enrich_context_window(conn: sqlite3.Connection, results: list[SearchChunk], n: int) -> None:
    """Attach +/-N context exchanges around each chunk's source prompts."""
    for r in results:
        source_ids = r.source_ids or []
        if not source_ids:
            continue

        all_prompts = conn.execute(
            """
            SELECT e.id FROM events e
            WHERE e.conversation_id = ? AND e.kind = 'prompt'
            ORDER BY e.timestamp
        """,
            (r.conversation_id,),
        ).fetchall()
        prompt_order = [row[0] for row in all_prompts]
        source_set = set(source_ids)
        source_indices = [i for i, pid in enumerate(prompt_order) if pid in source_set]
        if not source_indices:
            continue

        start = max(0, min(source_indices) - n)
        end = min(len(prompt_order), max(source_indices) + n + 1)
        context_ids = prompt_order[start:end]
        exchanges = fetch_prompt_response_texts(conn, context_ids)
        r.context_window = [(pid, pt, rt, pid in source_set) for pid, pt, rt in exchanges]


def _events_to_turn_indices(
    conn: sqlite3.Connection,
    event_ids: list[str],
    conv_id: str,
) -> list[int | None]:
    """Map a list of event_ids to turn indices for a given conversation.

    Batches the prompt-list lookup: fetches all prompts for conv_id once,
    then maps each event_id to its ordinal. Called by both _annotate_turn_positions
    and the query CLI ambiguous-match pre-pass.
    """

    prompt_rows = conn.execute(
        "SELECT id, timestamp FROM events WHERE conversation_id = ? AND kind = 'prompt' ORDER BY timestamp",
        (conv_id,),
    ).fetchall()
    prompt_id_to_idx = {row["id"]: i for i, row in enumerate(prompt_rows)}

    result: list[int | None] = []
    for eid in event_ids:
        row = conn.execute(
            "SELECT kind, timestamp FROM events WHERE id = ?",
            (eid,),
        ).fetchone()
        if row is None:
            result.append(None)
            continue
        kind = row["kind"]
        ts = row["timestamp"]
        if kind == "prompt":
            result.append(prompt_id_to_idx.get(eid))
        else:
            prompt_row = conn.execute(
                "SELECT id FROM events WHERE conversation_id = ? AND kind = 'prompt' AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                (conv_id, ts),
            ).fetchone()
            if prompt_row is None:
                result.append(None)
            else:
                result.append(prompt_id_to_idx.get(prompt_row["id"]))
    return result


def _annotate_turn_positions(conn: sqlite3.Connection, chunks: list[SearchChunk]) -> None:
    """Populate turn_index (and event_id for semantic) on each chunk, batched by conversation.

    Runs in-place as a finalization pass inside hybrid_search() for all modes.
    FTS5 chunks have event_id already set (from fts5_search_content result dict).
    Semantic/hybrid chunks derive event_id from source_ids[0] and look up its ordinal.
    """
    from siftd.storage.fts import fts5_event_turn_index

    by_conv: dict[str, list[SearchChunk]] = {}
    for chunk in chunks:
        by_conv.setdefault(chunk.conversation_id, []).append(chunk)

    for conv_id, conv_chunks in by_conv.items():
        try:
            prompt_rows = conn.execute(
                "SELECT id, timestamp FROM events WHERE conversation_id = ? AND kind = 'prompt' ORDER BY timestamp",
                (conv_id,),
            ).fetchall()
        except Exception:
            continue  # enrichment is non-fatal — turn_index stays None
        prompt_id_to_idx = {row["id"]: i for i, row in enumerate(prompt_rows)}

        for chunk in conv_chunks:
            if chunk.source_ids:
                # Semantic/hybrid chunk: use first source_id as the event anchor
                first_src = chunk.source_ids[0]
                chunk.event_id = first_src
                turn_idx = prompt_id_to_idx.get(first_src)
                if turn_idx is None:
                    # Non-prompt source (e.g. tool_summary) — derive from event's surrounding prompt
                    turn_idx = fts5_event_turn_index(conn, first_src, conv_id)
                chunk.turn_index = turn_idx
            elif chunk.event_id:
                # FTS5 chunk: event_id already set; derive turn index
                chunk.turn_index = fts5_event_turn_index(conn, chunk.event_id, conv_id)


def phrase_events_in_conversation(
    conn: sqlite3.Connection,
    phrase: str,
    *,
    conversation_id: str,
) -> list[str]:
    """Return all event IDs in conversation that FTS5-match phrase, in order."""
    from siftd.storage.fts import fts5_all_events_in_conversation

    return fts5_all_events_in_conversation(conn, phrase, conversation_id=conversation_id)


def enrich_around_window(
    conn: sqlite3.Connection,
    chunks: list,
    phrase: str,
    window_start: int,
    window_end: int,
) -> tuple[list, int]:
    """Enrich search chunks with context window anchored on FTS5 phrase match.

    For each chunk, finds the first FTS5 match of phrase in the conversation,
    computes the turn index for that match, and fetches the window around it.
    Updates chunk.turn_index to reflect the --around phrase anchor.

    Returns (enriched_chunks, n_skipped) — chunks where phrase was not found
    in the conversation are dropped (not silently carried through with stale
    turn_index). Caller should warn the user when n_skipped > 0.
    """
    from siftd.storage.fts import fts5_event_turn_index, fts5_first_event_in_conversation

    kept: list = []
    n_skipped = 0

    for chunk in chunks:
        conv_id = chunk.conversation_id if hasattr(chunk, "conversation_id") else chunk.get("conversation_id")
        if not conv_id:
            n_skipped += 1
            continue

        event_id = fts5_first_event_in_conversation(conn, phrase, conversation_id=conv_id)
        if not isinstance(event_id, str):
            n_skipped += 1
            continue

        turn_idx = fts5_event_turn_index(conn, event_id, conv_id)
        if turn_idx is None:
            n_skipped += 1
            continue

        if hasattr(chunk, "turn_index"):
            chunk.turn_index = turn_idx

        all_prompts = conn.execute(
            "SELECT e.id FROM events e WHERE e.conversation_id = ? AND e.kind = 'prompt' ORDER BY e.timestamp",
            (conv_id,),
        ).fetchall()
        prompt_order = [row[0] for row in all_prompts]

        start = max(0, turn_idx + window_start)
        end = min(len(prompt_order), turn_idx + window_end + 1)
        window_ids = prompt_order[start:end]
        exchanges = fetch_prompt_response_texts(conn, window_ids)
        anchor_prompt_id = prompt_order[turn_idx] if turn_idx < len(prompt_order) else None
        if hasattr(chunk, "context_window"):
            chunk.context_window = [(pid, pt, rt, pid == anchor_prompt_id) for pid, pt, rt in exchanges]
        kept.append(chunk)

    return kept, n_skipped


def first_mention(
    results: list[SearchChunk] | list[dict[str, Any]],
    *,
    threshold: float = 0.65,
    db_path: Path | None = None,
) -> SearchChunk | dict[str, Any] | None:
    """Find chronologically earliest result above relevance threshold.

    Args:
        results: List of SearchChunk or raw dicts from search.
            Dicts must have 'score', 'conversation_id', and 'source_ids'.
        threshold: Minimum score to consider relevant.
        db_path: Path to database (for timestamp lookup). Uses default if not specified.

    Returns:
        Earliest result above threshold (same type as input), or None if none qualify.
    """
    from siftd.paths import db_path as default_db_path

    def _get(r: SearchChunk | dict[str, Any], key: str):
        """Access attribute or dict key."""
        if isinstance(r, dict):
            return r.get(key)
        return getattr(r, key, None)

    # Filter to results above the relevance threshold — cosine for vector chunks,
    # score for breakdown-less results, entrants exempt (matching filter_by_threshold).
    above = [
        r for r in results
        if _passes_relevance_threshold(_get(r, "score") or 0.0, _get(r, "breakdown"), threshold)
    ]
    if not above:
        return None

    db = db_path or default_db_path()

    from siftd.api.database import open_database

    conn = open_database(db, read_only=True)

    # Collect all prompt IDs from source_ids for timestamp lookup
    all_prompt_ids = []
    for r in above:
        source_ids = _get(r, "source_ids") or []
        all_prompt_ids.extend(source_ids)

    # Get prompt timestamps (preferred) and conversation timestamps (fallback)
    prompt_times = fetch_prompt_timestamps(conn, all_prompt_ids) if all_prompt_ids else {}
    conv_ids = list({_get(r, "conversation_id") for r in above})
    conv_times = fetch_conversation_timestamps(conn, conv_ids)
    conn.close()

    def earliest_prompt_time(r):
        """Get earliest prompt timestamp for a result, fallback to conversation start."""
        source_ids = _get(r, "source_ids") or []
        if source_ids:
            # Get timestamps for this result's prompts
            times = [prompt_times.get(pid, "") for pid in source_ids]
            valid_times = [t for t in times if t]
            if valid_times:
                return min(valid_times)
        # Fallback to conversation start time
        return conv_times.get(_get(r, "conversation_id"), "")

    # Sort by earliest prompt timestamp, then by chunk_id as tiebreaker
    above.sort(key=lambda r: (earliest_prompt_time(r), _get(r, "chunk_id") or ""))

    return above[0]


def process_search_view(
    chunks: list[SearchChunk],
    conn: sqlite3.Connection,
    *,
    view: str = "chunks",
    sort: str = "score",
    select: str = "all",
    threshold: float | None = None,
    limit: int = 10,
    full: bool = False,
    around: str | None = None,
    turns_range: tuple[int, int] | None = None,
    db_path: Path | None = None,
) -> SearchView:
    """Run the shared search post-processing recipe over engine chunks.

    The single owner of the steps that used to live inline in the CLI handler:
    threshold filter → ``--select first`` → limit trim → metadata/file-ref
    enrichment → ``--sort time`` → the conversations/thread view shape →
    ``--full`` exchanges → the ``--around`` window. Operating on
    :class:`SearchChunk` objects end-to-end (one dict conversion, at the render
    boundary), it is the location every surface composes, so the recipe cannot
    drift between the CLI's two paths or any serve surface that adopts it. Steps
    are opt-in via the keyword controls — a caller wanting only a subset (e.g.
    the keyword-only path, which excludes embeddings-dependent richness) leaves
    the rest at their defaults.

    ``conn`` is a read-only main-DB connection for the enrichment steps;
    ``db_path`` is consulted only by ``--select first`` (which opens its own
    connection for the timestamp lookup).
    """
    # 1. Score threshold (client-side post-filter).
    if threshold is not None:
        chunks = filter_by_threshold(chunks, threshold=threshold)
        if not chunks:
            return SearchView(results=[], view=view, empty_reason="threshold")

    # 2. --select first: chronologically earliest match above the threshold.
    if select == "first":
        effective_threshold = threshold if threshold is not None else 0.65
        earliest = first_mention(chunks, threshold=effective_threshold, db_path=db_path)
        if earliest is None:
            return SearchView(results=[], view=view, empty_reason="first")
        chunks = [_as_chunk(earliest)]

    # 3. Trim to the requested count — only the chunks view; the aggregate and
    #    thread views manage their own (widened) candidate pools downstream.
    if view == "chunks":
        chunks = chunks[:limit]

    # 4. Enrich with conversation metadata (+ file refs, except the aggregate
    #    view, which never displays them). Both mutate the chunks in place.
    enrich_search_metadata(conn, chunks)
    if view != "conversations":
        enrich_file_refs(conn, chunks)
        enrich_tags(conn, chunks)

    # 5. --sort time (chunks view only; the other views impose their own order
    #    and reject --sort=time at axis validation).
    if sort == "time" and view == "chunks":
        chunks = sort_chunks_by_time(chunks)

    # 6. View shape.
    if view == "conversations":
        convs = aggregate_by_conversation(chunks, limit=limit)
        return SearchView(results=[c.to_render_dict() for c in convs], view=view)

    if view == "thread":
        enrich_exchanges(conn, chunks)  # tier1 displays full exchanges
        tier1, tier2 = compute_thread_tiers(chunks)
        return SearchView(
            results=[c.to_render_dict() for c in chunks],
            view=view,
            tier1=[c.to_render_dict() for c in tier1],
            tier2=[c.to_render_dict() for c in tier2],
        )

    # Chunks view: optional full-exchange and phrase-anchored window enrichment.
    if full:
        enrich_exchanges(conn, chunks)

    n_skipped = 0
    if around is not None and turns_range is not None:
        window_start, window_end = turns_range
        chunks, n_skipped = enrich_around_window(conn, chunks, around, window_start, window_end)

    return SearchView(
        results=[c.to_render_dict() for c in chunks],
        view=view,
        n_skipped=n_skipped,
    )


def build_index(
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
    rebuild: bool = False,
    verbose: bool = False,
) -> dict:
    """Build or incrementally update the embeddings index.

    Thin wrapper over ``siftd.embeddings.indexer.build_embeddings_index`` returning a
    dict. The backend is config-driven (``embed.backend``) — there is no override arg.

    Args:
        db_path: Path to main database. Uses default if not specified.
        embed_db_path: Path to embeddings database. Uses default if not specified.
        rebuild: If True, clear and rebuild from scratch.
        verbose: Print progress messages.

    Returns:
        Dict with add/remove counts and backend identity.

    Raises:
        FileNotFoundError: If main database doesn't exist.
        IncrementalCompatError: If an incremental build can't proceed.
        RuntimeError: If no embedding backend is available.
        EmbeddingsNotAvailable: If no embedding backend is configured/installed.
    """
    from siftd.embeddings import require_embeddings

    require_embeddings("Building embeddings index")

    from siftd.embeddings.indexer import build_embeddings_index

    stats = build_embeddings_index(
        db_path=db_path,
        embed_db_path=embed_db_path,
        rebuild=rebuild,
        verbose=verbose,
    )
    return {
        "chunks_added": stats.chunks_added,
        "chunks_removed": stats.chunks_removed,
        "conversations_indexed": stats.conversations_indexed,
        "conversations_pruned": stats.conversations_pruned,
        "total_chunks": stats.total_chunks,
        "backend_name": stats.backend_name,
        "model": stats.model,
        "dimension": stats.dimension,
    }


def embed_status(
    *,
    db_path: Path | None = None,
    embed_db_path: Path | None = None,
):
    """Return an :class:`EmbedIndexStatus` for ``siftd embed --status``.

    Config/metadata-driven (no ONNX load, no network) — reports the configured backend
    plus the built index's identity, chunk counts, coverage, staleness, and size.
    """
    from siftd.embeddings.indexer import embed_index_status

    return embed_index_status(db_path=db_path, embed_db_path=embed_db_path)


SEARCH_MODES = ("auto", "fts", "semantic", "hybrid")
"""Valid engine-mode selectors. ``auto`` resolves to a concrete engine at
request time; ``fts``/``semantic``/``hybrid`` name the engine directly."""


class EmbeddingsRequiredError(ValueError):
    """Raised when an explicit ``semantic``/``hybrid`` mode is requested but
    embeddings are unavailable. Distinct from a plain ``ValueError`` so callers
    can map it to an install/index hint (CLI) or a 4xx (route) rather than a
    generic invalid-argument message."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(f"mode {mode!r} requires embeddings")


def resolve_search_mode(requested: str, *, has_embeddings: bool) -> str:
    """Resolve a requested engine mode to the concrete engine that will run.

    The single source of truth for ``auto`` resolution, shared by the CLI and
    the serve route so the two surfaces cannot drift. ``auto`` → ``hybrid``
    when embeddings are available, else ``fts``. Explicit ``semantic``/
    ``hybrid`` require embeddings and raise :class:`EmbeddingsRequiredError`
    when absent; ``fts`` always resolves to ``fts``.

    The returned value is what should be reported back to the caller as the
    engine that actually ran (``output["mode"]``) — never ``auto``.
    """
    if requested not in SEARCH_MODES:
        raise ValueError(
            f"invalid mode: {requested!r}; expected one of {', '.join(SEARCH_MODES)}"
        )
    if requested == "auto":
        return "hybrid" if has_embeddings else "fts"
    if requested in ("semantic", "hybrid") and not has_embeddings:
        raise EmbeddingsRequiredError(requested)
    return requested


def search_chunks(
    q: str,
    *,
    db_path: Path,
    embed_db: Path | None = None,
    n: int = 10,
    mode: str = "hybrid",
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    exclude_active: bool = True,
    include_derivative: bool = False,
    owner: str | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
    recall: int | None = None,
    rerank: str = "mmr",
    lambda_: float = 0.7,
    recency: bool = False,
    recency_half_life: float = 30.0,
    recency_max_boost: float = 1.15,
    threshold: float = 0.0,
    embed_backend: EmbeddingBackend | None = None,
    raw_fts: bool = False,
) -> list[SearchChunk]:
    """Canonical entry point for retrieving search chunks."""
    return hybrid_search(
        q,
        db_path=db_path,
        embed_db=embed_db,
        n=n,
        mode=mode,
        workspace=workspace,
        model=model,
        since=since,
        before=before,
        tag=tag,
        all_tags=all_tags,
        no_tag=no_tag,
        tag_kind=tag_kind,
        exclude_active=exclude_active,
        include_derivative=include_derivative,
        owner=owner,
        tool=tool,
        tool_tag=tool_tag,
        recall=recall,
        rerank=rerank,
        lambda_=lambda_,
        recency=recency,
        recency_half_life=recency_half_life,
        recency_max_boost=recency_max_boost,
        threshold=threshold,
        embed_backend=embed_backend,
        raw_fts=raw_fts,
    )


# Shipped RRF fusion parameters — the winning 0.11.0 bench config
# `w1.0_kkw20_kvec20_p300_l1.0` on the voyage arm (docs/dev/bench-stage2-chunking-design
# -2026-07-06.md, "Complete verdict"). Weighted reciprocal-rank fusion with per-list
# damping k: `score = w_kw/(k_kw + keyword_rank) + 1/(k_vec + vector_rank)`. The smaller
# per-list k (20, vs the field-standard 60) sharpens the 1/rank curve the bench preferred;
# the 300-deep vector pool and a pure-relevance (λ=1.0) vector list feed the fusion; the
# conversation-dedup rollup (best-ranked chunk per conversation) runs on the fused output
# — narrow gets that dedup for free inside MMR, RRF needs it as an explicit stage.
_RRF_W_KW = 1.0
_RRF_K_KW = 20
_RRF_K_VEC = 20
_RRF_POOL = 300
_RRF_LAMBDA = 1.0


def _normalize_fts_score(rank: float) -> float:
    """Map a bm25 rank to a bounded (0,1) score, monotone-increasing with quality.

    FTS5 ranks are negative and more-negative = better; ``abs(rank)/(1+abs(rank))``
    is bounded and rises with ``|rank|``, so the best (most-negative) hit carries
    the max score. This inverts v1's ``1/(1+abs(rank))``, which ranked the weakest
    match highest."""
    a = abs(rank)
    return a / (1.0 + a)


def _resolve_search_backend(embed_backend: EmbeddingBackend | None) -> EmbeddingBackend:
    """Return the injected backend, or resolve the config-driven one.

    No probe/retry chain: resolution is deterministic (``embed.backend``); a
    transient reachability failure surfaces later at ``embed_query`` and is
    handled by the degrade in :func:`search_view`, not here."""
    if embed_backend is not None:
        return embed_backend
    from siftd.embeddings.base import get_backend

    return get_backend(verbose=False)


def _build_vector_list(
    embed_conn: sqlite3.Connection,
    query_embedding: list[float],
    candidate_ids: set[str] | None,
    *,
    search_limit: int,
    mmr_limit: int,
    use_mmr: bool,
    lambda_: float,
    mmr_cap: int,
    recency: bool,
    recency_half_life: float,
    recency_max_boost: float,
    db_path: Path,
) -> list[dict]:
    """The vector ranked list: cosine → recency → MMR, ordered best-first.

    Shared by semantic mode (returned directly) and hybrid mode (fed to fusion).
    Each result's ``breakdown.vector_rank`` is stamped with its 1-based position —
    the "has a computed cosine" marker the similarity threshold reads."""
    from siftd.search import apply_temporal_weight, mmr_rerank
    from siftd.storage.sqlite import open_database

    results = search_similar(
        embed_conn,
        query_embedding,
        limit=search_limit,
        conversation_ids=candidate_ids,
        include_embeddings=use_mmr,
    )
    if not results:
        return []

    if recency:
        conv_ids_for_ts = list({r["conversation_id"] for r in results})
        ts_conn = open_database(db_path, read_only=True)
        try:
            timestamps = fetch_conversation_timestamps(ts_conn, conv_ids_for_ts)
        finally:
            ts_conn.close()
        results = apply_temporal_weight(
            results, timestamps,
            half_life_days=recency_half_life, max_boost=recency_max_boost,
        )

    if use_mmr:
        # Cap candidates to bound the np.vstack inside mmr_rerank().
        if len(results) > mmr_cap:
            results = sorted(results, key=lambda r: -r["score"])[:mmr_cap]
        results = mmr_rerank(results, query_embedding, lambda_=lambda_, limit=mmr_limit)
        # Outward score = MMR-adjusted final (for semantic display / downstream sort).
        for r in results:
            breakdown = r.get("breakdown")
            final_score = getattr(breakdown, "final_score", None)
            if final_score is not None:
                r["score"] = float(final_score)
    else:
        # Deterministic relevance order; chunk_id (ULID) breaks ties.
        results = sorted(results, key=lambda r: (-r["score"], r.get("chunk_id", "")))
        results = results[:mmr_limit]

    for i, r in enumerate(results):
        breakdown = r.get("breakdown")
        if isinstance(breakdown, ScoreBreakdown):
            breakdown.vector_rank = i + 1
    return results


def _entrant_result(hit: dict) -> dict:
    """An FTS hit with no covering chunk → an engine result dict (vector-free)."""
    return {
        "conversation_id": hit["conversation_id"],
        "chunk_id": None,
        "chunk_type": hit["kind"],
        "text": hit["snippet"],
        "source_ids": [],
        "event_id": hit.get("event_id"),
        "breakdown": ScoreBreakdown(embedding_sim=0.0),
    }


def _keyword_only_result(chunk: dict) -> dict:
    """A covering chunk absent from the vector list → a vector-free result dict."""
    return {
        "conversation_id": chunk["conversation_id"],
        "chunk_id": chunk["chunk_id"],
        "chunk_type": chunk["chunk_type"],
        "text": chunk["text"],
        "source_ids": chunk.get("source_ids") or [],
        "breakdown": ScoreBreakdown(embedding_sim=0.0),
    }


@dataclass
class _FusionItem:
    """One member of the RRF fusion universe: an engine result plus its per-list ranks.

    ``vector_rank``/``keyword_rank`` are 1-based positions (``None`` = absent from
    that list). ``result`` is the engine result dict (a vector chunk, a keyword-only
    covering chunk, or an FTS-only entrant)."""

    result: dict
    vector_rank: int | None
    keyword_rank: int | None

    def note_keyword_rank(self, rank: int) -> None:
        """Record the best (lowest) keyword rank seen for this item."""
        if self.keyword_rank is None or rank < self.keyword_rank:
            self.keyword_rank = rank

    def fused_score(
        self, *, w_kw: float = _RRF_W_KW, k_kw: int = _RRF_K_KW, k_vec: int = _RRF_K_VEC
    ) -> float:
        """Weighted RRF over the lists this item appears in:
        ``1/(k_vec + vector_rank) + w_kw/(k_kw + keyword_rank)``."""
        fused = 0.0
        if self.vector_rank is not None:
            fused += 1.0 / (k_vec + self.vector_rank)
        if self.keyword_rank is not None:
            fused += w_kw / (k_kw + self.keyword_rank)
        return fused


def _fuse_hybrid(
    vector_results: list[dict],
    keyword_hits: list[dict],
    embed_conn: sqlite3.Connection,
    *,
    n: int,
    w_kw: float = _RRF_W_KW,
    k_kw: int = _RRF_K_KW,
    k_vec: int = _RRF_K_VEC,
    dedup: bool = True,
) -> list[dict]:
    """Weighted-reciprocal-rank-fuse the vector and keyword lists into one ordered list.

    Two ranked lists over the same candidate set combine by
    ``1/(k_vec + vector_rank) + w_kw/(k_kw + keyword_rank)``: the vector list carries
    ``vector_rank`` (cosine order, post-MMR); each FTS hit bridges via ``source_ids``
    membership to its covering chunk(s), contributing the best (lowest) bm25 position as
    ``keyword_rank``. FTS hits with no covering chunk become vector-free entrants. A hit
    that bridges never also becomes an entrant.

    ``dedup`` applies the conversation rollup: the fused list is the *chunk* ranking, but
    a conversation is the retrieval unit, so keeping the best-ranked chunk per
    conversation projects the chunk ranking onto ``n`` distinct conversations (the
    flooding fix — the RRF analogue of MMR's same-conversation suppression on the narrow
    path). Returns the top-``n`` fused result dicts, each breakdown stamped with
    ``vector_rank``/``keyword_rank``/``fused_score`` and ``score`` = fused score."""
    # key = chunk_id for real chunks, "entrant:<event_id>" for FTS-only entrants.
    fusion: dict[str, _FusionItem] = {}
    for r in vector_results:
        breakdown = r.get("breakdown")
        vr = breakdown.vector_rank if isinstance(breakdown, ScoreBreakdown) else None
        fusion[r["chunk_id"]] = _FusionItem(result=r, vector_rank=vr, keyword_rank=None)

    event_ids = [h["event_id"] for h in keyword_hits if h.get("event_id")]
    covering = chunks_for_events(embed_conn, event_ids)

    for pos, hit in enumerate(keyword_hits):
        kr = pos + 1
        eid = hit.get("event_id")
        chunks = covering.get(eid) if eid else None
        if chunks:
            for ch in chunks:
                cid = ch["chunk_id"]
                item = fusion.get(cid)
                if item is None:
                    item = _FusionItem(result=_keyword_only_result(ch), vector_rank=None, keyword_rank=None)
                    fusion[cid] = item
                item.note_keyword_rank(kr)
        elif eid:
            ekey = f"entrant:{eid}"
            item = fusion.get(ekey)
            if item is None:
                fusion[ekey] = _FusionItem(result=_entrant_result(hit), vector_rank=None, keyword_rank=kr)
            else:
                item.note_keyword_rank(kr)

    def _fused(item: _FusionItem) -> float:
        return item.fused_score(w_kw=w_kw, k_kw=k_kw, k_vec=k_vec)

    # Fused desc; the fusion key (chunk_id / entrant:event_id) breaks ties deterministically.
    scored = sorted(fusion.items(), key=lambda kv: (-_fused(kv[1]), kv[0]))

    out: list[dict] = []
    seen_convs: set[str] = set()
    for _key, item in scored:
        conv_id = item.result["conversation_id"]
        if dedup and conv_id in seen_convs:
            continue  # conversation rollup: best-ranked chunk represents its conversation
        seen_convs.add(conv_id)
        r = dict(item.result)
        breakdown = r.get("breakdown")
        if not isinstance(breakdown, ScoreBreakdown):
            breakdown = ScoreBreakdown(embedding_sim=0.0)
        fused = _fused(item)
        breakdown.vector_rank = item.vector_rank
        breakdown.keyword_rank = item.keyword_rank
        breakdown.fused_score = fused
        breakdown.fts5_matched = item.keyword_rank is not None
        r["breakdown"] = breakdown
        r["score"] = fused
        out.append(r)
        if len(out) >= n:
            break
    return out


def hybrid_search(
    q: str,
    *,
    db_path: Path,
    embed_db: Path | None = None,
    n: int = 10,
    mode: str = "hybrid",
    # Filters
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    exclude_active: bool = True,
    include_derivative: bool = False,
    owner: str | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
    # FTS5 tuning. recall=None ⇒ the shipped narrow-path candidate width (40, see
    # NARROW_RECALL_DEFAULT); an explicit int overrides (wider = more semantic reach on a
    # strong embedder, more FTS dilution on a weak one).
    recall: int | None = None,
    raw_fts: bool = False,
    # Reranking
    rerank: str = "mmr",
    lambda_: float = 0.7,
    # Recency
    recency: bool = False,
    recency_half_life: float = 30.0,
    recency_max_boost: float = 1.15,
    # Threshold
    threshold: float = 0.0,
    # Backend
    embed_backend: EmbeddingBackend | None = None,
) -> list[SearchChunk]:
    """Unified search pipeline — FTS5, semantic, or hybrid.

    Args:
        q: Search query string.
        db_path: Path to main database.
        embed_db: Path to embeddings database. Required for hybrid/semantic modes.
        n: Desired result count after all processing.
        mode: "hybrid" (FTS5 recall narrows candidates, embeddings rerank), "fts"
            (keyword only), "semantic" (vector only).
        rerank: "mmr" for diversity reranking of the vector list, "relevance" for
            pure cosine order.
        embed_backend: Injected embedding backend instance. If provided, skips
            get_backend() discovery (config-driven otherwise). Must implement
            embed_query(text) -> list[float], and have .name, .model, .dimension attributes.

    Returns:
        List of SearchChunk results. In hybrid/semantic mode ``score`` is the cosine
        (or MMR-adjusted) score; in fts mode it is the bounded normalized bm25 score.

    Raises:
        FileNotFoundError: If database doesn't exist.
        EmbeddingTransientError: If the query embedding fails at runtime (the
            reachability class :func:`search_view` catches to degrade to fts).
        EmbeddingConfigError: If the [embed] config is unusable (never degraded).
    """
    from siftd.search import resolve_candidates
    from siftd.storage.sqlite import open_database

    # --- FTS-only mode ---
    if mode == "fts":
        candidate_ids = resolve_candidates(
            db_path,
            workspace=workspace, model=model, since=since, before=before,
            tag=tag, all_tags=all_tags, no_tag=no_tag, tag_kind=tag_kind,
            exclude_active=exclude_active, include_derivative=include_derivative,
            owner=owner, tool=tool, tool_tag=tool_tag,
        )
        conn = open_database(db_path, read_only=True)
        try:
            raw = fts5_search_content(conn, q, limit=n * 5, raw_fts=raw_fts)
            if candidate_ids is not None:
                raw = [r for r in raw if r["conversation_id"] in candidate_ids]
            raw = raw[:n]
            chunks = [
                SearchChunk(
                    conversation_id=r["conversation_id"],
                    score=_normalize_fts_score(r["rank"]),
                    text=r["snippet"],
                    chunk_type=r["kind"],
                    source_ids=[],
                    file_refs=[],
                    event_id=r.get("event_id"),
                )
                for r in raw
            ]
            if chunks:
                _annotate_turn_positions(conn, chunks)
            return chunks
        finally:
            conn.close()

    # --- Hybrid / semantic modes need embeddings ---
    from siftd.embeddings.indexer import SCHEMA_VERSION
    from siftd.paths import embeddings_db_path as default_embed_db
    from siftd.search import MAX_MMR_CANDIDATES

    effective_embed_db = embed_db or default_embed_db()
    _backend = _resolve_search_backend(embed_backend)
    if recall is None:
        recall = _preset_recall(_backend)

    candidate_ids = resolve_candidates(
        db_path,
        workspace=workspace, model=model, since=since, before=before,
        tag=tag, all_tags=all_tags, no_tag=no_tag, tag_kind=tag_kind,
        exclude_active=exclude_active, include_derivative=include_derivative,
        owner=owner, tool=tool, tool_tag=tool_tag,
    )
    if candidate_ids is not None and not candidate_ids:
        return []

    use_mmr = rerank == "mmr"
    # Query embed. A transient (429/5xx/network) failure propagates as
    # EmbeddingTransientError to search_view, which degrades this query to fts.
    query_embedding = _backend.embed_query(q)

    embed_conn = open_embeddings_db(effective_embed_db, read_only=True)
    try:
        validate_index_compat(
            embed_conn,
            backend_name=_backend.name,
            backend_model=_backend.model,
            backend_dimension=_backend.dimension,
            current_schema_version=SCHEMA_VERSION,
        )

        if mode == "semantic":
            results = _build_vector_list(
                embed_conn, query_embedding, candidate_ids,
                search_limit=max(n * 3, n) if use_mmr else n,
                mmr_limit=n, use_mmr=use_mmr, lambda_=lambda_,
                mmr_cap=MAX_MMR_CANDIDATES, recency=recency,
                recency_half_life=recency_half_life, recency_max_boost=recency_max_boost,
                db_path=db_path,
            )
        elif _hybrid_strategy(_backend) == "rrf":
            # Dedup-on-RRF engine — the 0.11.0 default on strong embedders (see
            # _hybrid_strategy). The vector list runs over the full candidate set (no FTS
            # narrowing) at a 300-deep pool and pure-relevance λ=1.0, fused with a
            # chunk-level keyword list, then the conversation-dedup rollup projects the
            # fused chunk ranking onto n distinct conversations.
            pool = _RRF_POOL
            vector_results = _build_vector_list(
                embed_conn, query_embedding, candidate_ids,
                search_limit=pool, mmr_limit=pool, use_mmr=use_mmr, lambda_=_RRF_LAMBDA,
                mmr_cap=MAX_MMR_CANDIDATES, recency=recency,
                recency_half_life=recency_half_life, recency_max_boost=recency_max_boost,
                db_path=db_path,
            )
            kw_conn = open_database(db_path, read_only=True)
            try:
                keyword_hits = fts5_search_content(kw_conn, q, limit=max(pool, n), raw_fts=raw_fts)
            finally:
                kw_conn.close()
            if candidate_ids is not None:
                keyword_hits = [h for h in keyword_hits if h["conversation_id"] in candidate_ids]
            results = _fuse_hybrid(vector_results, keyword_hits, embed_conn, n=n)
        else:
            # Default hybrid: narrow-then-rank (FTS5 recall narrows candidates,
            # embeddings rerank). Kept the default by the F3 bench ruling — see
            # _hybrid_strategy for why RRF stays dormant.
            results = _narrow_then_rank(
                q, embed_conn, query_embedding, candidate_ids, db_path,
                n=n, recall=recall, raw_fts=raw_fts, use_mmr=use_mmr, lambda_=lambda_,
                mmr_cap=MAX_MMR_CANDIDATES, recency=recency,
                recency_half_life=recency_half_life, recency_max_boost=recency_max_boost,
            )
    finally:
        embed_conn.close()

    if not results:
        return []

    # Score threshold — rewired to relevance (cosine for vector chunks, score for
    # fts, entrants exempt), consistent with the recipe's filter_by_threshold.
    if threshold > 0:
        results = [
            r for r in results
            if _passes_relevance_threshold(r.get("score", 0.0), r.get("breakdown"), threshold)
        ]

    final_chunks = [SearchChunk.from_mapping(r) for r in results]
    if final_chunks:
        _pos_conn = open_database(db_path, read_only=True)
        try:
            _annotate_turn_positions(_pos_conn, final_chunks)
        finally:
            _pos_conn.close()
    return final_chunks


def _hybrid_strategy(backend: EmbeddingBackend) -> str:
    """Return the hybrid strategy for ``backend``: ``"rrf"`` or ``"narrow"``.

    Precedence: ``SIFTD_HYBRID_STRATEGY`` (env) overrides, else the embedder's preset
    default. The env knob stays an EXPERIMENT-ONLY escape hatch — deliberately absent
    from ``--help``, config, and docs — for pinning a strategy across arms (the bench and
    the fidelity gate rely on it); ``=rrf``/``=narrow`` force that engine, anything else
    is ignored.

    The shipped default is per-preset (0.11.0 bench, decision #4 resolved): strong
    embedders (voyage validated, +0.0129 composite over narrow) get dedup-on-RRF; weak/
    local ones keep narrow-then-rank (it beat every RRF config on the bge arm). Picking a
    provider picks the behavior — there is no user-facing strategy knob."""
    import os

    env = os.environ.get("SIFTD_HYBRID_STRATEGY")
    if env in ("rrf", "narrow"):
        return env

    from siftd.embeddings.presets import hybrid_defaults_for_backend

    return hybrid_defaults_for_backend(getattr(backend, "name", "") or "").strategy


def _preset_recall(backend: EmbeddingBackend) -> int:
    """The narrow-path FTS candidate width for ``backend``'s preset (global default 40)."""
    from siftd.embeddings.presets import hybrid_defaults_for_backend

    return hybrid_defaults_for_backend(getattr(backend, "name", "") or "").recall


def _narrow_then_rank(
    q: str,
    embed_conn: sqlite3.Connection,
    query_embedding: list[float],
    candidate_ids: set[str] | None,
    db_path: Path,
    *,
    n: int,
    recall: int,
    raw_fts: bool,
    use_mmr: bool,
    lambda_: float,
    mmr_cap: int,
    recency: bool,
    recency_half_life: float,
    recency_max_boost: float,
) -> list[dict]:
    """Default hybrid engine: FTS5 recall narrows candidates, embeddings rerank.

    The F3 bench kept this the default over RRF (see _hybrid_strategy). Faithful to
    the shipped v1 behavior."""
    from siftd.search import annotate_fts5_breakdown
    from siftd.storage.sqlite import open_database

    conn = open_database(db_path, read_only=True)
    try:
        fts5_ids, fts5_mode = fts5_recall_conversations(conn, q, limit=recall, raw_fts=raw_fts)
    finally:
        conn.close()
    narrowed = candidate_ids
    if fts5_ids:
        if narrowed is not None:
            intersected = fts5_ids & narrowed
            narrowed = intersected if intersected else narrowed
        else:
            narrowed = fts5_ids
    if narrowed is not None and not narrowed:
        return []

    results = _build_vector_list(
        embed_conn, query_embedding, narrowed,
        search_limit=max(n * 3, n) if use_mmr else n,
        mmr_limit=n, use_mmr=use_mmr, lambda_=lambda_, mmr_cap=mmr_cap,
        recency=recency, recency_half_life=recency_half_life,
        recency_max_boost=recency_max_boost, db_path=db_path,
    )
    annotate_fts5_breakdown(results, fts5_ids, fts5_mode)
    return results


# ---------------------------------------------------------------------------
# search_view — engine + recipe composed into one Operation result
# ---------------------------------------------------------------------------

SEARCH_VIEWS = ("chunks", "thread", "conversations")
SEARCH_SORTS = ("score", "time")
SEARCH_SELECTS = ("all", "first")


def parse_turns_range(s: str) -> tuple[int, int]:
    """Parse a turns-range string like ``-2:+2`` or ``5:10`` into (start, end).

    The neutral, layer-agnostic parser (raises :class:`ValueError`) used by
    :func:`search_view`, so the ``--around`` window validates the same way on
    the CLI, the REST route (``ValueError`` → 400), and any programmatic caller.
    The CLI keeps its own ``_parse_turns_range`` wrapper that maps the same
    failures to ``sys.exit(2)`` for a friendly argparse-style message.
    """
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"turns must be in A:B format (e.g. -2:+2, 5:10), got: {s!r}")
    try:
        start = int(parts[0].lstrip("+"))
        end = int(parts[1].lstrip("+"))
    except ValueError as e:
        raise ValueError(f"turns values must be integers, got: {s!r}") from e
    if end < start:
        raise ValueError(f"turns end ({end}) must be >= start ({start})")
    return start, end


def _validate_view_axes(view: str, sort: str, select: str, around: str | None, turns: str | None) -> None:
    """Validate the view/sort/select axis combination + the around/turns pairing.

    Raises :class:`ValueError` (→ CLI error / REST 400) so the axis rules can't
    drift between surfaces. The CLI keeps a thin early pre-check for the friendly
    ``exit(2)`` UX; this is the canonical gate every surface inherits.
    """
    if view not in SEARCH_VIEWS:
        raise ValueError(f"invalid view {view!r}; choose from {', '.join(SEARCH_VIEWS)}")
    if sort not in SEARCH_SORTS:
        raise ValueError(f"invalid sort {sort!r}; choose from {', '.join(SEARCH_SORTS)}")
    if select not in SEARCH_SELECTS:
        raise ValueError(f"invalid select {select!r}; choose from {', '.join(SEARCH_SELECTS)}")
    if view in ("thread", "conversations") and sort == "time":
        raise ValueError(
            f"view={view} is incompatible with sort=time ({view} imposes its own ordering)"
        )
    if turns is not None and around is None:
        raise ValueError("turns requires around=PHRASE")


def _engine_limit(n: int, *, view: str, select: str) -> int:
    """Widen the engine candidate pool for views that aggregate or filter post-hoc.

    Callers pass the *final* result count ``n``; the aggregate/thread/first
    shapes need a wider engine pool to draw from before trimming. This is the
    widening that used to live inline in the CLI handler — homed here so the
    REST route and HTML view inherit identical pool sizing.
    """
    if view == "thread":
        return max(n, 40)
    if select == "first" or view == "conversations":
        return max(n * 10, 100)
    return n


# Event-anchored kinds enumerated by the element arm; 'block' rides its own arm
# (its target is an event_content.id, not an events.id).
_ENUM_ELEMENT_KINDS = tuple(sorted(EVENT_TAG_KINDS))


def _enum_tag_facet_where(
    tag: list[str] | None, all_tags: list[str] | None
) -> tuple[list[str], list[object]]:
    """WHERE fragments matching the outer ``ta.target_id`` against the tag facet.

    ``tag`` is OR, ``all_tags`` is AND. Each fragment is a self-contained
    ``ta.target_id IN (subquery)`` so it composes with any outer target-kind
    filter (element rows or conversation rows) without an outer tags join.
    """
    from siftd.storage.filters import tag_condition

    frags: list[str] = []
    params: list[object] = []
    if tag:
        ors: list[str] = []
        for t in tag:
            clause, val = tag_condition(t)
            ors.append(f"({clause})")
            params.append(val)
        frags.append(
            "ta.target_id IN (SELECT s.target_id FROM tag_assignments s "
            "JOIN tags tg ON tg.id = s.tag_id "
            f"WHERE {' OR '.join(ors)})"
        )
    if all_tags:
        for t in all_tags:
            clause, val = tag_condition(t)
            frags.append(
                "ta.target_id IN (SELECT s.target_id FROM tag_assignments s "
                f"JOIN tags tg ON tg.id = s.tag_id WHERE {clause})"
            )
            params.append(val)
    return frags, params


def enumerate_tagged(
    *,
    db_path: Path,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    tag_kind: list[str] | None = None,
    workspace: str | None = None,
    since: str | None = None,
    before: str | None = None,
    owner: str | None = None,
    n: int = 10,
    view: str = "chunks",
) -> SearchView:
    """Enumerate tagged targets without ranking — the filter-only search path.

    Selects ``tag_assignments`` matching the tag facet (``tag`` = OR, ``all_tags``
    = AND), honoring the other facets (workspace, date range, owner),
    recency-ordered. Per decision 1: element-kind matches surface as element
    hits, conversation-kind matches surface as conversation-scoped hits. A tag
    that matches both (or ``view="conversations"``) resolves to the conversations
    shape — the element-owning conversations unioned with the directly-tagged
    ones — so a conversation-level tag (the common case for a pre-existing
    corpus) never returns silently empty.
    """
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate
    from siftd.storage.sqlite import open_database

    if not (tag or all_tags):
        return SearchView(results=[], view=view)

    element_kinds = tuple(
        k for k in (tag_kind or _ENUM_ELEMENT_KINDS) if k in _ENUM_ELEMENT_KINDS
    )
    want_conversation = tag_kind is None or "conversation" in tag_kind
    want_block = tag_kind is None or "block" in tag_kind

    conn = open_database(db_path, read_only=True)
    try:
        if owner and not has_conversation_owners_table(conn):
            return SearchView(results=[], view=view)

        # --- element-kind matches → element chunks ---
        chunks: list[SearchChunk] = []
        block_chunk_ids: list[tuple[SearchChunk, str]] = []
        if element_kinds:
            frags, params = _enum_tag_facet_where(tag, all_tags)
            where = [f"ta.target_kind IN ({','.join('?' * len(element_kinds))})", *frags]
            eparams: list[object] = [*element_kinds, *params]
            if workspace:
                where.append("w.path LIKE ?")
                eparams.append(f"%{workspace}%")
            if since:
                where.append("e.timestamp >= ?")
                eparams.append(since)
            if before:
                where.append("e.timestamp < ?")
                eparams.append(before)
            if owner:
                where.append(owner_predicate("c.id"))
                eparams.append(owner)
            rows = conn.execute(
                "SELECT DISTINCT ta.target_kind, ta.target_id, e.conversation_id, "
                "e.timestamp AS ev_ts, c.started_at, w.path AS workspace "
                "FROM tag_assignments ta "
                "JOIN events e ON e.id = ta.target_id "
                "JOIN conversations c ON c.id = e.conversation_id "
                "LEFT JOIN workspaces w ON w.id = c.workspace_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY e.timestamp DESC, e.id DESC LIMIT ?",
                (*eparams, n),
            ).fetchall()
            for row in rows:
                excerpt_row = conn.execute(
                    "SELECT json_extract(content, '$.text') AS text FROM event_content "
                    "WHERE event_id = ? AND json_extract(content, '$.text') IS NOT NULL "
                    "ORDER BY block_index LIMIT 1",
                    (row["target_id"],),
                ).fetchone()
                started = row["started_at"]
                chunks.append(
                    SearchChunk(
                        conversation_id=row["conversation_id"],
                        score=0.0,
                        text=(excerpt_row["text"] if excerpt_row else "") or "",
                        chunk_type=row["target_kind"],
                        workspace_path=_workspace_label(row["workspace"]),
                        started_at=(started or "")[:10] if started else None,
                        event_id=row["target_id"],
                    )
                )

        # --- block-kind matches → content-block chunks ---
        # target_id is an event_content.id, so the join descends
        # event_content → events → conversations (distinct from the event arm).
        if want_block:
            frags, params = _enum_tag_facet_where(tag, all_tags)
            where = ["ta.target_kind = 'block'", *frags]
            bparams: list[object] = [*params]
            if workspace:
                where.append("w.path LIKE ?")
                bparams.append(f"%{workspace}%")
            if since:
                where.append("e.timestamp >= ?")
                bparams.append(since)
            if before:
                where.append("e.timestamp < ?")
                bparams.append(before)
            if owner:
                where.append(owner_predicate("c.id"))
                bparams.append(owner)
            rows = conn.execute(
                "SELECT DISTINCT ta.target_id AS block_id, ec.block_type, "
                "ec.event_id, e.conversation_id, e.timestamp AS ev_ts, "
                "c.started_at, w.path AS workspace, "
                "json_extract(ec.content, '$.text') AS text "
                "FROM tag_assignments ta "
                "JOIN event_content ec ON ec.id = ta.target_id "
                "JOIN events e ON e.id = ec.event_id "
                "JOIN conversations c ON c.id = e.conversation_id "
                "LEFT JOIN workspaces w ON w.id = c.workspace_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY e.timestamp DESC, ec.id DESC LIMIT ?",
                (*bparams, n),
            ).fetchall()
            for row in rows:
                started = row["started_at"]
                chunk = SearchChunk(
                    conversation_id=row["conversation_id"],
                    score=0.0,
                    text=(row["text"] or ""),
                    chunk_type=row["block_type"],
                    workspace_path=_workspace_label(row["workspace"]),
                    started_at=(started or "")[:10] if started else None,
                    event_id=row["event_id"],
                )
                chunks.append(chunk)
                # A block chunk's event_id is the OWNING event (the folio-jump
                # address), but its tags live on the block id — so enrich_tags'
                # event-keyed pass can't reach them (and could leak the owning
                # event's own tags). Remember (chunk, block_id) to set chips
                # directly below, overriding that pass.
                block_chunk_ids.append((chunk, row["block_id"]))

        if chunks:
            enrich_tags(conn, chunks)
            _annotate_turn_positions(conn, chunks)
            _enrich_block_tags(conn, block_chunk_ids)

        # --- conversation-kind matches → directly-tagged conversation rows ---
        conv_rows: list = []
        if want_conversation:
            frags, params = _enum_tag_facet_where(tag, all_tags)
            where = ["ta.target_kind = 'conversation'", *frags]
            cparams: list[object] = [*params]
            if workspace:
                where.append("w.path LIKE ?")
                cparams.append(f"%{workspace}%")
            if since:
                where.append("c.started_at >= ?")
                cparams.append(since)
            if before:
                where.append("c.started_at < ?")
                cparams.append(before)
            if owner:
                where.append(owner_predicate("c.id"))
                cparams.append(owner)
            conv_rows = conn.execute(
                "SELECT DISTINCT ta.target_id AS conv_id, c.started_at, w.path AS workspace "
                "FROM tag_assignments ta "
                "JOIN conversations c ON c.id = ta.target_id "
                "LEFT JOIN workspaces w ON w.id = c.workspace_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY c.started_at DESC LIMIT ?",
                (*cparams, n),
            ).fetchall()

        # No conversation-kind matches and not explicitly asked for the
        # conversations shape → element hits.
        if view != "conversations" and not conv_rows:
            return SearchView(results=[c.to_render_dict() for c in chunks[:n]], view="chunks")

        # Conversations shape: element-owning conversations (aggregated) unioned
        # with the directly-tagged ones, deduped, recency-ordered.
        conv_hits: dict[str, dict] = {}
        for summ in aggregate_by_conversation(chunks, limit=n):
            d = summ.to_render_dict()
            conv_hits[d["conversation_id"]] = d
        for row in conv_rows:
            cid = row["conv_id"]
            if cid in conv_hits:
                continue
            excerpt = conn.execute(
                "SELECT json_extract(ec.content, '$.text') AS text FROM event_content ec "
                "JOIN events e ON e.id = ec.event_id "
                "WHERE e.conversation_id = ? AND e.kind = 'prompt' "
                "AND json_extract(ec.content, '$.text') IS NOT NULL "
                "ORDER BY e.timestamp LIMIT 1",
                (cid,),
            ).fetchone()
            started = row["started_at"]
            conv_hits[cid] = {
                "conversation_id": cid,
                "max_score": 0.0,
                "mean_score": 0.0,
                "chunk_count": 0,
                "best_excerpt": (excerpt["text"] if excerpt else "") or "",
                "_workspace": _workspace_label(row["workspace"]) or "",
                "_started_at": (started or "")[:10] if started else "",
                "file_refs": [],
            }
        merged = sorted(
            conv_hits.values(), key=lambda d: d.get("_started_at", ""), reverse=True
        )[:n]
        return SearchView(results=merged, view="conversations")
    finally:
        conn.close()


def search_view(
    q: str,
    *,
    db_path: Path,
    embed_db: Path | None = None,
    n: int = 10,
    mode: str = "hybrid",
    # Filters
    workspace: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    tag: list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    exclude_active: bool = True,
    include_derivative: bool = False,
    owner: str | None = None,
    tool: str | None = None,
    tool_tag: str | None = None,
    # Engine tuning. recall=None ⇒ the shipped default (40; see NARROW_RECALL_DEFAULT).
    recall: int | None = None,
    rerank: str = "mmr",
    lambda_: float = 0.7,
    recency: bool = False,
    recency_half_life: float = 30.0,
    recency_max_boost: float = 1.15,
    embed_backend: EmbeddingBackend | None = None,
    raw_fts: bool = False,
    # Recipe (post-processing) controls
    view: str = "chunks",
    sort: str = "score",
    select: str = "all",
    threshold: float | None = None,
    full: bool = False,
    around: str | None = None,
    turns: str | None = None,
) -> SearchView:
    """The whole search Operation: engine retrieval + the post-processing recipe.

    Composes :func:`search_chunks` (the engine) with
    :func:`process_search_view` (threshold → select → trim → enrich → sort →
    view shape → full → around) into one render-ready :class:`SearchView`, so
    every surface — the CLI, the REST ``/api/v1/search`` route, and the HTML
    Find view — runs the *same* recipe and the wire carries the post-processed
    result rather than raw chunks. The engine candidate pool is widened
    internally for the aggregate/thread/first views (callers pass the final
    ``n``); axis combinations and the ``turns`` window validate here
    (:class:`ValueError` → CLI error / REST 400) so the rules can't drift.

    ``threshold`` is the client-side post-filter (the CLI's ``--threshold``);
    the engine-side score threshold stays at its default. An empty engine result
    short-circuits to an empty chunks/thread/conversations view (``empty_reason``
    stays ``None``) so the "no results" message is distinct from a deliberately
    emptied one.
    """
    _validate_view_axes(view, sort, select, around, turns)

    # Filter-only search: no query, but a tag facet → enumerate tagged elements
    # (recency-ordered), skipping the FTS/vector engines entirely (decision 1).
    if not q.strip() and (tag or all_tags):
        return enumerate_tagged(
            db_path=db_path,
            tag=tag,
            all_tags=all_tags,
            tag_kind=tag_kind,
            workspace=workspace,
            since=since,
            before=before,
            owner=owner,
            n=n,
            view=view if view in ("chunks", "conversations") else "chunks",
        )

    turns_range = (
        parse_turns_range(turns) if (around is not None and turns is not None) else None
    )

    def _run_engine(engine_mode: str) -> list[SearchChunk]:
        return search_chunks(
            q,
            db_path=db_path,
            embed_db=embed_db,
            n=_engine_limit(n, view=view, select=select),
            mode=engine_mode,
            workspace=workspace,
            model=model,
            since=since,
            before=before,
            tag=tag,
            all_tags=all_tags,
            no_tag=no_tag,
            tag_kind=tag_kind,
            exclude_active=exclude_active,
            include_derivative=include_derivative,
            owner=owner,
            tool=tool,
            tool_tag=tool_tag,
            recall=recall,
            rerank=rerank,
            lambda_=lambda_,
            recency=recency,
            recency_half_life=recency_half_life,
            recency_max_boost=recency_max_boost,
            embed_backend=embed_backend,
            raw_fts=raw_fts,
        )

    # §5 truthful degrade: a runtime query-embed failure (backend configured but
    # unreachable — 429/5xx/network) degrades THIS query to keyword search and
    # re-derives the reported engine. Config errors (EmbeddingConfigError) are not
    # caught here — they raise. This is the single seam all surfaces inherit (CLI
    # local, REST route, HTML pane), so ``executed_mode`` on the SearchView is the
    # owner-safe channel that reports what actually ran.
    from siftd.embeddings.base import EmbeddingTransientError

    executed_mode = mode
    try:
        chunks = _run_engine(mode)
    except EmbeddingTransientError:
        if mode == "fts":
            raise  # fts never embeds — not our failure to swallow
        executed_mode = "fts"
        chunks = _run_engine("fts")

    degraded = executed_mode if executed_mode != mode else None

    if not chunks:
        return SearchView(results=[], view=view, executed_mode=degraded)

    from siftd.storage.sqlite import open_database

    conn = open_database(db_path, read_only=True)
    try:
        sv = process_search_view(
            chunks,
            conn,
            view=view,
            sort=sort,
            select=select,
            threshold=threshold,
            limit=n,
            full=full,
            around=around,
            turns_range=turns_range,
            db_path=db_path,
        )
    finally:
        conn.close()
    if degraded is not None:
        import dataclasses

        sv = dataclasses.replace(sv, executed_mode=degraded)
    return sv
