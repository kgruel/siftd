"""Conversation listing and detail API."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from painted import Fidelity

from siftd.domain.search_types import ROLE_ASSISTANT, ROLE_USER
from siftd.paths import db_path as default_db_path
from siftd.safecall import parse_json
from siftd.safecall import read_text as _safe_read_text
from siftd.storage.conversation_stats import (
    get_conversation_cost,
    has_conversation_stats_table,
)
from siftd.storage.filters import WhereBuilder
from siftd.storage.fts import fts5_first_event_in_conversation
from siftd.storage.fts import sanitize_fts5_query as sanitize_fts5_query  # re-export for the api boundary
from siftd.storage.queries import (
    fetch_conversation_by_id_or_prefix,
    fetch_conversation_model,
    fetch_conversation_tags,
    fetch_conversation_token_totals,
    fetch_prompt_text_contents,
    fetch_prompts_for_conversation,
    fetch_response_content_blocks,
    fetch_responses_for_conversation,
    fetch_tags_for_conversations,
    fetch_tool_calls_for_conversation,
)
from siftd.storage.sql_helpers import (
    batched_in_query,
    has_conversation_owners_table,
    owner_predicate,
)
from siftd.storage.sqlite import open_database


class AnchorError(Exception):
    """Raised when an anchor cannot be resolved during get_conversation."""


class AnchorOutOfRange(AnchorError):
    """--at-turn N is out of range for this conversation."""

    def __init__(self, turn_count: int) -> None:
        self.turn_count = turn_count
        super().__init__(f"turn index out of range (conversation has {turn_count} turns)")


class AnchorNotFound(AnchorError):
    """--around PHRASE matched nothing in this conversation."""

    def __init__(self, phrase: str) -> None:
        self.phrase = phrase
        super().__init__(f"phrase not found in conversation: {phrase!r}")


class AnchorPhraseInvalid(AnchorError):
    """--around PHRASE could not be parsed by FTS5."""

    def __init__(self, phrase: str) -> None:
        self.phrase = phrase
        super().__init__(f"invalid FTS5 phrase: {phrase!r}")


class AmbiguousPrefix(Exception):
    """Prefix matches multiple targets — caller must use a longer prefix or full ID.

    ``candidate_kinds`` (when supplied) is parallel to ``matched_ids`` and labels
    each candidate by target kind, used when a bare-ULID prefix collides across
    conversations and events (e.g. ``01HX… (response)`` vs ``01HX… (conversation)``).
    ``noun`` names the collided population for the summary line.
    """

    def __init__(
        self,
        prefix: str,
        matched_ids: list[str],
        total: int,
        *,
        candidate_kinds: list[str] | None = None,
        noun: str = "conversations",
    ) -> None:
        self.prefix = prefix
        self.matched_ids = matched_ids  # up to 5
        self.total = total
        self.candidate_kinds = candidate_kinds  # parallel to matched_ids, or None
        self.noun = noun
        super().__init__(f"prefix {prefix!r} matches {total} {noun}")


@dataclass
class ToolCallSummary:
    """Collapsed tool call for timeline display."""

    tool_name: str
    status: str
    count: int = 1


@dataclass
class ToolCallDetail:
    """Tool call with optional input/result for --tools mode."""

    tool_name: str
    status: str
    count: int = 1
    input: str | None = None
    result: str | None = None
    tool_call_id: str | None = None


@dataclass
class NarrativeBlock:
    """A single block in the response narrative."""

    block_type: str  # "text", "thinking", "tool_calls", "tool_result", "tool_output"
    content: str | None = None
    tool_calls: list[ToolCallDetail] = field(default_factory=list)
    event_id: str | None = None


@dataclass
class Turn:
    """A prompt and its full response narrative."""

    PROMPT_ROLE_LABEL: ClassVar[str] = ROLE_USER
    RESPONSE_ROLE_LABEL: ClassVar[str] = ROLE_ASSISTANT

    timestamp: str | None
    prompt_text: str | None
    total_input_tokens: int
    total_output_tokens: int
    narrative: list[NarrativeBlock] = field(default_factory=list)
    _tool_call_summaries: list[ToolCallSummary] = field(
        default_factory=list, repr=False,
    )
    prompt_id: str | None = None
    response_ids: list[str] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)

    @property
    def response_text(self) -> str | None:
        """Concatenated text blocks (excludes thinking/tool_calls)."""
        parts = [b.content for b in self.narrative
                 if b.block_type == "text" and b.content]
        return " ".join(parts) if parts else None

    @property
    def tool_call_summaries(self) -> list[ToolCallSummary]:
        """Collapsed tool calls for this turn."""
        return self._tool_call_summaries


@dataclass
class Exchange:
    """A prompt-response pair in the timeline."""

    timestamp: str | None
    prompt_text: str | None
    response_text: str | None
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolCallSummary] = field(default_factory=list)


@dataclass
class ConversationSummary:
    """Summary row for conversation listing."""

    id: str
    workspace_path: str | None
    model: str | None
    started_at: str | None
    prompt_count: int
    response_count: int
    total_tokens: int
    cost: float | None
    tags: list[str] = field(default_factory=list)
    owner: str | None = None
    # Adapter-scoped identity (e.g. "claude_code::<uuid>"). A spawned sub-agent
    # encodes its root session here as "<root>::agent::<agentId>", so the parent
    # link is derivable without a schema column (see parent_external_id).
    external_id: str | None = None
    parent_external_id: str | None = None
    # Sub-agent type (e.g. "Explore", "feature-dev:code-reviewer"), captured at
    # ingest from the Claude Code agent-<id>.meta.json sidecar (scope='analyzer'
    # 'subagent_type' attribute). None for top-level sessions and for historical
    # sub-agents whose sidecar had rotated off disk before ingest.
    agent_type: str | None = None


@dataclass
class ConversationDetail:
    """Full conversation with timeline."""

    id: str
    workspace_path: str | None
    model: str | None
    started_at: str | None
    total_input_tokens: int
    total_output_tokens: int
    turns: list[Turn]
    tags: list[str] = field(default_factory=list)
    cost: float | None = None
    # Element-level tags keyed by event id (prompt/response/tool_call/exchange
    # targets; exchange tags land on their anchor prompt's id). Batch-fetched.
    # Each value is a list of (tag name, target_kind) pairs — the kind rides the
    # chip so a remove posts against the assignment the user actually clicked.
    event_tags: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @property
    def exchanges(self) -> list[Exchange]:
        """Backward-compat: derive per-turn exchanges from turns."""
        return _turns_to_exchanges(self.turns)


def _turns_to_exchanges(turns: list[Turn]) -> list[Exchange]:
    """Convert turns to backward-compat exchanges (one per turn)."""
    return [
        Exchange(
            timestamp=t.timestamp,
            prompt_text=t.prompt_text,
            response_text=t.response_text,
            input_tokens=t.total_input_tokens,
            output_tokens=t.total_output_tokens,
            tool_calls=t.tool_call_summaries,
        )
        for t in turns
    ]


def list_conversations(
    *,
    fidelity: Fidelity,
    db_path: Path | None = None,
    workspace: str | None = None,
    workspace_id: str | None = None,
    model: str | None = None,
    since: str | None = None,
    before: str | None = None,
    search: str | None = None,
    tool: str | None = None,
    tag: str | list[str] | None = None,
    all_tags: list[str] | None = None,
    no_tag: list[str] | None = None,
    tag_kind: list[str] | None = None,
    tool_tag: str | None = None,
    n: int = 10,
    oldest: bool = False,
    owner: str | None = None,
    group_subagents: bool = False,
) -> list[ConversationSummary]:
    """List conversations with optional filtering.

    Args:
        fidelity: Cross-stage rendering contract carried through to the
            renderer, which emits the cost column at ``depth >= 3``. Cost
            itself is no longer recomputed here: the fast path reads the
            precomputed ``conversation_stats.cost`` (the rollup's single
            canonical definition), and the no-stats fallback emits NULL cost
            rather than re-deriving it (see ``_list_conversations_impl``).
        db_path: Path to database. Uses default if not specified.
        workspace: Filter by workspace path substring.
        workspace_id: Filter by exact workspace ULID (workspaces.id); distinct
            from ``workspace`` path/remote substring.
        model: Filter by model name substring.
        since: Filter conversations started after this date (ISO format).
        before: Filter conversations started before this date.
        search: FTS5 full-text search query.
        tool: Filter by canonical tool name (e.g., 'shell.execute').
        tag: OR filter — conversations with any of these tags. Also accepts
            a single string for backward compat.
        all_tags: AND filter — conversations with all of these tags.
        no_tag: NOT filter — exclude conversations with any of these tags.
        tag_kind: Scope tag/all_tags/no_tag matching to specific target_kinds
            (e.g., ['conversation'], ['response', 'tool_call']). Defaults to
            all conversation-bearing kinds when None.
        tool_tag: Filter by tool call tag (e.g., 'shell:test').
        n: Maximum results to return (0 = unlimited).
        oldest: Sort by oldest first instead of newest.
        owner: Filter to conversations owned by this user_id.
        group_subagents: Page by root session — ``n`` then counts only
            top-level sessions, and every sub-agent of a paged root is pulled
            in (owner-scoped) regardless of the limit, so the renderer can nest
            them. Sub-agents are identified by the ``::agent::`` marker in
            external_id. Off by default (flat listing).

    Returns:
        List of ConversationSummary objects.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)
    try:
        return _list_conversations_impl(conn, workspace, model, since, before, search, tool, tag, all_tags, no_tag, tool_tag, n, oldest, fidelity, owner, tag_kind, workspace_id, group_subagents)
    finally:
        conn.close()


def _list_conversations_impl(
    conn,
    workspace: str | None,
    model: str | None,
    since: str | None,
    before: str | None,
    search: str | None,
    tool: str | None,
    tag: str | list[str] | None,
    all_tags: list[str] | None,
    no_tag: list[str] | None,
    tool_tag: str | None,
    n: int,
    oldest: bool,
    fidelity: Fidelity,
    owner: str | None = None,
    tag_kind: list[str] | None = None,
    workspace_id: str | None = None,
    group_subagents: bool = False,
) -> list[ConversationSummary]:
    """Implementation of list_conversations with connection already open."""
    # Build WHERE clauses
    wb = WhereBuilder()
    wb.workspace(workspace)
    wb.workspace_id(workspace_id)
    wb.model(model)
    wb.since(since)
    wb.before(before)
    if owner and not has_conversation_owners_table(conn):
        return []
    wb.owner(owner)

    if search:
        wb.add(
            "c.id IN (SELECT conversation_id FROM content_fts WHERE content_fts MATCH ?)",
            search,
        )

    wb.tool(tool)

    # Normalize tag: accept str (single) or list (OR filter)
    effective_tags = [tag] if isinstance(tag, str) else list(tag or [])

    wb.tags_any(effective_tags or None, kinds=tag_kind)
    wb.tags_all(all_tags, kinds=tag_kind)
    wb.tags_none(no_tag, kinds=tag_kind)

    wb.tool_tag(tool_tag)

    if group_subagents:
        # Page by ROOT session: a sub-agent's external_id is "<root>::agent::…",
        # so excluding it here makes the n-limit count top-level sessions only.
        # Their sub-agents are pulled in unconditionally below, so a parent and
        # its children always travel together regardless of the page boundary.
        wb.add("c.external_id NOT LIKE '%::agent::%'")

    where = wb.where_sql()
    params = wb.params
    order = "ASC" if oldest else "DESC"
    limit_clause = f"LIMIT {n}" if n > 0 else ""

    # Phase 1: Identify the target conversations quickly.
    # WhereBuilder tracks which JOINs its filters actually need, so we only
    # join responses/models when a filter (e.g. --model) requires them.
    phase1_joins = wb.joins_sql()
    id_sql = f"""
        SELECT c.id, c.external_id
        FROM conversations c
        {phase1_joins}
        {where}
        ORDER BY c.started_at {order}
        {limit_clause}
    """
    id_rows = conn.execute(id_sql, params).fetchall()
    conv_ids = [row["id"] for row in id_rows]

    if not conv_ids:
        return []

    if group_subagents:
        # Pull every sub-agent of the paged roots (owner-scoped, no n-limit) so
        # the renderer can nest them — they were excluded from the count above.
        root_exts = [row["external_id"] for row in id_rows if row["external_id"]]
        seen = set(conv_ids)
        for cid in _fetch_subagent_ids(conn, root_exts, owner):
            if cid not in seen:
                conv_ids.append(cid)
                seen.add(cid)

    placeholders = ",".join("?" * len(conv_ids))
    use_stats = has_conversation_stats_table(conn)

    if use_stats:
        # Fast path: read precomputed stats from conversation_stats table.
        # COALESCEs to live subqueries for any rows missing from the stats
        # table (e.g. conversations inserted since the last ingest rebuild).
        rows = conn.execute(
            f"""SELECT c.id AS conversation_id, w.path AS workspace,
                    c.started_at, c.external_id AS external_id,
                    COALESCE(cs.prompt_count,
                        (SELECT COUNT(*) FROM events WHERE kind = 'prompt' AND conversation_id = c.id)
                    ) AS prompts,
                    COALESCE(cs.response_count,
                        (SELECT COUNT(*) FROM events WHERE kind = 'response' AND conversation_id = c.id)
                    ) AS responses,
                    COALESCE(cs.total_tokens,
                        (SELECT COALESCE(SUM(er2.input_tokens),0) + COALESCE(SUM(er2.output_tokens),0)
                         FROM events e2 JOIN event_response er2 ON er2.event_id = e2.id
                         WHERE e2.kind = 'response' AND e2.conversation_id = c.id)
                    ) AS tokens,
                    COALESCE(cs.model_name,
                        (SELECT m2.name FROM events e2
                         JOIN event_response er2 ON er2.event_id = e2.id
                         LEFT JOIN models m2 ON m2.id = er2.model_id
                         WHERE e2.kind = 'response' AND e2.conversation_id = c.id
                         GROUP BY m2.name ORDER BY COUNT(*) DESC LIMIT 1)
                    ) AS model,
                    cs.cost
                FROM conversations c
                LEFT JOIN workspaces w ON w.id = c.workspace_id
                LEFT JOIN conversation_stats cs ON cs.conversation_id = c.id
                WHERE c.id IN ({placeholders})
                ORDER BY c.started_at {order}""",
            conv_ids,
        ).fetchall()
    else:
        # Fallback: compute from source tables when the materialized derived
        # tier is absent — a sliced / never-ingested DB that hasn't built
        # conversation_stats (and therefore has no usage_by_conv_model either,
        # since both are ensured together at write-open).  Cost is the rollup's
        # single canonical definition, so it is NOT re-derived here: with no
        # materialized tier, cost is NULL (unknown), never a divergently-priced
        # number.  (The retired fallback used coalesce_pricing=True with a plain
        # provider join and no harness-source fallback, which mispriced the ~21%
        # of responses with a NULL provider_id and emitted 0.0 where the
        # canonical path emits NULL.)
        rows = conn.execute(
            f"""SELECT c.id AS conversation_id, w.path AS workspace,
                    (SELECT m2.name FROM events e2
                     JOIN event_response er2 ON er2.event_id = e2.id
                     LEFT JOIN models m2 ON m2.id = er2.model_id
                     WHERE e2.kind = 'response' AND e2.conversation_id = c.id
                     GROUP BY m2.name ORDER BY COUNT(*) DESC LIMIT 1) AS model,
                    c.started_at, c.external_id AS external_id,
                    (SELECT COUNT(*) FROM events WHERE kind = 'prompt' AND conversation_id = c.id) AS prompts,
                    (SELECT COUNT(*) FROM events WHERE kind = 'response' AND conversation_id = c.id) AS responses,
                    (SELECT COALESCE(SUM(er2.input_tokens), 0) + COALESCE(SUM(er2.output_tokens), 0)
                     FROM events e2 JOIN event_response er2 ON er2.event_id = e2.id
                     WHERE e2.kind = 'response' AND e2.conversation_id = c.id) AS tokens,
                    NULL AS cost
                FROM conversations c
                LEFT JOIN workspaces w ON w.id = c.workspace_id
                WHERE c.id IN ({placeholders})
                ORDER BY c.started_at {order}""",
            conv_ids,
        ).fetchall()

    # Bulk-fetch tags, owners, and sub-agent types
    tags_by_conv = fetch_tags_for_conversations(conn, conv_ids)
    owner_by_conv = _fetch_owners_for_conversations(conn, conv_ids)
    agent_type_by_conv = _fetch_agent_types_for_conversations(conn, conv_ids)

    summaries: list[ConversationSummary] = []
    for row in rows:
        ext = row["external_id"]
        # Sub-agent identity is "<root>::agent::<agentId>"; the root before the
        # marker is the parent session's external_id. None for a top-level conv.
        parent_ext = ext.split("::agent::")[0] if ext and "::agent::" in ext else None
        summaries.append(
            ConversationSummary(
                id=row["conversation_id"],
                workspace_path=row["workspace"],
                model=row["model"],
                started_at=row["started_at"],
                prompt_count=row["prompts"],
                response_count=row["responses"],
                total_tokens=row["tokens"],
                cost=row["cost"],
                tags=tags_by_conv.get(row["conversation_id"], []),
                owner=owner_by_conv.get(row["conversation_id"]),
                external_id=ext,
                parent_external_id=parent_ext,
                agent_type=agent_type_by_conv.get(row["conversation_id"]),
            )
        )
    return summaries


def _fetch_subagent_ids(conn, root_external_ids: list[str], owner: str | None) -> list[str]:
    """IDs of every sub-agent conversation whose root session is in the page.

    A sub-agent's external_id is ``<root>::agent::<agentId>``; match the prefix
    before ``::agent::`` against the paged roots. Owner-scoped exactly like the
    main query — a visible parent must not grant access to a child owned by
    someone else (IDOR), and an absent owners table with an owner filter yields
    nothing, matching the caller's own short-circuit.
    """
    if not root_external_ids:
        return []
    if owner and not has_conversation_owners_table(conn):
        return []
    cwb = WhereBuilder()
    cwb.owner(owner)
    placeholders = ",".join("?" * len(root_external_ids))
    cwb.add(
        "c.external_id LIKE '%::agent::%'"
        " AND substr(c.external_id, 1, instr(c.external_id, '::agent::') - 1)"
        f" IN ({placeholders})",
        *root_external_ids,
    )
    sql = f"SELECT c.id FROM conversations c {cwb.joins_sql()} {cwb.where_sql()}"
    return [row["id"] for row in conn.execute(sql, cwb.params).fetchall()]


def _fetch_agent_types_for_conversations(
    conn,
    conversation_ids: list[str],
) -> dict[str, str]:
    """Map conversation_id -> sub-agent type, from the 'subagent_type' attribute.

    Captured at ingest from the Claude Code agent-<id>.meta.json sidecar (see the
    claude_code adapter). Absent for top-level sessions and for historical
    sub-agents whose sidecar rotated off disk before ingest. Defensive against a
    pre-v4 DB with no attributes table (read-only opens) — returns {} rather
    than raising, matching the owners fetcher's degrade-don't-crash contract.
    """
    if not conversation_ids:
        return {}
    placeholders = ",".join("?" * len(conversation_ids))
    try:
        rows = conn.execute(
            "SELECT target_id, value FROM attributes"
            " WHERE target_kind = 'conversation' AND key = 'subagent_type'"
            f" AND target_id IN ({placeholders})",
            conversation_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["target_id"]: row["value"] for row in rows}


def _fetch_owners_for_conversations(
    conn,
    conversation_ids: list[str],
) -> dict[str, str]:
    """Bulk fetch owners for multiple conversations.

    Returns dict mapping conversation_id to user_id.
    Returns empty dict if the conversation_owners table doesn't exist
    (e.g. hand-built DBs, pre-migration schemas).
    """
    if not conversation_ids:
        return {}

    # Table may not exist on DBs created outside open_database()
    if not has_conversation_owners_table(conn):
        return {}

    rows = batched_in_query(
        conn,
        "SELECT conversation_id, user_id "
        "FROM conversation_owners "
        "WHERE conversation_id IN ({placeholders})",
        conversation_ids,
    )
    return {row["conversation_id"]: row["user_id"] for row in rows}


def _extract_text(raw: str) -> str:
    """Extract plain text from a content block (may be JSON-wrapped)."""
    obj = parse_json(raw)
    if isinstance(obj, dict) and "text" in obj:
        return obj["text"]
    return raw


def _resolve_anchor(
    turns: list,
    anchor: str,
    anchor_value: int | str | None,
    conn,
    conv_id: str,
) -> int:
    """Map an anchor spec to a turn index.

    Raises:
        AnchorOutOfRange: For 'at_turn' when N >= len(turns).
        AnchorNotFound: For 'around' when phrase has no match.
        AnchorPhraseInvalid: For 'around' when phrase fails FTS5 parsing.
    """
    if anchor == "from_start":
        return 0
    if anchor == "from_end":
        return max(0, len(turns) - 1)
    if anchor == "at_turn":
        n = int(anchor_value)  # type: ignore[arg-type]
        if n < 0 or n >= len(turns):
            raise AnchorOutOfRange(len(turns))
        return n
    if anchor == "around":
        phrase = str(anchor_value)
        try:
            event_id = fts5_first_event_in_conversation(conn, phrase, conversation_id=conv_id)
        except sqlite3.OperationalError as e:
            raise AnchorPhraseInvalid(phrase) from e
        if event_id is None:
            raise AnchorNotFound(phrase)
        # Map event_id to turn index via Turn.prompt_id / Turn.response_ids.
        for idx, turn in enumerate(turns):
            if turn.prompt_id == event_id:
                return idx
            if event_id in turn.response_ids:
                return idx
            if event_id in turn.tool_call_ids:
                return idx
        # Event found in FTS but not in any turn (e.g. orphaned event).
        raise AnchorNotFound(phrase)
    raise ValueError(f"unknown anchor: {anchor!r}")


def get_conversation(
    id: str,
    *,
    fidelity: Fidelity,
    db_path: Path | None = None,
    tool_filter: str | None = None,
    owner: str | None = None,
    anchor: str | None = None,
    anchor_value: int | str | None = None,
    window_start: int | None = None,
    window_end: int | None = None,
) -> ConversationDetail | None:
    """Get full conversation detail by ID.

    Supports prefix matching on conversation ID.

    Args:
        id: Full or prefix of conversation ULID.
        fidelity: Cross-stage rendering contract. ``fidelity.shows("thinking")``
            decides whether thinking blocks appear in turns;
            ``fidelity.shows("tools")`` decides whether tool inputs/results
            are fetched and inlined.
        db_path: Path to database. Uses default if not specified.
        tool_filter: Filter tool calls — 'errors' for failed only,
            or a tool name prefix (e.g. 'shell', 'file.read').
        anchor: Anchor axis — one of 'from_start', 'from_end', 'at_turn',
            'around'. None means no anchor (whole conversation returned).
        anchor_value: Value for the anchor: int for 'at_turn', str for
            'around'. Ignored for 'from_start' and 'from_end'.
        window_start: Turn offset from anchor (inclusive). None = anchor only.
        window_end: Turn offset from anchor (inclusive). None = anchor only.

    Returns:
        ConversationDetail with timeline, or None if not found.

    Raises:
        FileNotFoundError: If database does not exist.
        AmbiguousPrefix: If ``id`` is a prefix matching more than one conversation.
            Programmatic callers should catch this; CLI callers print the matched IDs
            and exit 2.
        AnchorOutOfRange: If ``anchor='at_turn'`` and N >= turn count.
        AnchorNotFound: If ``anchor='around'`` and phrase has no match.
        AnchorPhraseInvalid: If ``anchor='around'`` phrase cannot be parsed by FTS5.
    """
    include_thinking = fidelity.shows("thinking")
    include_tool_content = fidelity.shows("tools")
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)
    try:
        # Resolve prefix → full ID; raises AmbiguousPrefix if multiple matches.
        # Owner filter is applied inside resolve_entity_id, making the explicit
        # owner check that was here redundant.
        conv_id = resolve_entity_id(conn, "conversation", id, owner=owner)
        if not conv_id:
            return None

        # Fetch metadata (workspace, started_at) by the now-resolved full ID.
        # conv_id was just resolved, so this is an exact match; None means a
        # concurrent delete, which we treat as not found.
        conv = fetch_conversation_by_id_or_prefix(conn, conv_id)
        if not conv:
            return None

        # Model (most frequent) and token totals
        model_name = fetch_conversation_model(conn, conv_id)
        total_input, total_output = fetch_conversation_token_totals(conn, conv_id)

        # Fetch prompts and their text content (bulk)
        prompts = fetch_prompts_for_conversation(conn, conv_id)
        prompt_ids = [p["id"] for p in prompts]
        all_prompt_blocks = fetch_prompt_text_contents(conn, prompt_ids)
        prompt_texts: dict[str, str] = {}
        for pid in prompt_ids:
            blocks = all_prompt_blocks.get(pid, [])
            parts = [_extract_text(b["content"]) for b in blocks]
            prompt_texts[pid] = " ".join(parts).strip()

        # Fetch responses
        responses = fetch_responses_for_conversation(conn, conv_id)
        response_ids = [r["id"] for r in responses]

        # Always fetch text blocks for narrative; gate extra block kinds by fidelity.
        block_types: list[str] = ["text", "tool_use"]
        if include_thinking:
            block_types.append("thinking")
        if include_tool_content:
            block_types.extend(["tool_result", "tool_output"])
        all_content_blocks = fetch_response_content_blocks(
            conn, response_ids, tuple(block_types),
        )

        # Fetch tool calls grouped by response
        tool_calls = fetch_tool_calls_for_conversation(
            conn, conv_id, include_content=include_tool_content,
        )
        tc_by_response: dict[str, list] = {}
        for tc in tool_calls:
            tc_by_response.setdefault(tc["response_id"], []).append(tc)

        # Group responses by prompt_id
        responses_by_prompt: dict[str, list] = {}
        for r in responses:
            if r["prompt_id"]:
                responses_by_prompt.setdefault(r["prompt_id"], []).append(r)

        # Build turns (exchanges are derived via property)
        turns = []

        for p in prompts:
            prompt_id = p["id"]
            prompt_text = prompt_texts.get(prompt_id, "")

            prompt_responses = responses_by_prompt.get(prompt_id, [])
            if not prompt_responses:
                # Prompt with no response yet
                turns.append(
                    Turn(
                        timestamp=p["timestamp"],
                        prompt_text=prompt_text or None,
                        total_input_tokens=0,
                        total_output_tokens=0,
                        prompt_id=prompt_id,
                    )
                )
                continue

            turn_input = sum(r["input_tokens"] or 0 for r in prompt_responses)
            turn_output = sum(r["output_tokens"] or 0 for r in prompt_responses)

            narrative = _build_narrative(
                prompt_responses,
                all_content_blocks,
                tc_by_response,
                include_thinking=include_thinking,
                include_tool_content=include_tool_content,
                tool_filter=tool_filter,
            )

            # Build tool call summaries from DB data (independent of narrative)
            all_tcs = []
            for r in prompt_responses:
                all_tcs.extend(tc_by_response.get(r["id"], []))
            tool_summaries = _collapse_tool_call_rows(all_tcs)

            turn_response_ids = [r["id"] for r in prompt_responses]
            turn_tool_call_ids = [tc["tool_call_id"] for tc in all_tcs]

            turns.append(
                Turn(
                    timestamp=p["timestamp"],
                    prompt_text=prompt_text or None,
                    total_input_tokens=turn_input,
                    total_output_tokens=turn_output,
                    narrative=narrative,
                    _tool_call_summaries=tool_summaries,
                    prompt_id=prompt_id,
                    response_ids=turn_response_ids,
                    tool_call_ids=turn_tool_call_ids,
                )
            )

        # Tags + cost render in list/detail at depth >= 3. Cost is the rollup's
        # canonical precomputed value (None when no priced usage), never faked.
        tags = fetch_conversation_tags(conn, conv_id) if fidelity.depth >= 3 else []
        # Element tags always fetched (one batched query, no N+1) so transcript
        # chips appear at any depth.
        event_tags = _fetch_conversation_event_tags(conn, conv_id)
        cost = (
            get_conversation_cost(conn, conv_id)
            if fidelity.depth >= 3 and has_conversation_stats_table(conn)
            else None
        )

        # Anchor + window resolution (fetch-layer concern per fidelity-as-contract).
        # All turns are fetched above; we slice here to return only the requested window.
        if anchor is not None:
            anchor_idx = _resolve_anchor(turns, anchor, anchor_value, conn, conv_id)
            start = anchor_idx + (window_start or 0)
            end = anchor_idx + (window_end or 0)
            start = max(0, start)
            end = min(len(turns) - 1, end)
            turns = turns[start : end + 1]

        return ConversationDetail(
            id=conv_id,
            workspace_path=conv["workspace"],
            model=model_name,
            started_at=conv["started_at"],
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            turns=turns,
            tags=tags,
            cost=cost,
            event_tags=event_tags,
        )
    finally:
        conn.close()


def _fetch_conversation_event_tags(
    conn: sqlite3.Connection, conversation_id: str
) -> dict[str, list[tuple[str, str]]]:
    """Batch-fetch element tags for a conversation, keyed by event id.

    One query for all element (prompt/response/tool_call/exchange) tag
    assignments whose target is an event of this conversation — no N+1. Exchange
    tags anchor on the prompt event, so they key on that prompt's id.

    Each value is a list of ``(tag name, target_kind)`` pairs: the kind travels
    with the chip so a remove posts the kind the user clicked (an exchange chip
    sharing a prompt's id removes the exchange assignment, not the prompt one).
    """
    rows = conn.execute(
        "SELECT ta.target_id, ta.target_kind, tg.name FROM tag_assignments ta "
        "JOIN tags tg ON tg.id = ta.tag_id "
        "JOIN events e ON e.id = ta.target_id "
        "WHERE e.conversation_id = ? "
        "AND ta.target_kind IN ('prompt','response','tool_call','exchange') "
        "ORDER BY tg.name",
        (conversation_id,),
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        out.setdefault(row["target_id"], []).append((row["name"], row["target_kind"]))
    return out


def _matches_tool_filter(tool_name: str, status: str, tool_filter: str | None) -> bool:
    """Check if a tool call matches the given filter."""
    if tool_filter is None:
        return True
    if tool_filter == "errors":
        return status == "error"
    name = tool_name or ""
    return name == tool_filter or name.startswith(tool_filter + ".")


def _collapse_tool_call_rows(tool_calls: list) -> list[ToolCallSummary]:
    """Collapse consecutive tool call DB rows with same name+status."""
    if not tool_calls:
        return []

    collapsed = []
    prev_key = None
    count = 0

    for tc in tool_calls:
        name = tc["tool_name"] or "unknown"
        status = tc["status"] or "unknown"
        key = (name, status)

        if key == prev_key:
            count += 1
        else:
            if prev_key is not None:
                collapsed.append(
                    ToolCallSummary(tool_name=prev_key[0], status=prev_key[1], count=count)
                )
            prev_key = key
            count = 1

    if prev_key is not None:
        collapsed.append(
            ToolCallSummary(tool_name=prev_key[0], status=prev_key[1], count=count)
        )

    return collapsed


def _build_narrative(
    prompt_responses: list,
    all_content_blocks: dict[str, list],
    tc_by_response: dict[str, list],
    *,
    include_thinking: bool,
    include_tool_content: bool,
    tool_filter: str | None,
) -> list[NarrativeBlock]:
    """Build interleaved narrative blocks from response content.

    Walks all response content blocks in order (across all responses for
    a prompt), building text/thinking/tool_calls NarrativeBlocks.
    """
    narrative: list[NarrativeBlock] = []

    for r in prompt_responses:
        resp_id = r["id"]
        content_blocks = all_content_blocks.get(resp_id, [])
        resp_tool_calls = tc_by_response.get(resp_id, [])

        # Index into tool_calls list for fallback order; tool_use blocks
        # are best matched by external_id when available.
        tc_idx = 0
        tc_by_id = {}
        for tc in resp_tool_calls:
            tc_id = tc["external_id"]
            if tc_id:
                tc_by_id[tc_id] = tc
        used_ids: set[str] = set()
        # Accumulate consecutive tool calls for collapsing
        pending_tools: list[ToolCallDetail] = []

        def _flush_tools(narrative=narrative, resp_id=resp_id):
            nonlocal pending_tools
            if pending_tools:
                narrative.append(NarrativeBlock(
                    block_type="tool_calls",
                    tool_calls=_collapse_tool_details(
                        pending_tools,
                        collapse=not include_tool_content,
                    ),
                    event_id=resp_id,
                ))
                pending_tools = []

        for block in content_blocks:
            block_type = block["block_type"]

            if block_type == "text":
                _flush_tools()

                text = _extract_text(block["content"])
                if text.strip():
                    narrative.append(NarrativeBlock(
                        block_type="text",
                        content=text,
                        event_id=resp_id,
                    ))

            elif block_type == "thinking":
                if include_thinking:
                    _flush_tools()

                    text = _extract_thinking(block["content"])
                    if text.strip():
                        narrative.append(NarrativeBlock(
                            block_type="thinking",
                            content=text,
                            event_id=resp_id,
                        ))

            elif block_type == "tool_use":
                # Match to corresponding tool_call row by external_id (preferred),
                # fallback to order when missing or unmatched.
                tc = None
                tool_use_id = _extract_tool_use_id(block["content"])
                if tool_use_id:
                    tc = tc_by_id.get(tool_use_id)
                    if tc:
                        used_ids.add(tool_use_id)
                if tc is None:
                    while tc_idx < len(resp_tool_calls):
                        candidate = resp_tool_calls[tc_idx]
                        tc_idx += 1
                        candidate_id = candidate["external_id"]
                        if candidate_id and candidate_id in used_ids:
                            continue
                        tc = candidate
                        if candidate_id:
                            used_ids.add(candidate_id)
                        break

                if tc:
                    name = tc["tool_name"] or "unknown"
                    status = tc["status"] or "unknown"

                    if _matches_tool_filter(name, status, tool_filter):
                        detail = ToolCallDetail(
                            tool_name=name,
                            status=status,
                            input=tc["input"] if include_tool_content else None,
                            result=tc["result"] if include_tool_content else None,
                            tool_call_id=tc["tool_call_id"],
                        )
                        pending_tools.append(detail)

            elif block_type in ("tool_result", "tool_output"):
                _flush_tools()

                text = _extract_tool_result(block["content"])
                if text.strip():
                    narrative.append(NarrativeBlock(
                        block_type=block_type,
                        content=text,
                        event_id=resp_id,
                    ))

        # Flush remaining tool calls
        _flush_tools()

    return narrative


def _extract_thinking(raw: str) -> str:
    """Extract thinking text from a content block."""
    obj = raw if isinstance(raw, dict) else parse_json(raw)
    if isinstance(obj, dict):
        if "thinking" in obj:
            return obj.get("thinking", "")
        if "text" in obj:
            return obj.get("text", "")
        if "description" in obj or "subject" in obj:
            subject = obj.get("subject")
            description = obj.get("description")
            if subject and description:
                return f"{subject}: {description}"
            return description or subject or ""
    return raw


def _extract_tool_use_id(raw: str) -> str | None:
    """Extract tool_use id from a content block."""
    obj = raw if isinstance(raw, dict) else parse_json(raw)
    if isinstance(obj, dict):
        return obj.get("id") or obj.get("tool_use_id") or obj.get("call_id")
    return None


def _extract_tool_result(raw: str) -> str:
    """Extract display text from a tool_result/tool_output block."""
    obj = raw if isinstance(raw, dict) else parse_json(raw)
    if isinstance(obj, dict):
        for key in ("text", "content", "output", "result"):
            if key in obj:
                val = obj[key]
                if isinstance(val, str):
                    return val
                if isinstance(val, dict):
                    if "text" in val and isinstance(val["text"], str):
                        return val["text"]
                    return json.dumps(val)
                if isinstance(val, list):
                    parts = []
                    for item in val:
                        if isinstance(item, str):
                            parts.append(item)
                        elif isinstance(item, dict) and "text" in item:
                            parts.append(str(item["text"]))
                        else:
                            parts.append(json.dumps(item))
                    return "\n".join(parts)
                return str(val)
    return raw


def _collapse_tool_details(
    tools: list[ToolCallDetail],
    *,
    collapse: bool,
) -> list[ToolCallDetail]:
    """Collapse consecutive tool calls with same name+status."""
    if not tools:
        return []
    if not collapse:
        return tools

    collapsed: list[ToolCallDetail] = []
    prev = tools[0]
    count = 1

    for tc in tools[1:]:
        if tc.tool_name == prev.tool_name and tc.status == prev.status:
            count += 1
        else:
            collapsed.append(ToolCallDetail(
                tool_name=prev.tool_name,
                status=prev.status,
                count=count,
                input=prev.input,
                result=prev.result,
                tool_call_id=prev.tool_call_id if count == 1 else None,
            ))
            prev = tc
            count = 1

    collapsed.append(ToolCallDetail(
        tool_name=prev.tool_name,
        status=prev.status,
        count=count,
        input=prev.input,
        result=prev.result,
        tool_call_id=prev.tool_call_id if count == 1 else None,
    ))
    return collapsed


# =============================================================================
# User-defined SQL query files
# =============================================================================


@dataclass
class QueryFile:
    """Metadata about an available SQL report (builtin or user-defined).

    Attributes:
        name: Query file stem (without .sql extension).
        path: Path to the .sql file, or None for a packaged builtin.
        template_vars: Variables using $var syntax (text substitution).
        param_vars: Variables using :var syntax (parameterized, safe).
        variables: All variable names (union of template_vars and param_vars).
    """

    name: str
    path: Path | None
    template_vars: list[str]
    param_vars: list[str]

    @property
    def variables(self) -> list[str]:
        """All variable names (template + param)."""
        return sorted(set(self.template_vars + self.param_vars))


@dataclass
class QueryResult:
    """Result of running a SQL query file."""

    columns: list[str]
    rows: list[list]


def _extract_query_vars(sql: str) -> tuple[list[str], list[str]]:
    """Split a report's variables into ($var template, :var param) name lists."""
    import re

    template_pattern = re.compile(r"\$\{(\w+)\}|\$(\w+)")
    # Match :var but not ::var (Postgres cast) or :=var (assignment)
    param_pattern = re.compile(r"(?<!:):(\w+)\b(?!=)")
    template_vars = sorted(set(m[0] or m[1] for m in template_pattern.findall(sql)))
    param_vars = sorted(set(param_pattern.findall(sql)))
    return template_vars, param_vars


def _read_builtin_query(name: str) -> str | None:
    """Return a packaged builtin report's SQL, or None if there is no such builtin.

    Builtins are the fresh source of truth; unlike copied-and-forked files in the
    user queries dir they track the current schema automatically.
    """
    import importlib.resources

    try:
        ref = importlib.resources.files("siftd.builtin_queries").joinpath(f"{name}.sql")
        if ref.is_file():
            return ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, TypeError, OSError, UnicodeDecodeError):
        return None
    return None


def list_query_files() -> list[QueryFile]:
    """List available SQL reports — packaged builtins plus user overrides.

    Resolution mirrors how pricing and adapters work (builtin + user override),
    so reports never go stale the way copied-and-forked files do:
    - Builtins are packaged with the tool and always available (the fresh source).
    - Files in the user queries dir overlay them: a file with the same stem
      overrides the builtin; user-only files add new reports.

    Returns:
        List of QueryFile (sorted by name) with name, path, and required vars.
        ``path`` is None for a builtin, or the user file's path for an override.
    """
    from siftd.api.resources import list_builtin_queries
    from siftd.paths import queries_dir

    by_name: dict[str, QueryFile] = {}

    # 1. Builtins first — the fresh source of truth.
    for name in list_builtin_queries():
        sql = _read_builtin_query(name)
        if sql is None:
            continue
        template_vars, param_vars = _extract_query_vars(sql)
        by_name[name] = QueryFile(
            name=name, path=None, template_vars=template_vars, param_vars=param_vars
        )

    # 2. User dir overlays — override a builtin by stem, or add a new report.
    qdir = queries_dir()
    if qdir.exists():
        for f in sorted(qdir.glob("*.sql")):
            sql = _safe_read_text(f, context="list_query_files")
            if sql is None:
                continue
            template_vars, param_vars = _extract_query_vars(sql)
            by_name[f.stem] = QueryFile(
                name=f.stem, path=f, template_vars=template_vars, param_vars=param_vars
            )

    return [by_name[name] for name in sorted(by_name)]


class QueryError(Exception):
    """Error running a SQL query file."""

    pass


def run_query_file(
    name: str,
    variables: dict[str, str] | None = None,
    *,
    db_path: Path | None = None,
) -> QueryResult:
    """Run a user-defined SQL query file.

    Supports two variable syntaxes:
    - $var or ${var}: Text substitution (for structural elements like tables)
    - :var: Parameterized query (for values, with safe quoting)

    Args:
        name: Query file name (without .sql extension).
        variables: Dict of variable values. Same dict serves both syntaxes.
        db_path: Path to database. Uses default if not specified.

    Returns:
        QueryResult with columns and rows.

    Raises:
        FileNotFoundError: If database or query file doesn't exist.
        QueryError: If variables are missing or SQL fails.

    Example:
        SQL file with both syntaxes::

            SELECT * FROM $table
            WHERE workspace LIKE '%' || :ws || '%'
              AND started_at > :since

        Call with: run_query_file("myquery", {"table": "conversations",
                                              "ws": "project", "since": "2025-01"})
    """
    import re
    import sqlite3
    from string import Template

    from siftd.paths import queries_dir

    db = db_path or default_db_path()
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    qdir = queries_dir()
    sql_file = qdir / f"{name}.sql"
    if sql_file.exists():
        # User dir overrides the builtin (intentional customization).
        try:
            sql = sql_file.read_text()
        except (OSError, UnicodeDecodeError) as e:
            raise QueryError(f"Cannot read query file {sql_file}: {e}") from e
    else:
        # Fall back to the packaged builtin — the fresh source of truth, so
        # builtins run without a copy-and-fork step and never go stale.
        sql = _read_builtin_query(name)
        if sql is None:
            raise QueryError(f"Query file not found: {sql_file}")
    variables = variables or {}

    # 1. Extract :param names before $var substitution
    # Match :var but not ::var (Postgres cast) or :=var (assignment)
    param_pattern = re.compile(r"(?<!:):(\w+)\b(?!=)")
    param_names = set(param_pattern.findall(sql))

    # 2. Text-substitute $var / ${var}
    sql = Template(sql).safe_substitute(variables)

    # 3. Check for unsubstituted $vars
    remaining_template = re.findall(r"\$\{(\w+)\}|\$(\w+)", sql)
    if remaining_template:
        missing = sorted(set(m[0] or m[1] for m in remaining_template))
        raise QueryError(f"Missing template variables: {', '.join(missing)}")

    # 4. Build params dict for :var (only those present in SQL)
    params = {k: v for k, v in variables.items() if k in param_names}

    # 5. Check for unbound :params
    unbound = param_names - set(params.keys())
    if unbound:
        raise QueryError(f"Missing parameter variables: {', '.join(sorted(unbound))}")

    # 6. Execute with params (query_only prevents accidental mutation)
    conn = open_database(db, read_only=False)
    conn.execute("PRAGMA query_only = ON")

    try:
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        last_rows = None
        for stmt in statements:
            # Pass params to each statement (sqlite3 uses :name syntax)
            cursor = conn.execute(stmt, params)
            if cursor.description:
                last_rows = (cursor.description, cursor.fetchall())

        if last_rows:
            desc, rows = last_rows
            columns = [d[0] for d in desc]
            row_data = [
                [v if v is not None else None for v in row] for row in rows
            ]
            return QueryResult(columns=columns, rows=row_data)
        else:
            return QueryResult(columns=[], rows=[])

    except sqlite3.Error as e:
        raise QueryError(f"SQL error: {e}") from e
    finally:
        conn.close()


def get_recent_conversation_ids(
    conn: sqlite3.Connection,
    limit: int,
    *,
    owner: str | None = None,
) -> list[str]:
    """Get IDs of the most recent conversations.

    Args:
        conn: Database connection.
        limit: Number of conversations to return.

    Returns:
        List of conversation IDs, most recent first.
    """
    if owner and not has_conversation_owners_table(conn):
        return []

    where: list[str] = []
    params: list[object] = []
    if owner:
        where.append(owner_predicate("c.id"))
        params.append(owner)

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT c.id FROM conversations c{where_sql} ORDER BY c.started_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [row["id"] for row in rows]


def resolve_entity_id(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    *,
    owner: str | None = None,
) -> str | None:
    """Resolve an entity ID, supporting prefix match for conversations.

    For the 'conversation' entity type, raises AmbiguousPrefix if the given
    prefix matches more than one conversation. Callers at the CLI boundary
    should catch AmbiguousPrefix and exit with code 2.

    Args:
        conn: Database connection.
        entity_type: One of 'conversation', 'workspace', 'tool_call', 'prompt',
            'response', 'exchange', or 'block'.
        entity_id: Full or prefix ID to look up.

    Returns:
        Resolved full ID, or None if not found.

    Raises:
        AmbiguousPrefix: If entity_type is 'conversation' and the prefix matches
            more than one row.
    """
    if entity_type == "conversation":
        if owner and not has_conversation_owners_table(conn):
            return None

        where = ["(c.id = ? OR c.id LIKE ?)"]
        params: list[object] = [entity_id, f"{entity_id}%"]
        if owner:
            where.append(owner_predicate("c.id"))
            params.append(owner)

        # Fetch at most 6 rows ordered by ID — 1 sentinel beyond the display cap.
        # Avoids loading all N rows for a collision with hundreds of matches.
        rows = conn.execute(
            f"SELECT c.id FROM conversations c WHERE {' AND '.join(where)} ORDER BY c.id LIMIT 6",
            params,
        ).fetchall()
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]["id"]
        # Ambiguous: COUNT separately to get the exact total without fetching all rows.
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM conversations c WHERE {' AND '.join(where)}",
            params,
        ).fetchone()
        raise AmbiguousPrefix(entity_id, [r["id"] for r in rows[:5]], count_row["n"])
    elif entity_type == "workspace":
        # Owner scope is participation (any owned conversation in the workspace),
        # matching the workspace-pin guard's semantics. A non-participant gets
        # None (404-shaped), not an error — existence isn't leaked.
        if owner and not has_conversation_owners_table(conn):
            return None
        where = ["w.id = ?"]
        ws_params: list[object] = [entity_id]
        join = ""
        if owner:
            join = " JOIN conversations c ON c.workspace_id = w.id"
            where.append(owner_predicate("c.id"))
            ws_params.append(owner)
        row = conn.execute(
            f"SELECT DISTINCT w.id FROM workspaces w{join} WHERE {' AND '.join(where)}",
            ws_params,
        ).fetchone()
        return row["id"] if row else None
    elif entity_type in ("tool_call", "prompt", "response", "exchange"):
        # Prefix-match across event kinds so `siftd query <event_prefix>` /
        # `siftd tag <kind> <prefix>` work like conversation IDs. Mirror the
        # conversation branch: a colliding prefix must raise AmbiguousPrefix,
        # not silently resolve to an arbitrary match. ('exchange' anchors on a
        # prompt event.)
        #
        # Owner-scoped through the owning conversation (mirrors the conversation
        # branch and TargetRef._resolve_cross_kind): an owner-scoped caller must
        # not resolve — and then tag — another tenant's event by ULID/prefix.
        # This completes the resolver's owner safety so the serve /tag route can
        # trust resolve() on the kind-narrowed path, not just the bare-id one.
        kind = "prompt" if entity_type == "exchange" else entity_type
        if owner and not has_conversation_owners_table(conn):
            return None
        where = ["(e.id = ? OR e.id LIKE ?)", "e.kind = ?"]
        evt_params: list[object] = [entity_id, f"{entity_id}%", kind]
        if owner:
            where.append(owner_predicate("e.conversation_id"))
            evt_params.append(owner)
        where_sql = " AND ".join(where)
        rows = conn.execute(
            f"SELECT e.id FROM events e WHERE {where_sql} ORDER BY e.id LIMIT 6",
            evt_params,
        ).fetchall()
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]["id"]
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM events e WHERE {where_sql}",
            evt_params,
        ).fetchone()
        raise AmbiguousPrefix(entity_id, [r["id"] for r in rows[:5]], count_row["n"])
    elif entity_type == "block":
        # Content-block ids (event_content.id), owner-scoped through the owning
        # event's conversation — mirrors the events branch (WS8).
        if owner and not has_conversation_owners_table(conn):
            return None
        where = ["(ec.id = ? OR ec.id LIKE ?)"]
        blk_params: list[object] = [entity_id, f"{entity_id}%"]
        join = ""
        if owner:
            join = " JOIN events e ON e.id = ec.event_id"
            where.append(owner_predicate("e.conversation_id"))
            blk_params.append(owner)
        where_sql = " AND ".join(where)
        rows = conn.execute(
            f"SELECT ec.id FROM event_content ec{join} WHERE {where_sql} ORDER BY ec.id LIMIT 6",
            blk_params,
        ).fetchall()
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]["id"]
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM event_content ec{join} WHERE {where_sql}",
            blk_params,
        ).fetchone()
        raise AmbiguousPrefix(entity_id, [r["id"] for r in rows[:5]], count_row["n"])
    else:
        return None


def get_conversation_metadata(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> dict | None:
    """Fetch workspace and started_at for a fully-resolved conversation ID.

    Delegates to the storage primitive, which uses `c.id = ? OR c.id LIKE ?`.
    For a full ID (already resolved via resolve_entity_id), the equality arm
    matches first and the LIKE expansion is never reached — behavior is
    effectively exact-match. This wrapper exists because the CLI layer cannot
    import storage directly (architecture boundary).

    Returns:
        Dict with keys 'id', 'workspace', 'started_at', or None if not found.
    """
    return fetch_conversation_by_id_or_prefix(conn, conversation_id)
