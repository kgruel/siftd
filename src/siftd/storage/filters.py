"""Dynamic WHERE clause builder for conversation filters."""


def tag_condition(tag_value: str) -> tuple[str, str]:
    """Return (SQL fragment, param) for a tag value with optional prefix match.

    Trailing colon (e.g. 'research:') matches via LIKE; otherwise exact match.
    """
    if tag_value.endswith(":"):
        return "tg.name LIKE ?", f"{tag_value}%"
    return "tg.name = ?", tag_value


# Available JOIN clauses, keyed by alias.
# Phase-1 ID queries include only the joins their filters actually need.
JOINS: dict[str, str] = {
    "w": "LEFT JOIN workspaces w ON w.id = c.workspace_id",
    "r": "LEFT JOIN responses r ON r.conversation_id = c.id",
    "m": "LEFT JOIN models m ON m.id = r.model_id",
}

# Dependency edges: requesting 'm' also requires 'r'.
_JOIN_DEPS: dict[str, list[str]] = {
    "m": ["r"],
}


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

    def tags_any(self, tags: list[str] | None) -> None:
        """OR semantics: conversation has ANY of these tags."""
        if not tags:
            return
        parts = []
        for t in tags:
            op, val = tag_condition(t)
            parts.append(op)
            self.params.append(val)
        clause = " OR ".join(parts)
        self.conditions.append(
            f"c.id IN (SELECT ct.conversation_id FROM conversation_tags ct"
            f" JOIN tags tg ON tg.id = ct.tag_id WHERE {clause})"
        )

    def tags_all(self, tags: list[str] | None) -> None:
        """AND semantics: conversation has ALL of these tags."""
        if not tags:
            return
        for t in tags:
            op, val = tag_condition(t)
            self.conditions.append(
                f"c.id IN (SELECT ct.conversation_id FROM conversation_tags ct"
                f" JOIN tags tg ON tg.id = ct.tag_id WHERE {op})"
            )
            self.params.append(val)

    def tags_none(self, tags: list[str] | None) -> None:
        """NOT semantics: conversation has NONE of these tags."""
        if not tags:
            return
        parts = []
        for t in tags:
            op, val = tag_condition(t)
            parts.append(op)
            self.params.append(val)
        clause = " OR ".join(parts)
        self.conditions.append(
            f"c.id NOT IN (SELECT ct.conversation_id FROM conversation_tags ct"
            f" JOIN tags tg ON tg.id = ct.tag_id WHERE {clause})"
        )

    def joins_sql(self) -> str:
        """Return JOIN clauses for all required tables, in dependency order."""
        # Stable order: w, r, m (respects FK dependencies)
        ordered = [alias for alias in ("w", "r", "m") if alias in self._joins]
        return "\n        ".join(JOINS[a] for a in ordered)

    @property
    def needs_group_by(self) -> bool:
        """True when JOINs introduce duplicates that require GROUP BY c.id."""
        return "r" in self._joins

    def where_sql(self) -> str:
        """Return 'WHERE ...' string, or empty string if no conditions."""
        if not self.conditions:
            return ""
        return "WHERE " + " AND ".join(self.conditions)
