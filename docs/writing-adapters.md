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
