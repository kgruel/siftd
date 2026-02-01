"""Binary content detection and filtering.

Filters binary content (images, PDFs, base64 data) during ingestion to reduce
database size without losing searchable value. Binary content is replaced with
metadata-only placeholders that preserve context.

Detection patterns:
1. Anthropic API image/document blocks with base64 data
2. Generic base64 content in strings (500+ chars)
3. Binary file magic bytes (SQLite, PNG, PDF, JPEG, GIF)
"""

import re
from typing import Any

# Pattern for detecting base64 content (500+ chars to avoid JWT/hash false positives)
BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{500,}={0,2}")

# Magic bytes for common binary formats
BINARY_SIGNATURES = [
    b"SQLite format 3",
    b"\x89PNG",
    b"%PDF",
    b"GIF87a",
    b"GIF89a",
    b"\xff\xd8\xff",  # JPEG
]


def is_base64_image_block(block: dict) -> bool:
    """Check if block is an Anthropic API image/document with base64 data.

    Matches blocks like:
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", ...}}
    """
    if not isinstance(block, dict):
        return False
    block_type = block.get("type")
    if block_type not in ("image", "document"):
        return False
    source = block.get("source")
    if not isinstance(source, dict):
        return False
    return source.get("type") == "base64"


def is_binary_content(content: str) -> bool:
    """Check if string content appears to be binary data.

    Checks for:
    1. Null bytes in first 1000 chars (strong binary signal)
    2. Known binary magic bytes at start
    """
    if not isinstance(content, str):
        return False
    # Null bytes are strong signal
    if "\x00" in content[:1000]:
        return True
    # Check magic bytes
    # Use latin-1 encoding for 1-to-1 byte mapping (handles raw binary in strings)
    try:
        content_bytes = content[:50].encode("latin-1", errors="ignore")
    except (UnicodeDecodeError, AttributeError):
        return False
    return any(content_bytes.startswith(sig) for sig in BINARY_SIGNATURES)


def has_large_base64(content: str) -> bool:
    """Check if content contains large base64 strings (500+ chars).

    Used to detect embedded base64 data in tool results.
    """
    if not isinstance(content, str):
        return False
    return bool(BASE64_PATTERN.search(content))


def filter_binary_block(block: dict) -> dict:
    """Replace binary content in block with metadata placeholder.

    For image/document blocks with base64 data, replaces the data field with
    metadata preserving the media type and original size. Preserves all other
    fields from the original block (e.g., cache_control).

    Returns the original block unchanged if not binary.
    """
    if not is_base64_image_block(block):
        return block

    source = block.get("source", {})
    original_data = source.get("data", "")

    # Start with a copy of all non-source fields from the original block
    result = {k: v for k, v in block.items() if k != "source"}

    # Replace source with filtered version
    result["source"] = {
        "type": "filtered",
        "original_type": source.get("type", "base64"),
        "media_type": source.get("media_type"),
        "original_size": len(original_data),
        "filtered_reason": "binary_content",
    }

    return result


def filter_tool_result_binary(result: Any) -> Any:
    """Filter binary content from tool result.

    Handles Anthropic API content block structures:
    1. Dict with "content" list containing image/document blocks
    2. Dict with "content" string that is binary
    3. Dict with "content" string containing large base64

    Note: Only processes the "content" key. Binary data in other keys
    (e.g., "data", "stdout") is not filtered. This matches the Anthropic
    API structure where binary content appears in content blocks.

    Returns modified result with binary content replaced by placeholders.
    """
    if not isinstance(result, dict):
        return result

    content = result.get("content")
    if content is None:
        return result

    # Case 1: content is a list of blocks
    if isinstance(content, list):
        filtered_content = []
        for item in content:
            if isinstance(item, dict):
                filtered_content.append(filter_binary_block(item))
            else:
                filtered_content.append(item)

        # Only create new dict if something changed
        if filtered_content != content:
            new_result = result.copy()
            new_result["content"] = filtered_content
            return new_result
        return result

    # Case 2 & 3: content is a string
    if isinstance(content, str):
        # Check for binary file data
        if is_binary_content(content):
            new_result = result.copy()
            new_result["content"] = "[binary content filtered]"
            new_result["original_size"] = len(content)
            new_result["filtered_reason"] = "binary_content"
            return new_result

        # Check for large base64 strings
        if has_large_base64(content):
            new_result = result.copy()
            new_result["content"] = "[base64 content filtered]"
            new_result["original_size"] = len(content)
            new_result["filtered_reason"] = "base64_content"
            return new_result

    return result
