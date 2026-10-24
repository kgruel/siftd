# Writing Adapters

Adapters are drop-in modules that read log files from coding assistants and turn them into siftd's `Conversation` domain objects. An adapter only *decodes*: it says how its source is read and what each raw unit means, and siftd does the rest. This guide covers the adapter interface (version 2, which new adapters use) and common patterns; [the legacy `parse()` interface](#legacy-interface-version-1) still loads and is described last.

## Quick Start

Copy `src/siftd/adapters/template.py` (`siftd copy adapter template`) to `~/.config/siftd/adapters/my_harness.py` and customize:

```python
NAME = "my_harness"
DEFAULT_LOCATIONS = ["~/.my_harness/logs"]
HARNESS_SOURCE = "openai"  # provider name
```

then change the decoder to match your log format. Run `siftd ingest` — drop-in adapters are auto-discovered. The template is a working version-2 adapter for a tiny made-up JSONL format; it is tested to load, validate and decode a fixture.

## Required Exports

Every adapter must export these, in every version:

| Name | Type | Description |
|------|------|-------------|
| `ADAPTER_INTERFACE_VERSION` | `int` | `2` (decoding). `1` is the legacy `parse()` interface |
| `NAME` | `str` | Unique adapter identifier |
| `DEFAULT_LOCATIONS` | `list[str]` | Paths to scan (~ expanded) |
| `DEDUP_STRATEGY` | `str` | `"file"` or `"session"` |
| `HARNESS_SOURCE` | `str` | Provider name |
| `discover(locations=None)` | callable | Find log sources |
| `can_handle(source)` | callable | Check if adapter handles source |

and, for version 2:

| Name | Type | Description |
|------|------|-------------|
| `read(source)` | callable | Yield each raw unit of the source (see [Decoding to records](#decoding-to-records)) |
| `decoder(source)` | callable | A `Decoder` for the source: an object with `decode(unit) -> list[Record]`, or a class whose constructor takes the source |

`siftd.adapters.validation` is the one definition of this list (`required_names(version)`); the registry, `siftd doctor` and the test suite read it.

## Optional Hooks

All optional, and validated for type when present, so a misspelled value is an error instead of a silently missing feature.

| Name | Type | Description |
|------|------|-------------|
| `SUPPORT_TIER` | `str` | `"core"`, `"contrib"` (default) or `"frozen"` |
| `HARNESS_LOG_FORMAT` | `str` | `"jsonl"`, `"json"`, `"markdown"`, ... |
| `HARNESS_DISPLAY_NAME` | `str` | Human-readable name; defaults to `NAME.replace("_", " ").title()` |
| `TOOL_ALIASES` | `dict[str, str]` | Raw tool name to canonical name (see [Tool Aliases](#tool-aliases)) |
| `default_session(source)` | callable | The `Session` of a source whose records state none. Without it a source with no `Session` record fails assembly |
| `change_paths(source)` | callable | The files whose change means the source changed. A WAL-mode database keeps recent commits in a `-wal` file, so OpenCode names both. Default: the source's own file |
| `SUPPORTS_LIVE_REGISTRATION` | `bool` | Tagging during a live session is supported; ingest drains queued session tags for this adapter |
| `SUBAGENT_PATH_MARKER` | `str` | Path fragment that identifies a subagent log (Claude Code: `"/subagents/"`) |
| `PEEK_GLOB_PATTERNS` | `list[str]` | Globs `siftd peek` scans under `DEFAULT_LOCATIONS`; otherwise inferred from `HARNESS_LOG_FORMAT` |
| `SESSION_ID_IN_PATH` | `bool` | False when a session's id never appears in its file path, so peek lookup scans every file for it (aider) |
| `TOOL_HINT_KEYS` | `dict[str, list[str]]` | Canonical tool name to the input keys that summarize a call, in priority order |
| `SUPPORTS_PEEK` | `bool` | False when peek must not be derived (OpenCode: one database holds many sessions, and peek shows one file as one conversation). Default True |
| `PEEK_LAST_OF_MANY` | `bool` | A source that holds several conversations peeks as its last, the live one (aider's history file) |
| `peek_read(source)` | callable | A reader for peek when it must differ from `read` (VSCode reads a half-written session leniently) |
| `peek_enrich(path, scan)` | callable | Adds to the derived scan what the log alone cannot say (Claude Code reads a subagent's sidecar `.meta.json`) |
| `peek_scan`, `peek_exchanges`, `peek_tail` | callables | An explicit hook replaces the derived one (aider's markdown has no records to tail). See [Peek](#peek-hooks) |

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
  history file accumulates every session for a project)

Ingest decides *which* sessions moved from `ended_at`: a re-parsed session
replaces the stored copy when its `ended_at` is newer, and an unchanged one is
left alone with its tags and ownership. A session that reports **no**
`ended_at` is read as still open, so it is replaced on every content change —
which is correct for a live session and wasteful for a finished one. Emit an
`ended_at` for every session your format can bound.

`ended_at` cannot see every change: a message stamped before the session's last
update leaves the end where it was. A session can therefore also state a
`content.revision` attribute (`records.CONTENT_REVISION_ATTRIBUTE`), a digest of
its source rows. Ingest replaces the stored copy when the revision differs and
both copies came from the same source path, whatever the end time says. It names
that path itself as `content.source` (`records.CONTENT_SOURCE_ATTRIBUTE`). A
digest orders nothing, so a copy from another source (a backup beside the live
file) competes on `ended_at` alone: an older copy cannot overwrite a newer one.
OpenCode does this.

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

## Decoding to records

An adapter's only tool-specific job is decoding. A reader yields raw units (a
JSONL line, a JSON message, a database row); a decoder turns each unit into
the canonical records in `siftd.adapters.records`; the shared assembler in
`siftd.adapters.assembly` does the rest, once, for every adapter: it decides
where each record attaches, merges streamed chunks of one message, pairs tool
results with their calls in either order, places a response that precedes
every prompt in `Conversation.preamble`, and counts every record in a ledger.
The adapter declares the reader and the decoder, and siftd derives `parse()`
from them (`siftd.adapters.contract`):

```python
from siftd.adapters.assembly import read_jsonl
from siftd.adapters.records import Ignored, PromptRec, ResponseRec, Session, ToolCallRec, ToolResultRec

class MyDecoder:
    def __init__(self, source):  # state is allowed, but only decoding state
        ...

    def decode(self, unit: dict) -> list:
        ...  # one unit in, the records it means out

    def finish(self) -> list:  # optional: what only the end of the stream settles
        return []

read = read_jsonl      # a unit it cannot parse is yielded as Malformed, never skipped
decoder = MyDecoder    # called with the source; a function returning a decoder works too

def default_session(source):  # optional
    return Session(external_id=f"{NAME}::{source.as_path.stem}")
```

What siftd supplies from those declarations, and where a consumer gets it: the
registry hands every consumer an adapter with `parse(source)` (the conversations),
`assemble(source)` (the conversations *and* the ledger, from one parse),
`HARNESS` (built from `NAME`, `HARNESS_SOURCE`, `HARNESS_LOG_FORMAT` and
`HARNESS_DISPLAY_NAME`) and the peek hooks. A bare module has none of these; call
`siftd.adapters.contract.adapt(module)` to get them.

The ledger counts every record by its disposition. `siftd ingest` adds the
`UNKNOWN` (not recognized), `ORPHAN` (could not be placed) and `IGNORED` (skipped
on purpose) counts to `IngestStats.by_harness` and reports an adapter's
unrecognized and orphaned counts when they are not zero, so a new record type in a
tool's log surfaces instead of vanishing. The ledger is not stored in the database.

The rules a decoder follows:

- **Classify every unit.** Return `Ignored(reason=...)` for a unit that is
  deliberately not stored, and never return nothing: an empty return is
  counted `Unknown`, which is how a new record type in the tool's log shows up
  instead of vanishing. Say why each ignored type is ignored in the decoder's
  docstring.
- **Decode, don't decide structure.** Do not look for "the current prompt" or
  invent one for a response that arrives first; emit `PromptRec`,
  `ResponseRec`, `ToolCallRec` and `ToolResultRec` and let the assembler place
  them. A message identity (`key=`) merges chunks that describe one message;
  usage on a response is a snapshot of the message's total, and a `UsageRec`
  is an increment.
- **Dates are evidence.** Give a record `at=` only when its time belongs in
  the conversation's span; a bookkeeping record that lands hours later gets
  `at=None`.

`claude_code.ClaudeDecoder` is the worked example, including the table that
names what every record type in the tool's log becomes.

A container source (`DEDUP_STRATEGY = "session"`) is also a row-level reader:
`opencode.read` yields each session, message and part row, and
`opencode.OpencodeDecoder` decodes them. Where a database keeps recent commits
in a write-ahead log, the adapter declares its change files with an optional
`change_paths(source)` returning the paths whose change means the source may
have changed (`opencode.change_paths` names `opencode.db` and `opencode.db-wal`).
Without it, the source's own file is the whole change signal.

## Timestamps

`started_at` and `ended_at` must be ISO 8601 strings in UTC:

```python
# Good
started_at = "2025-01-15T14:32:01Z"
started_at = "2025-01-15T14:32:01.123456+00:00"

# Also acceptable (local time without zone)
started_at = "2025-01-15T14:32:01"
```

A legacy `parse()` can use the SDK helper:

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

## Peek Hooks

Peek hooks enable live session inspection via `siftd peek` without ingesting into SQLite.

### Derived from the decoder

A version-2 adapter needs to write nothing for peek: `peek_scan`,
`peek_exchanges` and `peek_tail` are derived from `read` and `decoder`, so peek
and ingest classify each record once. Peek assembles the whole file, then
projects the conversation, so a session peek shows is a session ingest would
store. A file that yields several conversations is an error for peek, not a
guess, unless the adapter says which one is live (`PEEK_LAST_OF_MANY = True`). A
subagent is shown as `<parent>:<agent>`, derived from its
`LineageRec(kind="subagent", ...)`. Tool names are canonicalized through
`TOOL_ALIASES`.

Three declarations refine the derivation, and an explicit hook of the same name
replaces it:

- `peek_read(source)` is the reader peek uses when it must differ from `read`.
- `peek_enrich(path, scan)` returns the derived scan with more in it.
- `SUPPORTS_PEEK = False` turns peek off for the adapter.
- A module-level `peek_scan(path)`, `peek_exchanges(path, last_n)` or
  `peek_tail(path, lines)` is used as written.

The shapes of those hooks, for an adapter that writes one:

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

### Graceful Degradation

A version-1 adapter without peek hooks, and a version-2 adapter that sets
`SUPPORTS_PEEK = False`, are still handled:
- Sessions from these adapters are still discovered (via `DEFAULT_LOCATIONS`)
- They appear in listings with `preview_available=False`
- Detail view shows "(preview unavailable)"

This allows partial peek support across a mixed adapter ecosystem.

## SDK Helpers

Import from `siftd.adapters.sdk`. `discover_files` and `build_harness` serve every
adapter. The rest (`timestamp_bounds`, `load_jsonl`, `ToolCallLinker`) are what a
legacy `parse()` builds its own tree with, and leave with version 1
(`V1_INTERFACE_REMOVAL` in `siftd/api/compat.py`). A decoding adapter does not need
them, because siftd builds the harness and the assembler places records, pairs tool
results and bounds the timestamps.

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

## Legacy interface (version 1)

An adapter that sets `ADAPTER_INTERFACE_VERSION = 1` and exports `parse(source)`
instead of `read` and `decoder` keeps loading and ingesting unchanged. Drop-ins
written before version 2 are version 1, and siftd does not convert them: a
conversation tree built by hand would give a ledger that measures nothing, so a
version-1 adapter has no ledger and `siftd ingest` has no unrecognized or
orphaned counts to report for it.

```python
def parse(source):
    """Yield Conversation objects: build the Prompt -> Response -> ToolCall tree yourself."""
    ...
```

A version-1 adapter's peek comes from its `normalize_record` (siftd derives `peek_scan` and
`peek_exchanges` from it), or from hooks it writes itself with the record folds in
`siftd.adapters.sdk`:

```python
from siftd.adapters.sdk import make_peek_hooks, NormalizedRecord

def _normalize(record: dict) -> NormalizedRecord | None:
    ...  # map your log's record shape to a NormalizedRecord

peek_scan, peek_exchanges, peek_tail = make_peek_hooks(
    _normalize,
    tool_aliases=TOOL_ALIASES,
    subagent_path_marker="/subagents/",  # optional
)
```

Moving to version 2 is a rewrite of `parse()` as a decoder, not a flag: delete the
tree-building, return a record per unit, and let the assembler place them.
No built-in is on version 1; a drop-in on it keeps loading until 1.0.0.

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
