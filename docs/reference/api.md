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

Extended adapter information for display/reporting.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `origin` | `str` |  |
| `locations` | `list[str]` |  |
| `source_path` | `str \| None` |  |
| `entrypoint` | `str \| None` |  |

### Functions

### list_adapters

List all discovered adapters from all sources.

```python
def list_adapters(*, dropin_path: pathlib._local.Path | None = ...) -> list[AdapterInfo]
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
| `requires_db` | `bool` |  |
| `requires_embed_db` | `bool` |  |
| `cost` | `Literal[fast, slow]` |  |

### Finding

A single issue detected by a check.

| Field | Type | Description |
|-------|------|-------------|
| `check` | `str` | Check name that produced this finding (e.g., "ingest-pending"). |
| `severity` | `str` | One of "info", "warning", or "error". |
| `message` | `str` | Human-readable description of the issue. |
| `fix_available` | `bool` | Whether a fix suggestion exists. |
| `fix_command` | `str \| None` | CLI command to fix the issue (advisory only, not executed automatically). User must run this command manually. |
| `context` | `dict \| None` | Optional structured data for programmatic consumers. |

### Functions

### list_checks

Return metadata about all available checks.

```python
def list_checks() -> list[CheckInfo]
```

### run_checks

Run health checks and return findings.

```python
def run_checks(*, checks: list[str] | None = ..., db_path: pathlib._local.Path | None = ..., embed_db_path: pathlib._local.Path | None = ..., on_check_done: object | None = ...) -> list[Finding]
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

A single user→assistant exchange for detail view.

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `str \| None` |  |
| `prompt_text` | `str \| None` |  |
| `response_text` | `str \| None` |  |
| `tool_calls` | `list[tuple[str, int]]` |  |
| `narrative` | `list[PeekNarrativeBlock]` |  |
| `input_tokens` | `int` |  |
| `output_tokens` | `int` |  |

### SessionDetail

Full session detail for detail view.

| Field | Type | Description |
|-------|------|-------------|
| `info` | `SessionInfo` |  |
| `started_at` | `str \| None` |  |
| `exchanges` | `list[PeekExchange]` |  |

### SessionInfo

Session metadata for list display.

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `str` |  |
| `file_path` | `Path` |  |
| `workspace_path` | `str \| None` |  |
| `workspace_name` | `str \| None` |  |
| `branch` | `str \| None` |  |
| `model` | `str \| None` |  |
| `last_activity` | `float` |  |
| `exchange_count` | `int` |  |
| `preview_available` | `bool` |  |
| `adapter_name` | `str \| None` |  |
| `parent_session_id` | `str \| None` |  |

### Functions

### find_session_file

Find a session file by ID prefix match.

```python
def find_session_file(session_id_prefix: str) -> pathlib._local.Path | None
```

**Returns:** Path to the matching file, or None if not found.

**Raises:**

- `AmbiguousSessionError`: If multiple files match the prefix.

### list_active_sessions

Discover active session files and extract lightweight metadata.

```python
def list_active_sessions(*, workspace: str | None = ..., branch: str | None = ..., threshold_seconds: int = ..., include_inactive: bool = ..., limit: int | None = ...) -> list[SessionInfo]
```

**Parameters:**

- `workspace`: Filter by workspace name substring.
- `branch`: Filter by worktree branch substring.
- `threshold_seconds`: Only include files modified within this many seconds. Default is 7200 (2 hours).
- `include_inactive`: If True, include all sessions regardless of mtime.

**Returns:** List of SessionInfo sorted by last_activity (most recent first).

### read_session_detail

Read session detail from a session file.

```python
def read_session_detail(path: Path, *, last_n: int = ..., include_thinking: bool = ...) -> siftd.domain.peek.SessionDetail | None
```

**Parameters:**

- `path`: Path to the session file.
- `last_n`: Number of most recent exchanges to include (minimum 1).

**Returns:** SessionDetail or None if the file can't be read.

### tail_session

Read and format the last N records of a session file.

```python
def tail_session(path: Path, *, lines: int = ..., raw: bool = ...) -> list[str]
```

**Parameters:**

- `path`: Path to the session file.
- `lines`: Number of records to return.

**Returns:** List of formatted strings — one per record.

## Conversations

### Data Types

### ConversationSummary

Summary row for conversation listing.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` |  |
| `workspace_path` | `str \| None` |  |
| `model` | `str \| None` |  |
| `started_at` | `str \| None` |  |
| `prompt_count` | `int` |  |
| `response_count` | `int` |  |
| `total_tokens` | `int` |  |
| `cost` | `float \| None` |  |
| `tags` | `list[str]` |  |
| `owner` | `str \| None` |  |

### ConversationDetail

Full conversation with timeline.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` |  |
| `workspace_path` | `str \| None` |  |
| `model` | `str \| None` |  |
| `started_at` | `str \| None` |  |
| `total_input_tokens` | `int` |  |
| `total_output_tokens` | `int` |  |
| `turns` | `list[Turn]` |  |
| `tags` | `list[str]` |  |

### Exchange

A prompt-response pair in the timeline.

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `str \| None` |  |
| `prompt_text` | `str \| None` |  |
| `response_text` | `str \| None` |  |
| `input_tokens` | `int` |  |
| `output_tokens` | `int` |  |
| `tool_calls` | `list[ToolCallSummary]` |  |

### NarrativeBlock

A single block in the response narrative.

| Field | Type | Description |
|-------|------|-------------|
| `block_type` | `str` |  |
| `content` | `str \| None` |  |
| `tool_calls` | `list[ToolCallDetail]` |  |

### ToolCallDetail

Tool call with optional input/result for --tools mode.

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | `str` |  |
| `status` | `str` |  |
| `count` | `int` |  |
| `input` | `str \| None` |  |
| `result` | `str \| None` |  |

### ToolCallSummary

Collapsed tool call for timeline display.

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | `str` |  |
| `status` | `str` |  |
| `count` | `int` |  |

### Turn

A prompt and its full response narrative.

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `str \| None` |  |
| `prompt_text` | `str \| None` |  |
| `total_input_tokens` | `int` |  |
| `total_output_tokens` | `int` |  |
| `narrative` | `list[NarrativeBlock]` |  |
| `_tool_call_summaries` | `list[ToolCallSummary]` |  |

### Functions

### get_recent_conversation_ids

Get IDs of the most recent conversations.

```python
def get_recent_conversation_ids(conn: Connection, limit: int, *, owner: str | None = ...) -> list[str]
```

**Parameters:**

- `conn`: Database connection.

**Returns:** List of conversation IDs, most recent first.

### list_conversations

List conversations with optional filtering.

```python
def list_conversations(*, db_path: pathlib._local.Path | None = ..., workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., search: str | None = ..., tool: str | None = ..., tag: str | list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., tool_tag: str | None = ..., n: int = ..., oldest: bool = ..., owner: str | None = ...) -> list[ConversationSummary]
```

**Parameters:**

- `db_path`: Path to database. Uses default if not specified.
- `workspace`: Filter by workspace path substring.
- `model`: Filter by model name substring.
- `since`: Filter conversations started after this date (ISO format).
- `before`: Filter conversations started before this date.
- `search`: FTS5 full-text search query.
- `tool`: Filter by canonical tool name (e.g., 'shell.execute').
- `tag`: OR filter — conversations with any of these tags. Also accepts a single string for backward compat.
- `all_tags`: AND filter — conversations with all of these tags.
- `no_tag`: NOT filter — exclude conversations with any of these tags.
- `tool_tag`: Filter by tool call tag (e.g., 'shell:test').
- `n`: Maximum results to return (0 = unlimited).
- `oldest`: Sort by oldest first instead of newest.

**Returns:** List of ConversationSummary objects.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### get_conversation

Get full conversation detail by ID.

```python
def get_conversation(id: str, *, db_path: pathlib._local.Path | None = ..., include_thinking: bool = ..., include_tool_content: bool = ..., tool_filter: str | None = ..., owner: str | None = ...) -> siftd.api.conversations.ConversationDetail | None
```

**Parameters:**

- `id`: Full or prefix of conversation ULID.
- `db_path`: Path to database. Uses default if not specified.
- `include_thinking`: Include thinking/reasoning blocks in turns.
- `include_tool_content`: Include tool input/result in turns.

**Returns:** ConversationDetail with timeline, or None if not found.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### resolve_entity_id

Resolve an entity ID, supporting prefix match for conversations.

```python
def resolve_entity_id(conn: Connection, entity_type: str, entity_id: str, *, owner: str | None = ...) -> str | None
```

**Parameters:**

- `conn`: Database connection.
- `entity_type`: One of 'conversation', 'workspace', 'tool_call'.

**Returns:** Resolved full ID, or None if not found.

### list_query_files

List available user-defined SQL query files.

```python
def list_query_files() -> list[QueryFile]
```

**Returns:** List of QueryFile with name, path, and required variables.

### run_query_file

Run a user-defined SQL query file.

```python
def run_query_file(name: str, variables: dict[str, str] | None = ..., *, db_path: pathlib._local.Path | None = ...) -> QueryResult
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
| `content` | `str \| None` |  |

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
def copy_adapter(name: str, *, dest_dir: pathlib._local.Path | None = ..., force: bool = ...) -> Path
```

**Parameters:**

- `name`: Adapter name (e.g., "claude_code").
- `dest_dir`: Destination directory. Uses default adapters_dir if not specified.

**Returns:** Path to the copied file.

**Raises:**

- `CopyError`: If adapter not found, file exists (without force), or copy fails.

### copy_formatter

Copy a built-in formatter to the config directory for customization.

```python
def copy_formatter(name: str, *, dest_dir: pathlib._local.Path | None = ..., force: bool = ...) -> Path
```

**Parameters:**

- `name`: Formatter name (e.g., "terminal", "markdown", "json").
- `dest_dir`: Destination directory. Uses default formatters_dir if not specified.

**Returns:** Path to the copied file.

**Raises:**

- `CopyError`: If formatter not found, file exists (without force), or copy fails.

### copy_query

Copy a built-in query to the config directory for customization.

```python
def copy_query(name: str, *, dest_dir: pathlib._local.Path | None = ..., force: bool = ...) -> Path
```

**Parameters:**

- `name`: Query name without .sql extension (e.g., "cost").
- `dest_dir`: Destination directory. Uses default queries_dir if not specified.

**Returns:** Path to the copied file.

**Raises:**

- `CopyError`: If query not found, file exists (without force), or copy fails.

### list_builtin_formatters

Return names of built-in formatters (for copy command).

```python
def list_builtin_formatters() -> list[str]
```

### list_builtin_queries

Return names of built-in queries (for copy command).

```python
def list_builtin_queries() -> list[str]
```

**Returns:** List of query names that can be copied.

## Search

### Data Types

### SearchResult

Canonical mutable search chunk result.

| Field | Type | Description |
|-------|------|-------------|
| `conversation_id` | `str` |  |
| `score` | `float` |  |
| `text` | `str` |  |
| `chunk_type` | `str` |  |
| `workspace_path` | `str \| None` |  |
| `started_at` | `str \| None` |  |
| `chunk_id` | `str \| None` |  |
| `source_ids` | `list[str]` |  |
| `breakdown` | `siftd.domain.search_types.ScoreBreakdown \| None` |  |
| `file_refs` | `list[Any] \| None` |  |
| `exchanges` | `list[tuple[str, str, str]] \| None` |  |
| `context_window` | `list[tuple[str, str, str, bool]] \| None` |  |

### ConversationScore

Conversation-level aggregate derived from chunk results.

| Field | Type | Description |
|-------|------|-------------|
| `conversation_id` | `str` |  |
| `max_score` | `float` |  |
| `mean_score` | `float` |  |
| `chunk_count` | `int` |  |
| `best_excerpt` | `str` |  |
| `workspace_path` | `str \| None` |  |
| `started_at` | `str \| None` |  |
| `file_refs` | `list[Any] \| None` |  |

### ToolSearchGroup

Conversation-level grouping for tool-search presentation.

| Field | Type | Description |
|-------|------|-------------|
| `conversation_id` | `str` |  |
| `workspace_path` | `str \| None` |  |
| `first_timestamp` | `str \| None` |  |
| `last_timestamp` | `str \| None` |  |
| `tool_call_count` | `int` |  |
| `tool_names` | `list[str]` |  |
| `results` | `list[ToolSearchResult]` |  |

### ToolSearchResult

Single tool-call search result.

| Field | Type | Description |
|-------|------|-------------|
| `tool_call_id` | `str` |  |
| `conversation_id` | `str` |  |
| `response_id` | `str` |  |
| `timestamp` | `str \| None` |  |
| `tool_name` | `str \| None` |  |
| `tool_family` | `str \| None` |  |
| `status` | `str \| None` |  |
| `path` | `str \| None` |  |
| `basename` | `str \| None` |  |
| `ext` | `str \| None` |  |
| `command` | `str \| None` |  |
| `command_verb` | `str \| None` |  |
| `pattern` | `str \| None` |  |
| `arg` | `str \| None` |  |
| `result_snippet` | `str \| None` |  |
| `workspace_path` | `str \| None` |  |
| `rank` | `float \| None` |  |

### Functions

### hybrid_search

Unified search pipeline — FTS5, semantic, or hybrid.

```python
def hybrid_search(q: str, *, db_path: Path, embed_db: pathlib._local.Path | None = ..., n: int = ..., mode: str = ..., workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., tag: list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., exclude_active: bool = ..., include_derivative: bool = ..., owner: str | None = ..., recall: int = ..., rerank: str = ..., lambda_: float = ..., recency: bool = ..., recency_half_life: float = ..., recency_max_boost: float = ..., threshold: float = ..., backend: str | None = ..., embed_backend: siftd.api.search.EmbeddingBackend | None = ...) -> list[SearchChunk]
```

**Parameters:**

- `q`: Search query string.
- `db_path`: Path to main database.
- `embed_db`: Path to embeddings database. Required for hybrid/semantic modes.
- `n`: Desired result count after all processing.
- `mode`: "hybrid" (FTS5 + semantic), "fts" (keyword only), "semantic" (embeddings only).
- `rerank`: "mmr" for diversity reranking, "relevance" for pure score order.
- `backend`: Preferred embedding backend name (ollama, fastembed).

**Returns:** List of SearchChunk results.

**Raises:**

- `FileNotFoundError`: If database doesn't exist.
- `ValueError`: If query is empty or search fails.
- `RuntimeError`: If embedding backend unavailable.

### first_mention

Find chronologically earliest result above relevance threshold.

```python
def first_mention(results: list[siftd.domain.search_types.SearchChunk] | list[dict[str, Any]], *, threshold: float = ..., db_path: pathlib._local.Path | None = ...) -> siftd.domain.search_types.SearchChunk | dict[str, Any] | None
```

**Parameters:**

- `results`: List of SearchChunk or raw dicts from search. Dicts must have 'score', 'conversation_id', and 'source_ids'.
- `threshold`: Minimum score to consider relevant.

**Returns:** Earliest result above threshold (same type as input), or None if none qualify.

### build_index

Build or update the embeddings index.

```python
def build_index(*, db_path: pathlib._local.Path | None = ..., embed_db_path: pathlib._local.Path | None = ..., rebuild: bool = ..., backend: str | None = ..., verbose: bool = ...) -> dict
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

### group_tool_search_results

Collapse tool-call results into conversation groups for display.

```python
def group_tool_search_results(results: list[ToolSearchResult]) -> list[ToolSearchGroup]
```

### search_tool_calls

Search tool calls using structured fields + FTS over the projection.

```python
def search_tool_calls(q: str, *, db_path: pathlib._local.Path | None = ..., n: int = ..., rebuild_index: bool = ..., workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., tag: list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., tool: str | None = ..., tool_tag: str | None = ..., owner: str | None = ...) -> tuple[ToolQuery, list[ToolSearchResult]]
```

## Stats

### Data Types

### CostCoverage

Cost coverage across conversations with token data.

| Field | Type | Description |
|-------|------|-------------|
| `total_with_tokens` | `int` |  |
| `with_positive_cost` | `int` |  |
| `with_null_cost` | `int` |  |
| `pct_covered` | `float` |  |

### DatabaseStats

Complete database statistics.

| Field | Type | Description |
|-------|------|-------------|
| `db_path` | `Path` |  |
| `db_size_bytes` | `int` |  |
| `counts` | `TableCounts` |  |
| `harnesses` | `list[HarnessInfo]` |  |
| `harness_counts` | `list[HarnessCount]` |  |
| `top_workspaces` | `list[WorkspaceStats]` |  |
| `models` | `list[str]` |  |
| `top_tools` | `list[ToolStats]` |  |
| `top_tags` | `list[TagStats]` |  |
| `token_coverage` | `TokenCoverage` |  |
| `activity_window` | `tuple[str \| None, str \| None]` |  |
| `last_ingest_at` | `str \| None` |  |

### GroupUsage

Token/cost breakdown for a single group (model or workspace).

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `conversations` | `int` |  |
| `input_tokens` | `int` |  |
| `output_tokens` | `int` |  |
| `cost` | `float` |  |

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
| `source` | `str \| None` |  |
| `log_format` | `str \| None` |  |

### UsageSummary

Aggregated token/cost stats.

| Field | Type | Description |
|-------|------|-------------|
| `total_conversations` | `int` |  |
| `total_input_tokens` | `int` |  |
| `total_output_tokens` | `int` |  |
| `total_cost` | `float` |  |

### WorkspaceStats

Workspace with conversation count.

| Field | Type | Description |
|-------|------|-------------|
| `path` | `str` |  |
| `conversation_count` | `int` |  |
| `last_activity` | `str \| None` |  |

### ToolStats

Tool with usage count.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `usage_count` | `int` |  |

### Functions

### get_cost_coverage

Get cost coverage statistics from conversation_stats.

```python
def get_cost_coverage(conn: sqlite3.Connection | None = ..., *, db_path: pathlib._local.Path | None = ...) -> siftd.api.stats.CostCoverage | None
```

### get_stats

Get comprehensive database statistics.

```python
def get_stats(*, db_path: pathlib._local.Path | None = ..., owner: str | None = ...) -> DatabaseStats
```

**Returns:** DatabaseStats with counts, harnesses, workspaces, models, tools.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### get_usage_by_model

Get token/cost breakdown grouped by model.

```python
def get_usage_by_model(*, db_path: pathlib._local.Path | None = ...) -> list[GroupUsage]
```

### get_usage_by_workspace

Get token/cost breakdown grouped by workspace.

```python
def get_usage_by_workspace(*, db_path: pathlib._local.Path | None = ...) -> list[GroupUsage]
```

### get_usage_summary

Get aggregate token/cost totals across all conversations.

```python
def get_usage_summary(*, db_path: pathlib._local.Path | None = ...) -> UsageSummary
```

### dict_to_stats

Deserialize a JSON dict back to DatabaseStats.

```python
def dict_to_stats(data: dict) -> DatabaseStats
```

### list_workspaces

List workspaces with conversation counts.

```python
def list_workspaces(conn: sqlite3.Connection | None = ..., n: int = ..., *, db_path: pathlib._local.Path | None = ..., owner: str | None = ...) -> list[Row]
```

**Parameters:**

- `conn`: Database connection. Opened from db_path if not provided.
- `n`: Maximum workspaces to return.

**Returns:** Rows with 'path' and 'convs' keys.

### stats_cache_path

Return path to the stats cache file.

```python
def stats_cache_path() -> Path
```

### write_stats_cache

Atomically write stats to the cache file.

```python
def write_stats_cache(stats: DatabaseStats) -> None
```

### read_stats_cache

Read cached stats if the cache exists and is fresh.

```python
def read_stats_cache(*, db_path: pathlib._local.Path | None = ...) -> siftd.api.stats.DatabaseStats | None
```

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
def get_tool_tag_summary(*, db_path: pathlib._local.Path | None = ..., prefix: str = ..., owner: str | None = ...) -> list[TagUsage]
```

**Parameters:**

- `db_path`: Path to database. Uses default if not specified.

**Returns:** List of TagUsage sorted by count descending.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### get_tool_tags_by_workspace

Get tool tag usage broken down by workspace.

```python
def get_tool_tags_by_workspace(*, db_path: pathlib._local.Path | None = ..., prefix: str = ..., n: int = ..., owner: str | None = ...) -> list[WorkspaceTagUsage]
```

**Parameters:**

- `db_path`: Path to database. Uses default if not specified.
- `prefix`: Tag prefix to filter by (default: "shell:").

**Returns:** List of WorkspaceTagUsage sorted by total count descending.

**Raises:**

- `FileNotFoundError`: If database does not exist.

## Export

### Data Types

### ExportArtifact

A complete, serialized export document ready to serve or write.

| Field | Type | Description |
|-------|------|-------------|
| `content` | `str` |  |
| `media_type` | `str` |  |
| `filename` | `str` |  |
| `count` | `int` |  |

### ExportedConversation

A conversation prepared for export.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` |  |
| `workspace_path` | `str \| None` |  |
| `workspace_name` | `str \| None` |  |
| `model` | `str \| None` |  |
| `started_at` | `str \| None` |  |
| `turns` | `list[Turn]` |  |
| `tags` | `list[str]` |  |
| `total_tokens` | `int` |  |

### Functions

### export_conversations

Export conversations matching the specified criteria.

```python
def export_conversations(*, id: list[str] | None = ..., last: int | None = ..., n: int = ..., workspace: str | None = ..., tag: list[str] | None = ..., no_tag: list[str] | None = ..., since: str | None = ..., before: str | None = ..., search: str | None = ..., db_path: pathlib._local.Path | None = ..., include_thinking: bool = ..., include_tool_content: bool = ..., owner: str | None = ...) -> list[ExportedConversation]
```

### export_document

Export conversations as a complete document.

**Parameters:**

- `format`: Output format — "md" (markdown) or "json".
- `fidelity`: Rendering fidelity. Defaults to full (show everything).
- `no_header`: Omit per-conversation metadata headers.
- `last`: Export N most recent conversations (takes precedence over n).

**Returns:** ExportArtifact with serialized content, media_type, and filename.

## Other

### Data Types

### ApplyResult

Batch apply/remove result with enough context for CLI messaging.

| Field | Type | Description |
|-------|------|-------------|
| `action` | `Literal[apply, remove]` |  |
| `results` | `list[ApplyTagOutcome]` |  |
| `target_count` | `int` |  |
| `entity_type` | `str` |  |
| `resolved_entity_id` | `str \| None` |  |

### DeleteResult

Safe delete result payload.

| Field | Type | Description |
|-------|------|-------------|
| `status` | `str` |  |
| `tag_name` | `str` |  |

### RenameResult

Safe rename result payload.

| Field | Type | Description |
|-------|------|-------------|
| `status` | `str` |  |
| `old_name` | `str` |  |
| `new_name` | `str` |  |

### TagInfo

Tag with usage counts.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `description` | `str \| None` |  |
| `created_at` | `str` |  |
| `conversation_count` | `int` |  |
| `workspace_count` | `int` |  |
| `tool_call_count` | `int` |  |
| `prompt_count` | `int` |  |

### PushResult

Result of a push operation.

| Field | Type | Description |
|-------|------|-------------|
| `conversations` | `int` |  |
| `size_bytes` | `int` |  |
| `remote_name` | `str` |  |
| `remote_existed` | `bool` |  |
| `dry_run` | `bool` |  |
| `last_push_updated` | `bool` |  |

### SyncRemote

A registered sync remote.

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` |  |
| `host` | `str \| None` |  |
| `path` | `str` |  |
| `last_push` | `str \| None` |  |
| `last_pull` | `str \| None` |  |
| `last_sent` | `str \| None` |  |
| `last_push_filters` | `str` |  |
| `last_pull_filters` | `str` |  |
| `last_sent_filters` | `str` |  |
| `strategy` | `str` |  |
| `filters` | `siftd.domain.sync.SyncFilters \| None` |  |

### Exceptions

#### IndexCompatError

Raised when index metadata is incompatible with current backend configuration.

#### SyncError

Raised when a sync operation fails.

### Functions

### backup_database

Create a consistent online backup using sqlite3.Connection.backup().

```python
def backup_database(source_path: Path, target_path: Path) -> None
```

**Parameters:**

- `source_path`: Path to the source database.

**Raises:**

- `FileNotFoundError`: If source database does not exist.

### create_database

Create or open a database, running migrations.

```python
def create_database(db_path: pathlib._local.Path | None = ...) -> Connection
```

**Returns:** An open sqlite3.Connection with schema initialized.

### open_database

Open a database connection.

```python
def open_database(db_path: pathlib._local.Path | None = ..., *, read_only: bool = ...) -> Connection
```

**Parameters:**

- `db_path`: Path to the database file. If None, uses the default path.

**Returns:** An open sqlite3.Connection with row_factory set.

**Raises:**

- `FileNotFoundError`: If read_only=True and database doesn't exist.

### apply_tags

Apply or remove tags with shared orchestration.

```python
def apply_tags(*, db_path: Path, tags: list[str], entity_type: str = ..., entity_id: str | None = ..., last: int | None = ..., owner: str | None = ..., remove: bool = ...) -> ApplyResult
```

### apply_tag

Apply a tag to an entity.

```python
def apply_tag(conn: Connection, entity_type: str, entity_id: str, tag_id: str, *, commit: bool = ...) -> str | None
```

**Parameters:**

- `conn`: Database connection.
- `entity_type`: One of 'conversation', 'workspace', 'tool_call'.
- `entity_id`: The entity's ULID.
- `tag_id`: The tag's ULID.

**Returns:** Assignment ID if newly applied, None if already applied.

### delete_tag_safe

Delete a tag with owner-scope protections.

```python
def delete_tag_safe(*, db_path: Path, tag_name: str, owner: str | None = ...) -> DeleteResult
```

### delete_tag

Delete a tag and all its associations.

```python
def delete_tag(conn: Connection, name: str, *, commit: bool = ...) -> int
```

**Parameters:**

- `conn`: Database connection.
- `name`: Tag name to delete.

**Returns:** Count of entity associations removed, or -1 if tag not found.

### get_tag_id

Return tag id for name, or None if not found.

```python
def get_tag_id(conn: Connection, name: str) -> str | None
```

### get_or_create_tag

Get or create a tag by name.

```python
def get_or_create_tag(conn: Connection, name: str, description: str | None = ...) -> str
```

**Parameters:**

- `conn`: Database connection.
- `name`: Tag name.

**Returns:** Tag ID (ULID).

### list_tags

List all tags with usage counts.

```python
def list_tags(db_path: pathlib._local.Path | None = ..., conn: sqlite3.Connection | None = ..., *, since: str | None = ..., before: str | None = ..., owner: str | None = ...) -> list[TagInfo]
```

**Parameters:**

- `db_path`: Path to database. Ignored if conn provided.
- `conn`: Existing connection to use.
- `since`: Only count associations where conversation started after this ISO date.

**Returns:** List of TagInfo objects sorted by name.

### rename_tag_safe

Rename a tag with owner-scope protections.

```python
def rename_tag_safe(*, db_path: Path, old_name: str, new_name: str, owner: str | None = ...) -> RenameResult
```

### remove_tag

Remove a tag from an entity.

```python
def remove_tag(conn: Connection, entity_type: str, entity_id: str, tag_id: str, *, commit: bool = ...) -> bool
```

**Parameters:**

- `conn`: Database connection.
- `entity_type`: One of 'conversation', 'workspace', 'tool_call'.
- `entity_id`: The entity's ULID.
- `tag_id`: The tag's ULID.

**Returns:** True if removed, False if not applied.

### rename_tag

Rename a tag.

```python
def rename_tag(old_name: str = ..., new_name: str = ..., *, conn: sqlite3.Connection | None = ..., db_path: pathlib._local.Path | None = ..., commit: bool = ...) -> bool
```

**Parameters:**

- `old_name`: Current tag name.
- `new_name`: New tag name.
- `conn`: Database connection. Opened from db_path if not provided.
- `db_path`: Path to database. Ignored if conn provided.

**Returns:** True if renamed, False if old_name not found.

**Raises:**

- `ValueError`: If new_name already exists.

### merge_database

Merge a source database (slice) into the target database.

```python
def merge_database(target_db: Path, source_path: Path, *, rebuild_fts: bool = ..., dry_run: bool = ..., replace: bool = ..., before_commit: collections.abc.Callable[[sqlite3.Connection, dict], None] | None = ...) -> dict
```

**Parameters:**

- `target_db`: Path to the main siftd database.
- `source_path`: Path to the source database to merge in.
- `rebuild_fts`: Whether to rebuild the FTS5 index after merge.
- `dry_run`: If True, compute counts but roll back all changes.
- `replace`: If True (default), replace stale conversations with newer versions from the source. If False, keep existing versions.

**Returns:** Dict with counts of merged entities.

**Raises:**

- `FileNotFoundError`: If either database does not exist.

### receive_database

Create or merge a source database into the target.

```python
def receive_database(source_path: Path, target_db: Path, *, rebuild_fts: bool = ..., user_id: str | None = ..., push_id: str | None = ...) -> dict
```

**Parameters:**

- `source_path`: Path to the incoming database (e.g. a slice).
- `target_db`: Path to the target siftd database.
- `rebuild_fts`: Whether to rebuild the FTS5 index after merge.
- `user_id`: Authenticated user identity to stamp as conversation owner.

**Returns:** Dict with ``status`` ("created" or "merged") and merge stats.

**Raises:**

- `ValueError`: If source is not a valid SQLite database.
- `FileNotFoundError`: If source does not exist.

### slice_database

Export filtered conversations into a standalone SQLite database.

```python
def slice_database(source_db: Path, target_path: Path, *, workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., tag: list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., tool: str | None = ..., tool_tag: str | None = ..., search: str | None = ..., rebuild_fts: bool = ..., owner: str | None = ...) -> dict
```

**Parameters:**

- `source_db`: Path to the source siftd database.
- `target_path`: Path to write the sliced database. workspace..search: Standard filter kwargs (same as list_conversations).

**Returns:** Dict with 'conversations' count and 'size_bytes'.

**Raises:**

- `FileNotFoundError`: If source database does not exist.

### sync_push

Push conversations to a remote database.

```python
def sync_push(db_path: Path, remote: SyncRemote, *, since: str | None = ..., push_all: bool = ..., workspace: str | None = ..., tag: list[str] | None = ..., no_tag: list[str] | None = ..., owner: str | None = ..., dry_run: bool = ...) -> PushResult
```

**Parameters:**

- `db_path`: Path to the local siftd database.
- `remote`: The remote to push to.
- `since`: Only push conversations started after this date.
- `push_all`: Push all conversations (ignore last_push). workspace..owner: Filter kwargs (override remote config filters).

**Returns:** PushResult with stats.

**Raises:**

- `SyncError`: On transport or merge failure.
- `FileNotFoundError`: If local database doesn't exist.
