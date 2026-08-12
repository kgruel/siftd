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
The source is a *container* of conversations. Each is deduped independently by
`external_id`, and a re-parsed session replaces the stored copy when it has
moved:

```python
DEDUP_STRATEGY = "session"
external_id = f"{NAME}::{session_id}"  # session-based ID
```

Use when:
- One source holds many sessions, or grows new ones over its life
- Re-ingesting should update, not duplicate
- The harness exports session IDs
- Examples: OpenCode (many sessions per SQLite database), aider (one markdown
  history file accumulates every session for a project), Gemini CLI (one
  conversation per chat JSON, re-exported as the session goes on)

Ingest decides *which* sessions moved from `ended_at`: a re-parsed session
replaces the stored copy when its `ended_at` is newer, and an unchanged one is
left alone with its tags and ownership. A session that reports **no**
`ended_at` is read as still open, so it is replaced on every content change —
which is correct for a live session and wasteful for a finished one. Emit an
`ended_at` for every session your format can bound.

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

### `SUPPORT_TIER` (optional)
Support tier: `"core"`, `"contrib"`, or `"frozen"`. Defaults to `"contrib"`,
which is the right value for drop-in adapters — see
[Support tiers](../concepts/adapters.md#support-tiers) for what each tier means.

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
    make_peek_hooks,             # peek_scan/peek_exchanges/peek_tail from a normalizer
    peek_scan_from_records,      # Lower-level: scan normalized records
    peek_exchanges_from_records, # Lower-level: extract exchanges
    peek_jsonl_tail,             # Generic JSONL tail
    canonicalize_tool_name,      # Apply TOOL_ALIASES
    extract_text_with_placeholders,  # Text + [image]/[tool] markers
)

# Most adapters: write one record normalizer, derive all three hooks.
def _normalize(record: dict) -> NormalizedRecord | None:
    ...  # map your log's record shape to a NormalizedRecord

peek_scan, peek_exchanges, peek_tail = make_peek_hooks(
    _normalize,
    tool_aliases=TOOL_ALIASES,
    subagent_path_marker="/subagents/",  # optional
)
```

The `*_from_records` functions are there when a hook needs behavior
`make_peek_hooks` doesn't cover (custom session ids, non-JSONL iteration via
`record_iterator`).

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

### Disabling

Any installed adapter can be switched off without removing it:

```bash
siftd config set adapters.my_harness.enabled false
```

Disabled adapters are skipped by ingest, peek, and doctor. See
[Adapters — Disabling an adapter](../concepts/adapters.md#disabling-an-adapter).

## Debugging

Run ingest with verbose output:

```bash
siftd ingest --path ~/.my_harness/logs -v
```

Check adapter discovery:

```bash
siftd adapters list
```
