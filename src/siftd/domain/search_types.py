"""Shared search-domain types used across API/search/storage/output layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

# Conversational role presentation labels — canonical across api.Turn and output renderers.
ROLE_USER: str = "user"
ROLE_ASSISTANT: str = "assistant"


@dataclass
class ScoreBreakdown:
    """Detailed score components for explainability."""

    embedding_sim: float
    recency_boost: float = 1.0
    pre_mmr_score: float | None = None
    mmr_penalty: float | None = None
    mmr_rank: int | None = None
    final_score: float | None = None
    fts5_matched: bool = False
    fts5_mode: str | None = None

    def __post_init__(self) -> None:
        if self.pre_mmr_score is None:
            self.pre_mmr_score = self.embedding_sim * self.recency_boost
        if self.final_score is None:
            self.final_score = self.pre_mmr_score

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ScoreBreakdown:
        """Create a breakdown from a mapping object."""
        return cls(
            embedding_sim=float(data.get("embedding_sim", 0.0)),
            recency_boost=float(data.get("recency_boost", 1.0)),
            pre_mmr_score=data.get("pre_mmr_score"),
            mmr_penalty=data.get("mmr_penalty"),
            mmr_rank=data.get("mmr_rank"),
            final_score=data.get("final_score"),
            fts5_matched=bool(data.get("fts5_matched", False)),
            fts5_mode=data.get("fts5_mode"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-safe dictionary."""
        return {
            "embedding_sim": round(self.embedding_sim, 4),
            "recency_boost": round(self.recency_boost, 4),
            "pre_mmr_score": round(self.pre_mmr_score, 4) if self.pre_mmr_score is not None else None,
            "mmr_penalty": round(self.mmr_penalty, 4) if self.mmr_penalty is not None else None,
            "mmr_rank": self.mmr_rank,
            "final_score": round(self.final_score, 4) if self.final_score is not None else None,
            "fts5_matched": self.fts5_matched,
            "fts5_mode": self.fts5_mode,
        }


@dataclass
class SearchChunk:
    """Canonical mutable search chunk result."""

    conversation_id: str
    score: float
    text: str
    chunk_type: str
    workspace_path: str | None = None
    started_at: str | None = None
    chunk_id: str | None = None
    source_ids: list[str] = field(default_factory=list)
    breakdown: ScoreBreakdown | None = None
    file_refs: list[Any] | None = None
    exchanges: list[tuple[str, str, str]] | None = None
    context_window: list[tuple[str, str, str, bool]] | None = None
    turn_index: int | None = None
    event_id: str | None = None

    _DISPLAY_LABELS: ClassVar[dict[str, str]] = {
        "prompt": "USER",
        "response": "ASSISTANT",
        "tool_call": "TOOL",
        "exchange": "EXCHANGE",
        "tool_summary": "SUMMARY",
    }

    def __post_init__(self) -> None:
        if self.source_ids is None:
            self.source_ids = []

    @property
    def display_label(self) -> str:
        """Presentation label derived from chunk_type; stable across renderers."""
        return self._DISPLAY_LABELS.get(self.chunk_type, self.chunk_type.upper())

    def __getitem__(self, key: str) -> Any:
        """Compatibility access for legacy dict-style callers."""
        if key == "_workspace":
            return self.workspace_path or ""
        if key == "_started_at":
            return (self.started_at or "")[:10] if self.started_at else ""
        if key == "_exchanges":
            return self.exchanges
        if key == "_context":
            return self.context_window
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Compatibility helper mirroring dict.get()."""
        try:
            return self[key]
        except KeyError:
            return default

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SearchChunk:
        """Create a SearchChunk from an arbitrary mapping."""
        raw_breakdown = data.get("breakdown")
        breakdown: ScoreBreakdown | None = None
        if isinstance(raw_breakdown, ScoreBreakdown):
            breakdown = raw_breakdown
        elif isinstance(raw_breakdown, Mapping):
            breakdown = ScoreBreakdown.from_mapping(raw_breakdown)

        source_ids = data.get("source_ids") or []
        if not isinstance(source_ids, list):
            source_ids = list(source_ids)

        raw_turn_index = data.get("turn_index")
        turn_index: int | None = int(raw_turn_index) if raw_turn_index is not None else None
        return cls(
            conversation_id=str(data.get("conversation_id", "")),
            score=float(data.get("score", 0.0)),
            text=str(data.get("text", "")),
            chunk_type=str(data.get("chunk_type", "")),
            workspace_path=data.get("workspace_path") or data.get("_workspace"),
            started_at=data.get("started_at") or data.get("_started_at"),
            chunk_id=data.get("chunk_id"),
            source_ids=source_ids,
            breakdown=breakdown,
            file_refs=data.get("file_refs"),
            exchanges=data.get("exchanges") or data.get("_exchanges"),
            context_window=data.get("context_window") or data.get("_context"),
            turn_index=turn_index,
            event_id=data.get("event_id"),
        )

    def to_render_dict(self, debug_ids: bool = True) -> dict[str, Any]:
        """Convert to the legacy dict shape expected by current formatters.

        chunk_id/source_ids are emitted by default. The debug_ids kwarg is
        accepted for backward compatibility through v0.9.x and removed in v0.10.0.
        """
        out: dict[str, Any] = {
            "conversation_id": self.conversation_id,
            "score": self.score,
            "chunk_type": self.chunk_type,
            "display_label": self.display_label,
            "text": self.text,
            "_workspace": self.workspace_path or "",
            "_started_at": (self.started_at or "")[:10] if self.started_at else "",
        }
        if debug_ids:
            out["chunk_id"] = self.chunk_id
            out["source_ids"] = self.source_ids
        out["turn_index"] = self.turn_index
        if self.event_id is not None:
            out["event_id"] = self.event_id
        if self.breakdown is not None:
            out["breakdown"] = self.breakdown
        if self.file_refs is not None:
            out["file_refs"] = self.file_refs
        if self.exchanges is not None:
            out["_exchanges"] = self.exchanges
        if self.context_window is not None:
            out["_context"] = self.context_window
        return out


@dataclass
class ConversationSearchSummary:
    """Conversation-level aggregate derived from chunk results."""

    conversation_id: str
    max_score: float
    mean_score: float
    chunk_count: int
    best_excerpt: str
    workspace_path: str | None = None
    started_at: str | None = None
    file_refs: list[Any] | None = None

    def to_render_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "max_score": self.max_score,
            "mean_score": self.mean_score,
            "chunk_count": self.chunk_count,
            "best_excerpt": self.best_excerpt,
            "_workspace": self.workspace_path or "",
            "_started_at": (self.started_at or "")[:10] if self.started_at else "",
            "file_refs": self.file_refs or [],
        }


@dataclass
class ThreadTiers:
    """Tiered chunk groups for thread mode output."""

    tier1: list[SearchChunk]
    tier2: list[SearchChunk]


@dataclass(frozen=True)
class SearchView:
    """Post-processed, render-ready search output — the recipe's single product.

    ``results`` is what a formatter consumes as its positional argument, in
    render-dict shape: chunk dicts for the ``chunks``/``thread`` views,
    per-conversation dicts for ``conversations``. ``tier1``/``tier2`` carry the
    thread split (``None`` outside the thread view; the thread renderers read
    *them*, not ``results``). ``n_skipped`` counts results dropped by the
    ``--around`` phrase filter. ``empty_reason`` distinguishes a deliberately
    emptied result (``"threshold"`` / ``"first"``) from an ordinary empty one,
    so a caller can phrase the right message; it is never set while ``results``
    is non-empty.

    It lives in ``domain`` (not ``api``) so every layer that renders or
    serializes a search — ``output`` formatters, the ``serialization`` serve
    formatter, and the ``api`` deserializer — can construct one without
    crossing an architecture boundary. ``api.search.process_search_view`` (the
    DB-touching recipe that produces it) re-exports it for back-compat.
    """

    results: list[dict[str, Any]]
    view: str
    tier1: list[dict[str, Any]] | None = None
    tier2: list[dict[str, Any]] | None = None
    n_skipped: int = 0
    empty_reason: str | None = None


def as_search_view(result: Any, *, view: str = "chunks") -> SearchView:
    """Normalize a renderer's positional argument to a :class:`SearchView`.

    The canonical search render contract passes a ``SearchView``; this shim
    wraps a bare list of render-dicts into a chunks-shaped view so the empty-
    result helper and any list-passing caller keep working. A value that is
    already a ``SearchView`` (duck-typed on ``.results``) passes through
    unchanged.
    """
    if hasattr(result, "results") and hasattr(result, "view"):
        return result
    return SearchView(results=list(result or []), view=view)
