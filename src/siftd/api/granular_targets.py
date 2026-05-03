"""Colon-path target resolution for granular tagging.

Resolves <conv_ref>:<kind>:<n> → (target_kind, target_id) for the tag CLI and serve layer.
"""

import sqlite3

GRANULAR_KINDS = frozenset({"prompt", "response", "tool_call", "exchange"})

# Maps CLI kind names to the events.kind value used to query
_KIND_TO_EVENT_KIND: dict[str, str] = {
    "prompt": "prompt",
    "response": "response",
    "tool_call": "tool_call",
    # exchange anchors on the n-th prompt event; target_kind is 'exchange'
    "exchange": "prompt",
}


def resolve_colon_target(
    conn: sqlite3.Connection,
    conversation_id: str,
    kind: str,
    n: int,
) -> tuple[str, str]:
    """Resolve a granular target reference to (target_kind, target_id).

    Args:
        conn: Database connection (must already be open).
        conversation_id: Fully resolved conversation ULID.
        kind: One of 'prompt', 'response', 'tool_call', 'exchange'.
        n: 1-indexed position within the conversation, ordered by timestamp.

    Returns:
        (target_kind, target_id) — target_kind is 'exchange' for kind='exchange',
        otherwise matches kind. target_id is the event's ULID.

    Raises:
        ValueError: If kind is not a supported granular kind.
        IndexError: If n is out of range for the conversation.
    """
    if kind not in GRANULAR_KINDS:
        valid = ", ".join(sorted(GRANULAR_KINDS))
        raise ValueError(f"Invalid target kind {kind!r}. Valid: {valid}")

    if n < 1:
        raise ValueError(f"Index must be >= 1, got {n}")

    event_kind = _KIND_TO_EVENT_KIND[kind]
    row = conn.execute(
        "SELECT id FROM events "
        "WHERE conversation_id = ? AND kind = ? "
        "ORDER BY timestamp, id "
        "LIMIT 1 OFFSET ?",
        (conversation_id, event_kind, n - 1),
    ).fetchone()

    if row is None:
        raise IndexError(f"No {kind} at index {n} in conversation {conversation_id[:12]}")

    target_kind = "exchange" if kind == "exchange" else kind
    return target_kind, row["id"]


def parse_colon_path(raw: str) -> tuple[str, str, int] | None:
    """Parse a colon-path string into (conv_ref, kind, n).

    Returns None if the string does not match the colon-path pattern
    (exactly two colons, last segment is an integer).

    Examples:
        '01HX...:prompt:1' → ('01HX...', 'prompt', 1)
        '01HX...:exchange:3' → ('01HX...', 'exchange', 3)
        '01HX...' → None (no colons)
    """
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    conv_ref, kind, n_str = parts
    if not conv_ref or not kind:
        return None
    try:
        n = int(n_str)
    except ValueError:
        return None
    if n < 1:
        return None
    return conv_ref, kind, n
