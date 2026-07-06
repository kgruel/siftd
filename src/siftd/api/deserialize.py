"""Deserializers — inverse of the wire-form serializers.

When the CLI delegates a read to siftd-serve, the server returns a JSON dict
serialized via ``serve_fmt.render_*`` (which in turn uses
``serialize_conversation_*`` and friends in :mod:`siftd.serialization`). To
make the delegated path *indistinguishable* from local execution for
downstream renderers, the CLI needs to reconstruct the typed objects
(``ConversationDetail``, etc.) from the dict.

These functions are the inverse of the serializers in
:mod:`siftd.serialization`. The pair is the response-side half of the
operation-has-local-form-and-wire-form pattern (the request-side half is
:meth:`siftd.api.dispatch.Operation.to_local` /
:meth:`siftd.api.dispatch.Operation.to_wire`, with per-op rules in
:mod:`siftd.api.op_spec`). See ``docs/guides/delegation-contract.md``.

This module lives under ``api/`` (not ``serialization/``) because it must
construct api-layer dataclasses at runtime; the architectural rule is
one-way ``api → serialization``, never the reverse.

Round-trip property: for any value ``v`` produced by the local API fn,

    deserialize(serialize(v)) ≈ v

where ``≈`` means "rendered output is byte-identical for the same fidelity."
Strict structural equality is not always possible because some serializers
collapse split fields (e.g. ``total_input_tokens + total_output_tokens`` →
``total_tokens``); the deserializer reconstructs the splits by summing
per-turn data where available, falling back to ``(total_tokens, 0)`` when
no turns are present.
"""

from __future__ import annotations

from typing import Any

from siftd.api.conversations import (
    ConversationDetail,
    ConversationSummary,
    NarrativeBlock,
    ToolCallDetail,
    Turn,
)
from siftd.api.export import ExportArtifact

# ---------------------------------------------------------------------------
# Conversation list / detail
# ---------------------------------------------------------------------------


def _coerce_int(value: Any, default: int = 0) -> int:
    """Coerce a wire value to int; return default on any failure.

    Wire bodies can carry malformed field-level types even when the structural
    shape is correct (e.g. a server bug emitting ``"tokens": "bad"`` for what
    should be an int). The deserializer contract says we return ``None`` on
    schema mismatch rather than raising — see
    ``docs/guides/delegation-contract.md`` rule #8. This helper enforces that
    at the field level so deserializers don't need their own try/except.
    """
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_cost(value: Any) -> float | None:
    """Coerce a wire value to float, preserving ``None``.

    Unlike ``_coerce_int``, ``None`` is meaningful here — it means "no priced
    usage" (distinct from a real 0.0), so it must survive the round-trip rather
    than collapse to a default. Malformed non-null values fall back to ``None``.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_list(value: Any) -> list:
    """Coerce a wire value to a list; return [] on any failure."""
    if isinstance(value, list):
        return value
    if value is None:
        return []
    # Some servers may emit a single scalar where a list is expected. Don't
    # try to coerce — return empty so the caller treats it as missing.
    return []


def deserialize_conversation_summary(d: dict[str, Any]) -> ConversationSummary | None:
    """Inverse of ``serialize_conversation_summary``.

    Returns ``None`` if the input isn't a dict or lacks an ``id`` — the same
    schema-mismatch tolerance as :func:`deserialize_conversation_detail`.
    Any field-level type mismatch (e.g. non-numeric ``prompts``) is absorbed
    by ``_coerce_int`` / ``_coerce_list`` rather than raising.
    """
    if not isinstance(d, dict) or "id" not in d:
        return None
    try:
        return ConversationSummary(
            id=d["id"],
            workspace_path=d.get("workspace"),
            model=d.get("model"),
            started_at=d.get("started_at"),
            prompt_count=_coerce_int(d.get("prompts")),
            response_count=_coerce_int(d.get("responses")),
            total_tokens=_coerce_int(d.get("tokens")),
            cost=d.get("cost"),
            tags=_coerce_list(d.get("tags")),
            owner=d.get("owner"),
        )
    except Exception:
        # Any other unexpected failure (dataclass validation, etc.) — fall back.
        return None


def deserialize_conversation_list(body: dict[str, Any]) -> list[ConversationSummary] | None:
    """Deserialize the body of ``GET /api/v1/conversations``.

    Body shape: ``{"conversations": [<summary dict>, ...]}``. Returns:

    - ``None`` on schema mismatch (body isn't a dict, or has no
      ``"conversations"`` key, or that key isn't a list). The caller treats
      ``None`` as "fall back to local execute."
    - ``[]`` for a legitimately empty list (server has no matching
      conversations) — a valid result, not a fallback signal.

    Malformed entries within an otherwise-valid list are silently skipped.
    """
    if not isinstance(body, dict):
        return None
    items = body.get("conversations")
    if not isinstance(items, list):
        return None
    out: list[ConversationSummary] = []
    for d in items:
        summary = deserialize_conversation_summary(d)
        if summary is not None:
            out.append(summary)
    return out


def deserialize_narrative_block(d: dict[str, Any]) -> NarrativeBlock | None:
    """Convert one emitted narrative dict back to a NarrativeBlock.

    Maps the JsonEmitter output (see ``serialization/narrative.py``) back to
    the in-memory dataclass shape produced by ``api.conversations._build_narrative``.

    Returns ``None`` for block types we don't recognize, which the caller
    should skip rather than raise — the wire vocabulary may grow over time
    and we want delegated reads to degrade gracefully against newer servers.
    """
    block_type = d.get("type")
    event_id = d.get("event_id")

    if block_type in ("text", "thinking"):
        return NarrativeBlock(
            block_type=block_type,
            content=d.get("content"),
            event_id=event_id,
        )

    if block_type in ("tool_result", "tool_output"):
        return NarrativeBlock(
            block_type=block_type,
            content=d.get("content"),
            event_id=event_id,
        )

    if block_type == "tool_calls":
        # Aggregated/collapsed view from JsonEmitter.tool_summary.
        # Per-tool entries have at minimum {name, count}; status is optional.
        # Malformed entries (non-dict) are skipped; field-level type errors
        # absorbed by _coerce_int.
        tools = _coerce_list(d.get("tools"))
        tool_calls = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            tool_calls.append(ToolCallDetail(
                tool_name=t.get("name", "") or "",
                status=t.get("status", "") or "",
                count=_coerce_int(t.get("count"), default=1),
            ))
        return NarrativeBlock(
            block_type="tool_calls",
            tool_calls=tool_calls,
            event_id=event_id,
        )

    if block_type == "tool_call":
        # Expanded per-call view from JsonEmitter.tool_content. Wrap in a
        # single-element tool_calls NarrativeBlock so the renderer sees the
        # same shape it would from a local fetch (where consecutive
        # tool_call events get collapsed into one block).
        return NarrativeBlock(
            block_type="tool_calls",
            tool_calls=[
                ToolCallDetail(
                    tool_name=d.get("name", "") or "",
                    status=d.get("status", "") or "",
                    count=_coerce_int(d.get("count"), default=1),
                    input=d.get("input"),
                    result=d.get("result"),
                    tool_call_id=d.get("tool_call_id"),
                )
            ],
            event_id=event_id,
        )

    return None


def _coalesce_adjacent_tool_calls(blocks: list[NarrativeBlock]) -> list[NarrativeBlock]:
    """Merge adjacent ``block_type="tool_calls"`` blocks into one.

    The local fetch path produces one ``tool_calls`` block per response with
    *all* of that response's tool calls inside. The wire emits one block per
    tool call when ``include_tool_content=True`` (each tool_call dict from
    JsonEmitter.tool_content). Coalescing on deserialize restores the
    local-form shape so renderers don't need to know which path produced
    their input.
    """
    out: list[NarrativeBlock] = []
    for b in blocks:
        if (
            b.block_type == "tool_calls"
            and out
            and out[-1].block_type == "tool_calls"
            and out[-1].event_id == b.event_id
        ):
            out[-1].tool_calls.extend(b.tool_calls)
        else:
            out.append(b)
    return out


def deserialize_turn(d: dict[str, Any]) -> Turn | None:
    """Inverse of the per-turn shape emitted by ``serialize_conversation_detail``.

    Returns ``None`` for any non-dict input. The deserializer treats every
    shape-mismatch as recoverable (the caller falls back to local execution),
    which is more robust against schema-drift between older/newer servers
    than raising an exception the CLI catch ladder may not handle.
    """
    if not isinstance(d, dict):
        return None
    try:
        tokens_raw = d.get("tokens")
        tokens: dict = tokens_raw if isinstance(tokens_raw, dict) else {}
        narrative = []
        # narrative can be a list, None, or anything else from a skewed
        # server. _coerce_list normalizes to a list; non-dict entries get
        # skipped inside deserialize_narrative_block.
        for nd in _coerce_list(d.get("narrative")):
            if not isinstance(nd, dict):
                continue
            block = deserialize_narrative_block(nd)
            if block is not None:
                narrative.append(block)
        narrative = _coalesce_adjacent_tool_calls(narrative)

        return Turn(
            timestamp=d.get("timestamp"),
            prompt_text=d.get("prompt"),
            total_input_tokens=_coerce_int(tokens.get("input")),
            total_output_tokens=_coerce_int(tokens.get("output")),
            narrative=narrative,
            prompt_id=d.get("prompt_id"),
            response_ids=_coerce_list(d.get("response_ids")),
            tool_call_ids=_coerce_list(d.get("tool_call_ids")),
        )
    except Exception:
        # Any unexpected field-level failure (e.g. malformed narrative block
        # raising despite isinstance guards) — schema-mismatch fallback.
        return None


def deserialize_conversation_detail(body: dict[str, Any]) -> ConversationDetail | None:
    """Inverse of ``serialize_conversation_detail``.

    Body shape: ``{"conversation": <detail dict>}``. Returns ``None`` when:

    - the body doesn't contain a ``"conversation"`` key (e.g. an error
      response like ``{"error": ...}``), or
    - the value at ``"conversation"`` is not a dict (older/newer server
      returning an unexpected shape — the schema-mismatch fallback case
      flagged in round-3 + round-4 reviews), or
    - the ``id`` field is missing (we can't construct a valid
      ``ConversationDetail`` without an id).

    The serialized form carries a combined ``total_tokens`` rather than
    split input/output sums; we reconstruct the splits by summing per-turn
    tokens. When no turns are present, the split defaults to
    ``(total_tokens, 0)`` — a known small loss documented at the top of
    this module.
    """
    if not isinstance(body, dict):
        return None
    if "conversation" not in body:
        return None
    d = body["conversation"]
    if not isinstance(d, dict):
        return None
    if "id" not in d:
        return None

    try:
        raw_turns = d.get("turns")
        if not isinstance(raw_turns, list):
            raw_turns = []
        turns = []
        for t in raw_turns:
            turn = deserialize_turn(t)
            if turn is not None:
                turns.append(turn)

        total_in = sum(t.total_input_tokens for t in turns)
        total_out = sum(t.total_output_tokens for t in turns)
        if not turns:
            total_in, total_out = _coerce_int(d.get("total_tokens")), 0

        return ConversationDetail(
            id=d["id"],
            workspace_path=d.get("workspace"),
            model=d.get("model"),
            started_at=d.get("started_at"),
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            turns=turns,
            tags=_coerce_list(d.get("tags")),
            cost=_coerce_cost(d.get("cost")),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Export artifact
# ---------------------------------------------------------------------------


def deserialize_export_artifact(body: dict[str, Any]) -> ExportArtifact | None:
    """Inverse of ``serialize_export_artifact``.

    The serve route for the rendered-artifact path (added in Phase C of the
    wire-form dissolution) returns a JSON dict with the artifact's content,
    media type, filename, and count.

    Returns ``None`` if the body is malformed (older server returning the
    legacy ``{"conversations": [...]}`` shape, or any other unexpected
    structure). The CLI's delegation path treats ``None`` as "fall back to
    local execute," which is the desired schema-drift behavior.
    """
    if not isinstance(body, dict) or "content" not in body:
        return None
    try:
        return ExportArtifact(
            content=body["content"],
            media_type=body.get("media_type", "text/markdown"),
            filename=body.get("filename", "siftd-export"),
            count=_coerce_int(body.get("count")),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Search view
# ---------------------------------------------------------------------------


def _ref_from_wire(d: Any) -> Any:
    """Reconstruct a :class:`FileRef` from a wire ref dict (metadata, no content).

    The wire carries ``{basename, path, op, content_length}`` (content is never
    serialized — ``--refs`` content dumps run against the local DB). The
    renderers read ``.basename``/``.op``/``.path`` attributes, so we rebuild a
    typed FileRef with ``content=None`` rather than leave a bare dict.
    """
    from siftd.api.file_refs import FileRef

    if not isinstance(d, dict):
        return None
    return FileRef(
        path=d.get("path", "") or "",
        basename=d.get("basename", "") or "",
        op=d.get("op", "") or "",
        content=None,
    )


def _chunk_render_dict(c: dict[str, Any]) -> dict[str, Any]:
    """Inverse of ``serve_fmt._wire_chunk`` — rebuild a renderer render-dict.

    Restores the ``_workspace``/``_started_at``/``_exchanges``/``_context``
    keys (and a typed breakdown / file-ref list) the output formatters consume,
    so a delegated render is byte-identical to a local one.
    """
    from siftd.domain.search_types import ScoreBreakdown

    conv = c.get("conversation") if isinstance(c.get("conversation"), dict) else {}
    rd: dict[str, Any] = {
        "conversation_id": c.get("conversation_id"),
        "score": c.get("score", 0.0),
        "chunk_type": c.get("chunk_type", ""),
        "display_label": c.get("display_label", ""),
        "text": c.get("text", ""),
        "_workspace": conv.get("workspace") or "",
        "_started_at": conv.get("started_at") or "",
        "chunk_id": c.get("chunk_id"),
        "source_ids": _coerce_list(c.get("source_ids")),
        "turn_index": c.get("turn_index"),
    }
    if c.get("event_id") is not None:
        rd["event_id"] = c.get("event_id")
    breakdown = c.get("breakdown")
    if isinstance(breakdown, dict):
        rd["breakdown"] = ScoreBreakdown.from_mapping(breakdown)
    file_refs = c.get("file_refs")
    if file_refs:
        rd["file_refs"] = [r for r in (_ref_from_wire(x) for x in file_refs) if r is not None]
    exchanges = c.get("exchanges")
    if exchanges:
        rd["_exchanges"] = [tuple(ex) for ex in exchanges if isinstance(ex, list)]
    context_window = c.get("context")
    if context_window:
        rd["_context"] = [tuple(x) for x in context_window if isinstance(x, list)]
    return rd


def _conv_render_dict(c: dict[str, Any]) -> dict[str, Any]:
    """Inverse of ``serve_fmt._wire_conv`` — rebuild a conversations render-dict."""
    return {
        "conversation_id": c.get("conversation_id"),
        "max_score": c.get("max_score", 0.0),
        "mean_score": c.get("mean_score", 0.0),
        "chunk_count": c.get("chunk_count", 0),
        "best_excerpt": c.get("best_excerpt", ""),
        "_workspace": c.get("workspace") or "",
        "_started_at": c.get("started_at") or "",
        "file_refs": [],
    }


def deserialize_search_view(body: dict[str, Any]) -> Any:
    """Inverse of ``serve_fmt.render_search`` — rebuild a :class:`SearchView`.

    Branches on the envelope's ``view`` (chunks/thread/conversations),
    reconstructing render-dicts plus the ``tier1``/``tier2`` split,
    ``n_skipped`` and ``empty_reason`` so the delegated CLI renders identically
    to local execution. Returns ``None`` on schema mismatch (caller falls back
    to local execute), matching the other deserializers' contract.
    """
    if not isinstance(body, dict):
        return None
    from siftd.domain.search_types import SearchView

    view = body.get("view", "chunks")
    n_skipped = _coerce_int(body.get("n_skipped"))
    empty_reason = body.get("empty_reason")
    # Serve delegation is server-authoritative for the executed engine: the wire
    # ``mode`` is what the server actually ran (a degrade to fts included), so the
    # delegated CLI reports it via ``SearchView.executed_mode`` rather than its own
    # pre-resolved mode. ``None`` when the server omitted it (legacy envelope).
    executed_mode = body.get("mode")

    try:
        if view == "thread":
            tier1 = [_chunk_render_dict(c) for c in _coerce_list(body.get("tier1")) if isinstance(c, dict)]
            tier2 = [_chunk_render_dict(c) for c in _coerce_list(body.get("tier2")) if isinstance(c, dict)]
            return SearchView(
                results=tier1 + tier2,
                view="thread",
                tier1=tier1,
                tier2=tier2,
                n_skipped=n_skipped,
                empty_reason=empty_reason,
                executed_mode=executed_mode,
            )
        rows = _coerce_list(body.get("results"))
        if view == "conversations":
            results = [_conv_render_dict(c) for c in rows if isinstance(c, dict)]
        else:
            results = [_chunk_render_dict(c) for c in rows if isinstance(c, dict)]
        return SearchView(
            results=results,
            view=view,
            n_skipped=n_skipped,
            empty_reason=empty_reason,
            executed_mode=executed_mode,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def from_wire(render_method: str, body: dict[str, Any]) -> Any:
    """Pick the right deserializer based on the Operation's render_method.

    Returns the body unchanged for render methods that don't have (or don't
    need) a typed reconstruction — the caller will use the raw dict.
    """
    if render_method == "list":
        return deserialize_conversation_list(body)
    if render_method == "detail":
        return deserialize_conversation_detail(body)
    if render_method == "export-artifact":
        return deserialize_export_artifact(body)
    if render_method == "search":
        return deserialize_search_view(body)
    # No registered deserializer — caller handles the raw dict (e.g. stats,
    # tags, raw).
    return body


def deserialize_caveats(body: Any) -> list:
    """Reconstruct caveat Findings from a delegation response envelope (I5).

    Inverse of ``serialization.serve_fmt.serialize_caveats`` (which emits
    ``asdict(Finding)`` under the envelope's top-level ``caveats`` key). The
    typed result deserializers above intentionally extract only the result rows
    and drop this key; this lets delegated callers thread it back so a thin
    client surfaces the same editorial-honesty caveats (stale index, degraded
    mode, truncation) that local execution would — without it, every delegated
    read silently reports ``caveats: []``.

    Defensive by design: non-dict entries are skipped and unknown keys dropped,
    so a server on a newer ``Finding`` shape degrades to the fields this client
    knows rather than raising. Returns ``[]`` when the envelope carries none.
    """
    if not isinstance(body, dict):
        return []
    raw = body.get("caveats")
    if not isinstance(raw, list):
        return []
    import dataclasses

    from siftd.doctor.checks import Finding

    known = {f.name for f in dataclasses.fields(Finding)}
    required = {"check", "severity", "message", "fix_available"}
    out: list = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kwargs = {k: v for k, v in entry.items() if k in known}
        if not required <= kwargs.keys():
            continue
        try:
            out.append(Finding(**kwargs))
        except Exception:
            continue
    return out
