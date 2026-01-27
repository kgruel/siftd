"""Shared tag query helpers."""


def tag_condition(tag_value: str) -> tuple[str, str]:
    """Return (SQL operator, param) for a tag value with prefix support.

    Supports prefix matching: a trailing colon (e.g., 'research:') matches
    all tags starting with that prefix via LIKE. Otherwise uses exact match.
    """
    if tag_value.endswith(":"):
        return "tg.name LIKE ?", f"{tag_value}%"
    return "tg.name = ?", tag_value
