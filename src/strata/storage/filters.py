"""Dynamic WHERE clause builder for conversation filters."""


def tag_condition(tag_value: str) -> tuple[str, str]:
    """Return (SQL fragment, param) for a tag value with optional prefix match.

    Trailing colon (e.g. 'research:') matches via LIKE; otherwise exact match.
    """
    if tag_value.endswith(":"):
        return "tg.name LIKE ?", f"{tag_value}%"
    return "tg.name = ?", tag_value


class WhereBuilder:
    """Accumulates WHERE conditions and params for conversation queries.

    Handles the filter patterns shared by list_conversations and
    filter_conversations: workspace, model, date range, and tag booleans.
    """

    def __init__(self) -> None:
        self.conditions: list[str] = []
        self.params: list[str] = []

    def add(self, condition: str, *params: str) -> None:
        """Append a raw condition with positional params."""
        self.conditions.append(condition)
        self.params.extend(params)

    # -- common filter patterns --

    def workspace(self, value: str | None) -> None:
        if value:
            self.add("w.path LIKE ?", f"%{value}%")

    def model(self, value: str | None) -> None:
        if value:
            self.add("(m.raw_name LIKE ? OR m.name LIKE ?)", f"%{value}%", f"%{value}%")

    def since(self, value: str | None) -> None:
        if value:
            self.add("c.started_at >= ?", value)

    def before(self, value: str | None) -> None:
        if value:
            self.add("c.started_at < ?", value)

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

    def where_sql(self) -> str:
        """Return 'WHERE ...' string, or empty string if no conditions."""
        if not self.conditions:
            return ""
        return "WHERE " + " AND ".join(self.conditions)
