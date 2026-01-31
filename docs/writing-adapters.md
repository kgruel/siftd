# Writing Adapters

Adapters are drop-in modules that parse log files from coding assistants and yield `Conversation` domain objects. This guide covers the adapter interface and common patterns.

## Quick Start

Copy `src/siftd/adapters/template.py` to `~/.config/siftd/adapters/my_harness.py` and customize:

```python
NAME = "my_harness"
DEFAULT_LOCATIONS = ["~/.my_harness/logs"]
HARNESS_SOURCE = "openai"  # provider name
```

Then run `siftd ingest` — drop-in adapters are auto-discovered.

## Required Exports

Every adapter must export:

| Name | Type | Description |
|------|------|-------------|
| `ADAPTER_INTERFACE_VERSION` | `int` | Must be `1` |
| `NAME` | `str` | Unique adapter identifier |
| `DEFAULT_LOCATIONS` | `list[str]` | Paths to scan (~ expanded) |
| `DEDUP_STRATEGY` | `str` | `"file"` or `"session"` |
| `HARNESS_SOURCE` | `str` | Provider name |
| `discover(locations=None)` | callable | Find log sources |
| `can_handle(source)` | callable | Check if adapter handles source |
| `parse(source)` | callable | Parse source into conversations |

## Deduplication Strategy

### `file` (most common)
One conversation per file. Each file is a distinct source:

```python
DEDUP_STRATEGY = "file"
external_id = f"{NAME}::{path.stem}"  # file-based ID
```

Use when:
- Each log file represents a single session
- Files are append-only or immutable
- Examples: Claude Code, Codex CLI

### `session`
Multiple files may update the same conversation. Latest wins:

```python
DEDUP_STRATEGY = "session"
external_id = f"{NAME}::{session_id}"  # session-based ID
```

Use when:
- Conversations can span multiple files
- Re-ingesting should update, not duplicate
- The harness exports session IDs
- Example: Gemini CLI (multiple chats per project hash)

## External ID

`external_id` is the stable, unique identifier for a conversation. It must:

1. **Be deterministic** — same input always produces same ID
2. **Be unique** — no collisions between different conversations
3. **Be stable** — doesn't change if file is re-parsed

Common patterns:

```python
# File-based (DEDUP_STRATEGY=file)
external_id = f"{NAME}::{path.stem}"

# Session-based (DEDUP_STRATEGY=session)
external_id = f"{NAME}::{session_id}"

# With sub-sessions (e.g., Claude Code agents)
external_id = f"{NAME}::{session_id}::agent::{agent_id}"
```

## Timestamps

`started_at` and `ended_at` must be ISO 8601 strings in UTC:

```python
# Good
started_at = "2025-01-15T14:32:01Z"
started_at = "2025-01-15T14:32:01.123456+00:00"

# Also acceptable (local time without zone)
started_at = "2025-01-15T14:32:01"
```

Use the SDK helper:

```python
from siftd.adapters.sdk import timestamp_bounds
started_at, ended_at = timestamp_bounds(records)
```

## Harness Metadata

### `HARNESS_SOURCE`
Provider or vendor name:
- `"anthropic"` — Claude
- `"openai"` — GPT, Codex
- `"google"` — Gemini
- `"multi"` — Multiple providers (e.g., Aider)

### `HARNESS_LOG_FORMAT` (optional)
Log format identifier:
- `"jsonl"` — JSON Lines
- `"json"` — Single JSON document
- `"markdown"` — Markdown-based logs

### `HARNESS_DISPLAY_NAME` (optional)
Human-readable name shown in UI. Defaults to `NAME.replace("_", " ").title()`.

## Tool Aliases

Map raw tool names from logs to canonical names:

```python
TOOL_ALIASES: dict[str, str] = {
    "Read": "file.read",
    "Write": "file.write",
    "Bash": "shell.execute",
    "search_files": "search.grep",
}
```

Canonical names use dot notation: `category.action`.

Common categories:
- `file.*` — file operations (read, write, edit, glob)
- `shell.*` — shell commands
- `search.*` — search operations (grep, web)
- `ui.*` — user interaction (ask, todo)
- `task.*` — task/agent management

Tool aliases enable cross-harness analysis (e.g., "all file reads").

## Peek Hooks (Optional)

Peek hooks enable live session inspection via `siftd peek` without ingesting into SQLite. These are **optional** — adapters without peek hooks will still work for ingest, but their sessions will show "preview unavailable" in peek listings.

### Hook Functions

Export these functions to support peek:

```python
from pathlib import Path
from typing import Iterator
from siftd.peek.types import PeekExchange, PeekScanResult

def peek_scan(path: Path) -> PeekScanResult | None:
    """Extract lightweight metadata for session listing.

    Called per-file during list_active_sessions().
    Return None if file can't be parsed or has no exchanges.
    """
    ...

def peek_exchanges(path: Path, last_n: int = 5) -> list[PeekExchange]:
    """Extract recent exchanges for session detail view.

    Called by read_session_detail().
    Should return the last N user→assistant exchanges.
    """
    ...

def peek_tail(path: Path, lines: int = 20) -> Iterator[dict]:
    """Yield last N raw records from the session file.

    Called by tail_session().
    For JSONL files, should seek from end for efficiency.
    """
    ...
```

### PeekScanResult Fields

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `str` | Canonical ID (adapter decides: file stem or in-record ID) |
| `workspace_path` | `str \| None` | Working directory / project path |
| `model` | `str \| None` | Last model used |
| `exchange_count` | `int` | Number of user turns (real prompts, not tool_results) |
| `started_at` | `str \| None` | Earliest timestamp |
| `last_activity_at` | `str \| None` | Latest timestamp (prefer over mtime) |

### PeekExchange Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `str \| None` | Exchange timestamp |
| `prompt_text` | `str \| None` | User prompt text |
| `response_text` | `str \| None` | Assistant response text |
| `tool_calls` | `list[tuple[str, int]]` | (tool_name, count) pairs |
| `input_tokens` | `int` | Input token count |
| `output_tokens` | `int` | Output token count |

### SDK Helpers for Peek

```python
from siftd.adapters.sdk import (
    seek_last_lines,        # Efficient tail read
    peek_jsonl_scan,        # Generic JSONL scanner
    peek_jsonl_exchanges,   # Generic JSONL exchange extractor
    peek_jsonl_tail,        # Generic JSONL tail
    canonicalize_tool_name, # Apply TOOL_ALIASES
    extract_text_with_placeholders,  # Text + [image]/[tool] markers
)

# Example: Claude Code-compatible JSONL
def peek_scan(path: Path) -> PeekScanResult | None:
    return peek_jsonl_scan(
        path,
        user_type="user",
        assistant_type="assistant",
        cwd_key="cwd",
        session_id_key="sessionId",
        is_tool_result=lambda r: _has_tool_result(r),
    )
```

### Graceful Degradation

Adapters without peek hooks are automatically handled:
- Sessions from these adapters are still discovered (via `DEFAULT_LOCATIONS`)
- They appear in listings with `preview_available=False`
- Detail view shows "(preview unavailable)"

This allows partial peek support across a mixed adapter ecosystem.

## SDK Helpers

Import from `siftd.adapters.sdk`:

### `discover_files(locations, default_locations, glob_patterns)`
Standard file discovery with glob patterns:

```python
def discover(locations=None):
    yield from discover_files(
        locations,
        DEFAULT_LOCATIONS,
        ["**/*.jsonl", "*.json"],
    )
```

### `build_harness(name, source, log_format, display_name=None)`
Construct `Harness` with defaults:

```python
harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT)
```

### `timestamp_bounds(records, key="timestamp")`
Extract min/max timestamps from records:

```python
started_at, ended_at = timestamp_bounds(records)
```

### `load_jsonl(path)`
Load JSONL with line-numbered errors:

```python
records, errors = load_jsonl(path)
for e in errors:
    print(f"Line {e.line_number}: {e.error}")
```

### `ToolCallLinker`
Pair tool_use with tool_result by ID:

```python
linker = ToolCallLinker()

# In assistant message
linker.add_use(block.id, name=block.name, input=block.input)

# In user message (tool result)
linker.add_result(block.tool_use_id, content=block.content)

# After processing
for tool_id, use_data, result_data in linker.get_pairs():
    # Build ToolCall objects
```

## Installation Methods

### Drop-in (simplest)
Place `.py` file in `~/.config/siftd/adapters/`:

```
~/.config/siftd/adapters/my_harness.py
```

### Entry point (for packages)
Register in `pyproject.toml`:

```toml
[project.entry-points."siftd.adapters"]
my_harness = "my_package.adapters:my_harness"
```

## Debugging

Run ingest with verbose output:

```bash
siftd ingest --path ~/.my_harness/logs -v
```

Check adapter discovery:

```bash
siftd adapters list
```
