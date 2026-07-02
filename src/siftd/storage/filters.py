"""Dynamic WHERE clause builder for conversation filters."""


# Canonical tag target-kind vocabulary. Every site that enumerates kinds (per-kind
# query arms, validation gates, merge/slice copies) derives from these sets instead
# of restating them — adding a kind means editing here and following the type errors.
#
# EVENT_TAG_KINDS: kinds whose target_id is an events.id (ownership/conversation
# joins go through events). 'exchange' anchors on its prompt event.
EVENT_TAG_KINDS: frozenset[str] = frozenset({"prompt", "response", "tool_call", "exchange"})
# ELEMENT_TAG_KINDS: all sub-conversation kinds. 'block' targets an event_content.id,
# so its joins descend event_content → events (one hop more than EVENT_TAG_KINDS).
ELEMENT_TAG_KINDS: frozenset[str] = EVENT_TAG_KINDS | {"block"}
# ALL_TAG_KINDS: every valid tag_assignments.target_kind.
ALL_TAG_KINDS: frozenset[str] = ELEMENT_TAG_KINDS | {"conversation", "workspace"}

# Tag target_kinds whose target_id resolves to a conversation (directly or via events).
# 'workspace' is excluded — workspace tags don't map to a single conversation.
ALL_CONVERSATION_TAG_KINDS: tuple[str, ...] = ("conversation", *sorted(ELEMENT_TAG_KINDS))


def tag_condition(tag_value: str) -> tuple[str, str]:
    """Return (SQL fragment, param) for a tag value with optional prefix match.

    Trailing colon (e.g. 'research:') matches via LIKE; otherwise exact match.
    """
    if tag_value.endswith(":"):
        return "tg.name LIKE ?", f"{tag_value}%"
    return "tg.name = ?", tag_value


def _normalize_kinds(kinds: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Return the effective kinds tuple, defaulting to all conversation-bearing kinds.

    Unknown kinds (e.g. 'workspace') are filtered out to keep the subquery
    well-formed; an empty result short-circuits to no-match downstream.
    """
    if not kinds:
        return ALL_CONVERSATION_TAG_KINDS
    return tuple(k for k in kinds if k in ALL_CONVERSATION_TAG_KINDS)


def _tag_target_subquery(allowed_kinds: tuple[str, ...], tag_clause: str) -> str:
    """Build a subquery that yields conversation IDs for matched tag rows.

    Resolves the polymorphic target_kind: rows tagged on a conversation
    contribute their target_id directly; rows tagged on an event
    (prompt/response/tool_call/exchange) join through events.conversation_id.
    """
    placeholders = ", ".join("?" * len(allowed_kinds))
    return (
        "SELECT CASE ta.target_kind"
        " WHEN 'conversation' THEN ta.target_id"
        " WHEN 'block' THEN eb.conversation_id"
        " ELSE e.conversation_id"
        " END AS conv_id"
        " FROM tag_assignments ta"
        " JOIN tags tg ON tg.id = ta.tag_id"
        " LEFT JOIN events e ON e.id = ta.target_id"
        " AND ta.target_kind IN ('prompt','response','tool_call','exchange')"
        " LEFT JOIN event_content ec ON ec.id = ta.target_id AND ta.target_kind = 'block'"
        " LEFT JOIN events eb ON eb.id = ec.event_id"
        f" WHERE ta.target_kind IN ({placeholders})"
        f" AND ({tag_clause})"
    )


# Available JOIN clauses, keyed by alias.
# Phase-1 ID queries include only the joins their filters actually need.
JOINS: dict[str, str] = {
    "w": "LEFT JOIN workspaces w ON w.id = c.workspace_id",
}

# Dependency edges (none remaining after responses join removal).
_JOIN_DEPS: dict[str, list[str]] = {}


class WhereBuilder:
    """Accumulates WHERE conditions and params for conversation queries.

    Handles the filter patterns shared by list_conversations and
    filter_conversations: workspace, model, date range, and tag booleans.

    Tracks which JOINs are needed so callers can build minimal queries.
    """

    def __init__(self) -> None:
        self.conditions: list[str] = []
        self.params: list[str] = []
        self._joins: set[str] = set()

    def add(self, condition: str, *params: str) -> None:
        """Append a raw condition with positional params."""
        self.conditions.append(condition)
        self.params.extend(params)

    def require_join(self, *aliases: str) -> None:
        """Declare that one or more table aliases are needed.

        Transitive dependencies (e.g. m → r) are resolved automatically.
        """
        for alias in aliases:
            self._joins.add(alias)
            for dep in _JOIN_DEPS.get(alias, []):
                self._joins.add(dep)

    # -- common filter patterns --

    def workspace(self, value: str | None) -> None:
        if value:
            self.require_join("w")
            self.add("(w.path LIKE ? OR w.git_remote LIKE ?)", f"%{value}%", f"%{value}%")

    def workspace_id(self, value: str | None) -> None:
        """Filter by exact workspace ULID.

        Distinct from :meth:`workspace` (a path/remote substring match):
        ``workspace_id`` matches the ``conversations.workspace_id`` column
        exactly, so a substring like ``/foo`` cannot bleed in conversations
        from ``/foo-bar``. No join needed — ``workspace_id`` is a column on
        ``conversations c``.
        """
        if value:
            self.add("c.workspace_id = ?", value)

    def model(self, value: str | None) -> None:
        if value:
            self.add(
                "EXISTS (SELECT 1 FROM events e_m"
                " JOIN event_response er_m ON er_m.event_id = e_m.id"
                " JOIN models m ON m.id = er_m.model_id"
                " WHERE e_m.conversation_id = c.id AND e_m.kind = 'response'"
                " AND (m.raw_name LIKE ? OR m.name LIKE ?))",
                f"%{value}%",
                f"%{value}%",
            )

    def tool(self, value: str | None) -> None:
        """Filter to conversations with a tool_call event matching a canonical name.

        Conversation-level (any tool call in the conversation matches), like the
        other candidate filters — so it composes identically across the
        list/browse path and the search engine's candidate resolution.
        """
        if value:
            self.add(
                "c.id IN (SELECT e.conversation_id FROM events e"
                " JOIN event_tool_call etc ON etc.event_id = e.id"
                " JOIN tools t ON t.id = etc.tool_id"
                " WHERE e.kind = 'tool_call' AND t.name = ?)",
                value,
            )

    def tool_tag(self, value: str | None) -> None:
        """Filter to conversations with a tool_call tagged with this tag.

        Honors the trailing-colon prefix match (see :func:`tag_condition`),
        scoped to ``target_kind = 'tool_call'``.
        """
        if value:
            op, val = tag_condition(value)
            self.add(
                "c.id IN (SELECT e.conversation_id FROM tag_assignments ta"
                " JOIN events e ON e.id = ta.target_id"
                " JOIN tags tg ON tg.id = ta.tag_id"
                f" WHERE ta.target_kind = 'tool_call' AND {op})",
                val,
            )

    def since(self, value: str | None) -> None:
        if value:
            self.add("c.started_at >= ?", value)

    def before(self, value: str | None) -> None:
        if value:
            self.add("c.started_at < ?", value)

    def owner(self, value: str | None) -> None:
        """Filter to conversations owned by this user_id."""
        if value:
            self.add(
                "c.id IN (SELECT conversation_id FROM conversation_owners WHERE user_id = ?)",
                value,
            )

    def tags_any(
        self, tags: list[str] | None,
        *, kinds: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """OR semantics: conversation has ANY of these tags.

        Matches tags applied at any conversation-bearing target_kind
        (conversation, prompt, response, tool_call, exchange) by default.
        Pass `kinds=` to scope to specific target_kinds.
        """
        if not tags:
            return
        allowed = _normalize_kinds(kinds)
        if not allowed:
            self.conditions.append("0")
            return
        parts: list[str] = []
        clause_params: list[str] = []
        for t in tags:
            op, val = tag_condition(t)
            parts.append(op)
            clause_params.append(val)
        tag_clause = " OR ".join(parts)
        sub = _tag_target_subquery(allowed, tag_clause)
        self.conditions.append(f"c.id IN ({sub})")
        self.params.extend(allowed)
        self.params.extend(clause_params)

    def tags_all(
        self, tags: list[str] | None,
        *, kinds: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """AND semantics: conversation has ALL of these tags."""
        if not tags:
            return
        allowed = _normalize_kinds(kinds)
        if not allowed:
            self.conditions.append("0")
            return
        for t in tags:
            op, val = tag_condition(t)
            sub = _tag_target_subquery(allowed, op)
            self.conditions.append(f"c.id IN ({sub})")
            self.params.extend(allowed)
            self.params.append(val)

    def tags_none(
        self, tags: list[str] | None,
        *, kinds: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """NOT semantics: conversation has NONE of these tags."""
        if not tags:
            return
        allowed = _normalize_kinds(kinds)
        if not allowed:
            return
        parts: list[str] = []
        clause_params: list[str] = []
        for t in tags:
            op, val = tag_condition(t)
            parts.append(op)
            clause_params.append(val)
        tag_clause = " OR ".join(parts)
        sub = _tag_target_subquery(allowed, tag_clause)
        self.conditions.append(f"c.id NOT IN ({sub})")
        self.params.extend(allowed)
        self.params.extend(clause_params)

    def joins_sql(self) -> str:
        """Return JOIN clauses for all required tables, in dependency order."""
        ordered = [alias for alias in ("w",) if alias in self._joins]
        return "\n        ".join(JOINS[a] for a in ordered)

    def where_sql(self) -> str:
        """Return 'WHERE ...' string, or empty string if no conditions."""
        if not self.conditions:
            return ""
        return "WHERE " + " AND ".join(self.conditions)
