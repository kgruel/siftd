# API Reference

_Auto-generated from source code._

## Overview

The `siftd.api` module provides programmatic access to siftd functionality.
CLI commands are thin wrappers over these functions.

```python
from siftd import api
```

## Adapters

### Data Types

### AdapterInfo

Information about a discovered adapter.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `source` | `str` |  |
| `locations` | `list[str]` |  |
| `file_path` | `Union[str, None]` |  |

### Functions

### list_adapters

List all discovered adapters from all sources.

```python
def list_adapters(*, dropin_path: Union[Path, None] = ...) -> list[AdapterInfo]
```

**Returns:** List of AdapterInfo for all discovered adapters.

### list_builtin_adapters

Return names of built-in adapters (for copy command).

```python
def list_builtin_adapters() -> list[str]
```

**Returns:** List of adapter names that can be copied.

## Doctor

### Data Types

### CheckInfo

Metadata about an available check.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `description` | `str` |  |
| `has_fix` | `bool` |  |

### Finding

A single issue detected by a check.

| Field | Type | Description |
|-------|------|-------------|
| `check` | `str` |  |
| `severity` | `str` |  |
| `message` | `str` |  |
| `fix_available` | `bool` |  |
| `fix_command` | `Union[str, None]` |  |
| `context` | `Union[dict, None]` |  |

### FixResult

Result of applying a fix.

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` |  |
| `message` | `str` |  |

### Functions

### apply_fix

Apply fix for a finding (if available).

```python
def apply_fix(finding: Finding) -> FixResult
```

**Returns:** FixResult indicating success/failure.

### list_checks

Return metadata about all available checks.

```python
def list_checks() -> list[CheckInfo]
```

### run_checks

Run health checks and return findings.

```python
def run_checks(*, checks: Union[list[str], None] = ..., db_path: Union[Path, None] = ..., embed_db_path: Union[Path, None] = ...) -> list[Finding]
```

**Parameters:**

- `checks`: Specific check names to run, or None for all.
- `db_path`: Main database path. Uses default if not specified.

**Returns:** List of Finding objects from all checks.

**Raises:**

- `FileNotFoundError`: If the main database doesn't exist.
- `ValueError`: If a specified check name doesn't exist.

## Peek

### Data Types

### PeekExchange

PeekExchange(timestamp: str | None = None, prompt_text: str | None = None, response_text: str | None = None, tool_calls: list[tuple[str, int]] = <factory>, input_tokens: int = 0, output_tokens: int = 0)

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `Union[str, None]` |  |
| `prompt_text` | `Union[str, None]` |  |
| `response_text` | `Union[str, None]` |  |
| `tool_calls` | `list[tuple[str, int]]` |  |
| `input_tokens` | `int` |  |
| `output_tokens` | `int` |  |

### SessionDetail

SessionDetail(info: siftd.peek.scanner.SessionInfo, started_at: str | None = None, exchanges: list[siftd.peek.reader.PeekExchange] = <factory>)

| Field | Type | Description |
|-------|------|-------------|
| `info` | `SessionInfo` |  |
| `started_at` | `Union[str, None]` |  |
| `exchanges` | `list[PeekExchange]` |  |

### SessionInfo

SessionInfo(session_id: str, file_path: pathlib.Path, workspace_path: str | None = None, workspace_name: str | None = None, model: str | None = None, last_activity: float = 0.0, exchange_count: int = 0)

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `str` |  |
| `file_path` | `Path` |  |
| `workspace_path` | `Union[str, None]` |  |
| `workspace_name` | `Union[str, None]` |  |
| `model` | `Union[str, None]` |  |
| `last_activity` | `float` |  |
| `exchange_count` | `int` |  |

### Functions

### find_session_file

Find a session file by ID prefix match.

```python
def find_session_file(session_id_prefix: str) -> Union[Path, None]
```

**Returns:** Path to the matching file, or None if not found.

### list_active_sessions

Discover active session files and extract lightweight metadata.

```python
def list_active_sessions(*, workspace: Union[str, None] = ..., threshold_seconds: int = ..., include_inactive: bool = ...) -> list[SessionInfo]
```

**Parameters:**

- `workspace`: Filter by workspace name substring.
- `threshold_seconds`: Only include files modified within this many seconds. Default is 7200 (2 hours).

**Returns:** List of SessionInfo sorted by last_activity (most recent first).

### read_session_detail

Read session detail from a JSONL file.

```python
def read_session_detail(path: Path, *, last_n: int = ...) -> Union[SessionDetail, None]
```

**Parameters:**

- `path`: Path to the JSONL session file.

**Returns:** SessionDetail or None if the file can't be read.

### tail_session

Read and format the last N lines of a session file.

```python
def tail_session(path: Path, *, lines: int = ...) -> list[str]
```

**Parameters:**

- `path`: Path to the JSONL session file.

**Returns:** List of formatted JSON strings (pretty-printed single records).

## Conversations

### Data Types

### ConversationSummary

Summary row for conversation listing.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` |  |
| `workspace_path` | `Union[str, None]` |  |
| `model` | `Union[str, None]` |  |
| `started_at` | `Union[str, None]` |  |
| `prompt_count` | `int` |  |
| `response_count` | `int` |  |
| `total_tokens` | `int` |  |
| `cost` | `Union[float, None]` |  |
| `tags` | `list[str]` |  |

### ConversationDetail

Full conversation with timeline.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` |  |
| `workspace_path` | `Union[str, None]` |  |
| `model` | `Union[str, None]` |  |
| `started_at` | `Union[str, None]` |  |
| `total_input_tokens` | `int` |  |
| `total_output_tokens` | `int` |  |
| `exchanges` | `list[Exchange]` |  |
| `tags` | `list[str]` |  |

### Exchange

A prompt-response pair in the timeline.

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `Union[str, None]` |  |
| `prompt_text` | `Union[str, None]` |  |
| `response_text` | `Union[str, None]` |  |
| `input_tokens` | `int` |  |
| `output_tokens` | `int` |  |
| `tool_calls` | `list[ToolCallSummary]` |  |

### ToolCallSummary

Collapsed tool call for timeline display.

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | `str` |  |
| `status` | `str` |  |
| `count` | `int` |  |

### Functions

### list_conversations

List conversations with optional filtering.

```python
def list_conversations(*, db_path: Union[Path, None] = ..., workspace: Union[str, None] = ..., model: Union[str, None] = ..., since: Union[str, None] = ..., before: Union[str, None] = ..., search: Union[str, None] = ..., tool: Union[str, None] = ..., tag: Union[str, None] = ..., tags: Union[list[str], None] = ..., all_tags: Union[list[str], None] = ..., exclude_tags: Union[list[str], None] = ..., tool_tag: Union[str, None] = ..., limit: int = ..., oldest_first: bool = ...) -> list[ConversationSummary]
```

**Parameters:**

- `db_path`: Path to database. Uses default if not specified.
- `workspace`: Filter by workspace path substring.
- `model`: Filter by model name substring.
- `since`: Filter conversations started after this date (ISO format).
- `before`: Filter conversations started before this date.
- `search`: FTS5 full-text search query.
- `tool`: Filter by canonical tool name (e.g., 'shell.execute').
- `tag`: Filter by tag name (single, backward compat — prefer tags).
- `tags`: OR filter — conversations with any of these tags.
- `all_tags`: AND filter — conversations with all of these tags.
- `exclude_tags`: NOT filter — exclude conversations with any of these tags.
- `tool_tag`: Filter by tool call tag (e.g., 'shell:test').
- `limit`: Maximum results to return (0 = unlimited).

**Returns:** List of ConversationSummary objects.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### get_conversation

Get full conversation detail by ID.

```python
def get_conversation(conversation_id: str, *, db_path: Union[Path, None] = ...) -> Union[ConversationDetail, None]
```

**Parameters:**

- `conversation_id`: Full or prefix of conversation ULID.

**Returns:** ConversationDetail with timeline, or None if not found.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### list_query_files

List available user-defined SQL query files.

```python
def list_query_files() -> list[QueryFile]
```

**Returns:** List of QueryFile with name, path, and required variables.

### run_query_file

Run a user-defined SQL query file.

```python
def run_query_file(name: str, variables: Union[dict[str, str], None] = ..., *, db_path: Union[Path, None] = ...) -> QueryResult
```

**Parameters:**

- `name`: Query file name (without .sql extension).
- `variables`: Dict of variable values. Same dict serves both syntaxes.

**Returns:** QueryResult with columns and rows.

**Raises:**

- `FileNotFoundError`: If database or query file doesn't exist.

## Query Files

### Data Types

### QueryFile

Metadata about a user-defined SQL query file.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Query file stem (without .sql extension). |
| `path` | `Path` | Full path to the .sql file. |
| `template_vars` | `list[str]` | Variables using $var syntax (text substitution). |
| `param_vars` | `list[str]` | Variables using :var syntax (parameterized, safe). |

### QueryResult

Result of running a SQL query file.

| Field | Type | Description |
|-------|------|-------------|
| `columns` | `list[str]` |  |
| `rows` | `list[list]` |  |

### Exceptions

#### QueryError

Error running a SQL query file.

## File Refs

### Data Types

### FileRef

A file operation reference from a tool call.

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` |  |
| `basename` | `str` |  |
| `op` | `str` |  |
| `content` | `Union[str, None]` |  |

### Functions

### fetch_file_refs

Batch query: prompt_ids → file references from tool calls.

```python
def fetch_file_refs(conn: Connection, source_ids: list[str]) -> dict[str, list[FileRef]]
```

**Parameters:**

- `conn`: Database connection with row_factory set.

**Returns:** Dict mapping prompt_id to list of FileRef for file.read/write/edit calls.

## Resources

### Exceptions

#### CopyError

Error copying a resource.

### Functions

### copy_adapter

Copy a built-in adapter to the config directory for customization.

```python
def copy_adapter(name: str, *, dest_dir: Union[Path, None] = ..., force: bool = ...) -> Path
```

**Parameters:**

- `name`: Adapter name (e.g., "claude_code").
- `dest_dir`: Destination directory. Uses default adapters_dir if not specified.

**Returns:** Path to the copied file.

**Raises:**

- `CopyError`: If adapter not found, file exists (without force), or copy fails.

### copy_query

Copy a built-in query to the config directory for customization.

```python
def copy_query(name: str, *, dest_dir: Union[Path, None] = ..., force: bool = ...) -> Path
```

**Parameters:**

- `name`: Query name without .sql extension (e.g., "cost").
- `dest_dir`: Destination directory. Uses default queries_dir if not specified.

**Returns:** Path to the copied file.

**Raises:**

- `CopyError`: If query not found, file exists (without force), or copy fails.

### list_builtin_queries

Return names of built-in queries (for copy command).

```python
def list_builtin_queries() -> list[str]
```

**Returns:** List of query names that can be copied.

## Search

### Data Types

### SearchResult

A single search result from hybrid_search.

| Field | Type | Description |
|-------|------|-------------|
| `conversation_id` | `str` |  |
| `score` | `float` |  |
| `text` | `str` |  |
| `chunk_type` | `str` |  |
| `workspace_path` | `Union[str, None]` |  |
| `started_at` | `Union[str, None]` |  |
| `chunk_id` | `Union[str, None]` |  |
| `source_ids` | `Union[list[str], None]` |  |

### ConversationScore

Aggregated conversation-level search result.

| Field | Type | Description |
|-------|------|-------------|
| `conversation_id` | `str` |  |
| `max_score` | `float` |  |
| `mean_score` | `float` |  |
| `chunk_count` | `int` |  |
| `best_excerpt` | `str` |  |
| `workspace_path` | `Union[str, None]` |  |
| `started_at` | `Union[str, None]` |  |

### Functions

### hybrid_search

Run hybrid FTS5+embeddings search, return structured results.

```python
def hybrid_search(query: str, *, db_path: Union[Path, None] = ..., embed_db_path: Union[Path, None] = ..., limit: int = ..., recall: int = ..., embeddings_only: bool = ..., workspace: Union[str, None] = ..., model: Union[str, None] = ..., since: Union[str, None] = ..., before: Union[str, None] = ..., backend: Union[str, None] = ..., exclude_active: bool = ..., rerank: str = ..., lambda_: float = ...) -> list[SearchResult]
```

**Parameters:**

- `query`: The search query string.
- `db_path`: Path to main SQLite DB. Defaults to XDG data path.
- `embed_db_path`: Path to embeddings DB. Defaults to XDG data path.
- `limit`: Maximum number of results to return.
- `recall`: Number of FTS5 candidate conversations for hybrid recall.
- `embeddings_only`: Skip FTS5 recall, search all embeddings directly.
- `workspace`: Filter to conversations from workspaces matching this substring.
- `model`: Filter to conversations using models matching this substring.
- `since`: Filter to conversations started at or after this ISO date.
- `before`: Filter to conversations started before this ISO date.
- `backend`: Preferred embedding backend name (ollama, fastembed).
- `exclude_active`: Auto-exclude conversations from active sessions (default True).
- `rerank`: Reranking strategy — "mmr" for diversity or "relevance" for pure similarity.

**Returns:** List of SearchResult ordered by reranking strategy.

**Raises:**

- `FileNotFoundError`: If the database files don't exist.
- `RuntimeError`: If no embedding backend is available.
- `EmbeddingsNotAvailable`: If embedding dependencies are not installed.

### aggregate_by_conversation

Aggregate chunk results to conversation-level scores.

```python
def aggregate_by_conversation(results: list[SearchResult], *, limit: int = ...) -> list[ConversationScore]
```

**Parameters:**

- `results`: List of SearchResult from hybrid_search.

**Returns:** List of ConversationScore, sorted by max_score descending.

### first_mention

Find chronologically earliest result above relevance threshold.

```python
def first_mention(results: Union[list[SearchResult], list[dict]], *, threshold: float = ..., db_path: Union[Path, None] = ...) -> Union[SearchResult, dict, None]
```

**Parameters:**

- `results`: List of SearchResult or raw dicts from search. Dicts must have 'score', 'conversation_id', and optionally 'chunk_id'.
- `threshold`: Minimum score to consider relevant.

**Returns:** Earliest result above threshold (same type as input), or None if none qualify.

### build_index

Build or update the embeddings index.

```python
def build_index(*, db_path: Union[Path, None] = ..., embed_db_path: Union[Path, None] = ..., rebuild: bool = ..., backend: Union[str, None] = ..., verbose: bool = ...) -> dict
```

**Parameters:**

- `db_path`: Path to main database. Uses default if not specified.
- `embed_db_path`: Path to embeddings database. Uses default if not specified.
- `rebuild`: If True, clear and rebuild from scratch.
- `backend`: Preferred embedding backend name.

**Returns:** Dict with 'chunks_added' and 'total_chunks' counts.

**Raises:**

- `FileNotFoundError`: If main database doesn't exist.
- `RuntimeError`: If no embedding backend is available.
- `EmbeddingsNotAvailable`: If embedding dependencies are not installed.

## Stats

### Data Types

### DatabaseStats

Complete database statistics.

| Field | Type | Description |
|-------|------|-------------|
| `db_path` | `Path` |  |
| `db_size_bytes` | `int` |  |
| `counts` | `TableCounts` |  |
| `harnesses` | `list[HarnessInfo]` |  |
| `top_workspaces` | `list[WorkspaceStats]` |  |
| `models` | `list[str]` |  |
| `top_tools` | `list[ToolStats]` |  |

### TableCounts

Row counts for core tables.

| Field | Type | Description |
|-------|------|-------------|
| `conversations` | `int` |  |
| `prompts` | `int` |  |
| `responses` | `int` |  |
| `tool_calls` | `int` |  |
| `harnesses` | `int` |  |
| `workspaces` | `int` |  |
| `tools` | `int` |  |
| `models` | `int` |  |
| `ingested_files` | `int` |  |

### HarnessInfo

Harness metadata.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `source` | `Union[str, None]` |  |
| `log_format` | `Union[str, None]` |  |

### WorkspaceStats

Workspace with conversation count.

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` |  |
| `conversation_count` | `int` |  |

### ToolStats

Tool with usage count.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `usage_count` | `int` |  |

### Functions

### get_stats

Get comprehensive database statistics.

```python
def get_stats(*, db_path: Union[Path, None] = ...) -> DatabaseStats
```

**Returns:** DatabaseStats with counts, harnesses, workspaces, models, tools.

**Raises:**

- `FileNotFoundError`: If database does not exist.

## Tools

### Data Types

### TagUsage

Tag with usage count.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `count` | `int` |  |

### WorkspaceTagUsage

Per-workspace breakdown of tool tag usage.

| Field | Type | Description |
|-------|------|-------------|
| `workspace` | `str` |  |
| `tags` | `list[TagUsage]` |  |
| `total` | `int` |  |

### Functions

### get_tool_tag_summary

Get summary of tool call tags by category.

```python
def get_tool_tag_summary(*, db_path: Union[Path, None] = ..., prefix: str = ...) -> list[TagUsage]
```

**Parameters:**

- `db_path`: Path to database. Uses default if not specified.

**Returns:** List of TagUsage sorted by count descending.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### get_tool_tags_by_workspace

Get tool tag usage broken down by workspace.

```python
def get_tool_tags_by_workspace(*, db_path: Union[Path, None] = ..., prefix: str = ..., limit: int = ...) -> list[WorkspaceTagUsage]
```

**Parameters:**

- `db_path`: Path to database. Uses default if not specified.
- `prefix`: Tag prefix to filter by (default: "shell:").

**Returns:** List of WorkspaceTagUsage sorted by total count descending.

**Raises:**

- `FileNotFoundError`: If database does not exist.

## Export

### Data Types

### ExportedConversation

A conversation prepared for export.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` |  |
| `workspace_path` | `Union[str, None]` |  |
| `workspace_name` | `Union[str, None]` |  |
| `model` | `Union[str, None]` |  |
| `started_at` | `Union[str, None]` |  |
| `exchanges` | `list[Exchange]` |  |
| `tags` | `list[str]` |  |
| `total_tokens` | `int` |  |

### ExportOptions

Options controlling export output.

| Field | Type | Description |
|-------|------|-------------|
| `format` | `str` |  |
| `prompts_only` | `bool` |  |
| `no_header` | `bool` |  |

### Functions

### export_conversations

Export conversations matching the specified criteria.

```python
def export_conversations(*, conversation_ids: Union[list[str], None] = ..., last: Union[int, None] = ..., workspace: Union[str, None] = ..., tags: Union[list[str], None] = ..., exclude_tags: Union[list[str], None] = ..., since: Union[str, None] = ..., before: Union[str, None] = ..., search: Union[str, None] = ..., db_path: Union[Path, None] = ...) -> list[ExportedConversation]
```

**Parameters:**

- `conversation_ids`: Specific conversation IDs to export (prefix match).
- `last`: Export the N most recent conversations.
- `workspace`: Filter by workspace path substring.
- `tags`: Include only conversations with any of these tags.
- `exclude_tags`: Exclude conversations with any of these tags.
- `since`: Conversations started after this date.
- `before`: Conversations started before this date.
- `search`: FTS5 full-text search filter.

**Returns:** List of ExportedConversation objects with full exchange data.

**Raises:**

- `FileNotFoundError`: If database does not exist.
- `ValueError`: If no conversations match criteria.

### format_export

Format conversations according to export options.

```python
def format_export(conversations: list[ExportedConversation], options: ExportOptions) -> str
```

**Parameters:**

- `conversations`: List of exported conversations.

**Returns:** Formatted string (markdown or JSON).

### format_exchanges

Format conversations as prompt-response exchanges.

```python
def format_exchanges(conversations: list[ExportedConversation], *, prompts_only: bool = ..., no_header: bool = ...) -> str
```

**Parameters:**

- `conversations`: List of exported conversations.
- `prompts_only`: If True, omit response text and tool calls.

**Returns:** Markdown string with exchanges.

### format_json

Format conversations as JSON.

```python
def format_json(conversations: list[ExportedConversation], *, prompts_only: bool = ...) -> str
```

**Parameters:**

- `conversations`: List of exported conversations.

**Returns:** JSON string with structured conversation data.

### format_prompts

Format conversations as prompts-only markdown.

```python
def format_prompts(conversations: list[ExportedConversation], *, no_header: bool = ...) -> str
```

**Parameters:**

- `conversations`: List of exported conversations.

**Returns:** Markdown string with numbered prompts.
