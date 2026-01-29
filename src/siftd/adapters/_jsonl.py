"""Shared JSONL adapter utilities.

Common functions used by JSONL-based adapters (claude_code, codex_cli).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from siftd.domain import ContentBlock


def load_jsonl(path: Path) -> list[dict]:
    """Load JSONL file, returning a list of parsed records."""
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


def now_iso() -> str:
    """ISO timestamp for now (UTC)."""
    return datetime.now(UTC).isoformat()


def parse_block(block) -> ContentBlock:
    """Parse content block into a ContentBlock domain object."""
    if isinstance(block, str):
        return ContentBlock(block_type="text", content={"text": block})
    block_type = block.get("type", "unknown")
    return ContentBlock(block_type=block_type, content=block)
