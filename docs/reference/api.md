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
def list_adapters(*, dropin_path: pathlib.Path | None = ...) -> list[AdapterInfo]
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
| `cost` | `Literal[fast, slow, deep]` |  |

### Finding

A single issue detected by a check.

| Field | Type | Description |
|-------|------|-------------|
| `check` | `str` | Check name that produced this finding (e.g., "ingest-pending"). |
| `severity` | `Literal[info, warning, error, hint]` | One of "info", "warning", "error", or "hint". |
| `message` | `str` | Human-readable description of the issue. |
| `fix_available` | `bool` | Whether a fix suggestion exists. |
| `fix_command` | `str \| None` | CLI command to fix the issue (advisory only, not executed automatically). User must run this command manually. |
| `context` | `dict \| None` | Optional structured data for programmatic consumers. |
| `target` | `str \| None` | Optional row-scope identifier — when set, the finding refers to a specific entity (e.g., a conversation id) rather than the whole result set or DB. Used by the caveats producer registry to thread row-level annotations through dispatch into renderers. |
| `channel` | `Literal[text, json, both]` | Controls output-format visibility. "text" findings are excluded from --json output; "json" findings are excluded from text/TTY output; "both" (default) appears everywhere. |

### Functions

### list_checks

Return metadata about all available checks.

```python
def list_checks() -> list[CheckInfo]
```

### run_checks

Run health checks and return findings.

```python
def run_checks(*, checks: list[str] | None = ..., db_path: pathlib.Path | None = ..., embed_db_path: pathlib.Path | None = ..., deep: bool = ..., fast: bool = ..., on_check_done: object | None = ...) -> list[Finding]
```

**Parameters:**

- `checks`: Specific check names to run, or None for all.
- `db_path`: Main database path. Uses default if not specified.
- `embed_db_path`: Embeddings database path. Uses default if not specified.
- `deep`: Include checks with cost="deep". Default False.

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
| `prompt_external_id` | `str \| None` |  |
| `response_external_ids` | `list[str]` |  |
| `tool_use_ids` | `list[str]` |  |

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
def find_session_file(session_id_prefix: str) -> pathlib.Path | None
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
| `event_id` | `str \| None` |  |

### ToolCallDetail

Tool call with optional input/result for --tools mode.

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | `str` |  |
| `status` | `str` |  |
| `count` | `int` |  |
| `input` | `str \| None` |  |
| `result` | `str \| None` |  |
| `tool_call_id` | `str \| None` |  |

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
| `prompt_id` | `str \| None` |  |
| `response_ids` | `list[str]` |  |
| `tool_call_ids` | `list[str]` |  |

### Exceptions

#### AmbiguousPrefix

Prefix matches multiple conversations — caller must use a longer prefix or full ID.

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
def list_conversations(*, fidelity: Fidelity, db_path: pathlib.Path | None = ..., workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., search: str | None = ..., tool: str | None = ..., tag: str | list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., tag_kind: list[str] | None = ..., tool_tag: str | None = ..., n: int = ..., oldest: bool = ..., owner: str | None = ...) -> list[ConversationSummary]
```

**Parameters:**

- `fidelity`: Cross-stage rendering contract carried through to the renderer, which emits the cost column at ``depth >= 3``. Cost itself is no longer recomputed here: the fast path reads the precomputed ``conversation_stats.cost`` (the rollup's single canonical definition), and the no-stats fallback emits NULL cost rather than re-deriving it (see ``_list_conversations_impl``).
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
- `tag_kind`: Scope tag/all_tags/no_tag matching to specific target_kinds (e.g., ['conversation'], ['response', 'tool_call']). Defaults to all conversation-bearing kinds when None.
- `tool_tag`: Filter by tool call tag (e.g., 'shell:test').
- `n`: Maximum results to return (0 = unlimited).
- `oldest`: Sort by oldest first instead of newest.

**Returns:** List of ConversationSummary objects.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### get_conversation

Get full conversation detail by ID.

```python
def get_conversation(id: str, *, fidelity: Fidelity, db_path: pathlib.Path | None = ..., tool_filter: str | None = ..., owner: str | None = ..., anchor: str | None = ..., anchor_value: int | str | None = ..., window_start: int | None = ..., window_end: int | None = ...) -> siftd.api.conversations.ConversationDetail | None
```

**Parameters:**

- `id`: Full or prefix of conversation ULID.
- `fidelity`: Cross-stage rendering contract. ``fidelity.shows("thinking")`` decides whether thinking blocks appear in turns; ``fidelity.shows("tools")`` decides whether tool inputs/results are fetched and inlined.
- `db_path`: Path to database. Uses default if not specified.
- `tool_filter`: Filter tool calls — 'errors' for failed only, or a tool name prefix (e.g. 'shell', 'file.read').
- `anchor`: Anchor axis — one of 'from_start', 'from_end', 'at_turn', 'around'. None means no anchor (whole conversation returned).
- `anchor_value`: Value for the anchor: int for 'at_turn', str for 'around'. Ignored for 'from_start' and 'from_end'.
- `window_start`: Turn offset from anchor (inclusive). None = anchor only.

**Returns:** ConversationDetail with timeline, or None if not found.

**Raises:**

- `FileNotFoundError`: If database does not exist.
- `AmbiguousPrefix`: If ``id`` is a prefix matching more than one conversation. Programmatic callers should catch this; CLI callers print the matched IDs and exit 2.
- `AnchorOutOfRange`: If ``anchor='at_turn'`` and N >= turn count.
- `AnchorNotFound`: If ``anchor='around'`` and phrase has no match.
- `AnchorPhraseInvalid`: If ``anchor='around'`` phrase cannot be parsed by FTS5.

### get_conversation_metadata

Fetch workspace and started_at for a fully-resolved conversation ID.

```python
def get_conversation_metadata(conn: Connection, conversation_id: str) -> dict | None
```

**Returns:** Dict with keys 'id', 'workspace', 'started_at', or None if not found.

### resolve_entity_id

Resolve an entity ID, supporting prefix match for conversations.

```python
def resolve_entity_id(conn: Connection, entity_type: str, entity_id: str, *, owner: str | None = ...) -> str | None
```

**Parameters:**

- `conn`: Database connection.
- `entity_type`: One of 'conversation', 'workspace', 'tool_call', 'prompt', 'response', or 'exchange'.

**Returns:** Resolved full ID, or None if not found.

**Raises:**

- `AmbiguousPrefix`: If entity_type is 'conversation' and the prefix matches more than one row.

### list_query_files

List available user-defined SQL query files.

```python
def list_query_files() -> list[QueryFile]
```

**Returns:** List of QueryFile with name, path, and required variables.

### run_query_file

Run a user-defined SQL query file.

```python
def run_query_file(name: str, variables: dict[str, str] | None = ..., *, db_path: pathlib.Path | None = ...) -> QueryResult
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
def copy_adapter(name: str, *, dest_dir: pathlib.Path | None = ..., force: bool = ...) -> Path
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
def copy_formatter(name: str, *, dest_dir: pathlib.Path | None = ..., force: bool = ...) -> Path
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
def copy_query(name: str, *, dest_dir: pathlib.Path | None = ..., force: bool = ...) -> Path
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

### SearchChunk

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
| `turn_index` | `int \| None` |  |
| `event_id` | `str \| None` |  |

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
| `turn_index` | `int \| None` |  |
| `event_id` | `str \| None` |  |

### ScoreBreakdown

Detailed score components for explainability.

| Field | Type | Description |
|-------|------|-------------|
| `embedding_sim` | `float` |  |
| `recency_boost` | `float` |  |
| `pre_mmr_score` | `float \| None` |  |
| `mmr_penalty` | `float \| None` |  |
| `mmr_rank` | `int \| None` |  |
| `final_score` | `float \| None` |  |
| `fts5_matched` | `bool` |  |
| `fts5_mode` | `str \| None` |  |

### ConversationSearchSummary

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

### Functions

### search_chunks

Canonical entry point for retrieving search chunks.

```python
def search_chunks(q: str, *, db_path: Path, embed_db: pathlib.Path | None = ..., n: int = ..., mode: str = ..., workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., tag: list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., tag_kind: list[str] | None = ..., exclude_active: bool = ..., include_derivative: bool = ..., owner: str | None = ..., recall: int = ..., rerank: str = ..., lambda_: float = ..., recency: bool = ..., recency_half_life: float = ..., recency_max_boost: float = ..., threshold: float = ..., backend: str | None = ..., embed_backend: siftd.api.search.EmbeddingBackend | None = ..., raw_fts: bool = ...) -> list[SearchChunk]
```

### hybrid_search

Unified search pipeline — FTS5, semantic, or hybrid.

```python
def hybrid_search(q: str, *, db_path: Path, embed_db: pathlib.Path | None = ..., n: int = ..., mode: str = ..., workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., tag: list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., tag_kind: list[str] | None = ..., exclude_active: bool = ..., include_derivative: bool = ..., owner: str | None = ..., recall: int = ..., raw_fts: bool = ..., rerank: str = ..., lambda_: float = ..., recency: bool = ..., recency_half_life: float = ..., recency_max_boost: float = ..., threshold: float = ..., backend: str | None = ..., embed_backend: siftd.api.search.EmbeddingBackend | None = ...) -> list[SearchChunk]
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

### aggregate_by_conversation

Aggregate chunk results to conversation-level scores.

```python
def aggregate_by_conversation(results: list[siftd.domain.search_types.SearchChunk] | list[dict[str, Any]], *, limit: int = ...) -> list[ConversationSearchSummary]
```

**Parameters:**

- `results`: List of SearchResult from hybrid_search.

**Returns:** List of ConversationScore, sorted by max_score descending.

### compute_thread_tiers

Split chunks into tier1 (expanded) and tier2 (compact) for thread mode.

```python
def compute_thread_tiers(results: list[siftd.domain.search_types.SearchChunk] | list[dict[str, Any]]) -> tuple[list[SearchChunk], list[SearchChunk]]
```

### filter_by_threshold

Filter chunk results by score threshold.

```python
def filter_by_threshold(results: list[siftd.domain.search_types.SearchChunk] | list[dict[str, Any]], *, threshold: float | None) -> list[SearchChunk]
```

### sort_chunks_by_time

Sort chunks by date then chunk_id (legacy CLI behavior).

```python
def sort_chunks_by_time(results: list[siftd.domain.search_types.SearchChunk] | list[dict[str, Any]]) -> list[SearchChunk]
```

### enrich_search_metadata

Enrich chunks with workspace and started_at metadata in-place.

```python
def enrich_search_metadata(conn: Connection, results: list[SearchChunk]) -> None
```

### enrich_file_refs

Attach file references to each chunk in-place.

```python
def enrich_file_refs(conn: Connection, results: list[SearchChunk]) -> None
```

### enrich_exchanges

Attach full prompt+response exchanges for each chunk in-place.

```python
def enrich_exchanges(conn: Connection, results: list[SearchChunk]) -> None
```

### enrich_context_window

Attach +/-N context exchanges around each chunk's source prompts.

```python
def enrich_context_window(conn: Connection, results: list[SearchChunk], n: int) -> None
```

### embeddings_available

Return whether optional embedding dependencies are installed.

```python
def embeddings_available() -> bool
```

### first_mention

Find chronologically earliest result above relevance threshold.

```python
def first_mention(results: list[siftd.domain.search_types.SearchChunk] | list[dict[str, Any]], *, threshold: float = ..., db_path: pathlib.Path | None = ...) -> siftd.domain.search_types.SearchChunk | dict[str, Any] | None
```

**Parameters:**

- `results`: List of SearchChunk or raw dicts from search. Dicts must have 'score', 'conversation_id', and 'source_ids'.
- `threshold`: Minimum score to consider relevant.

**Returns:** Earliest result above threshold (same type as input), or None if none qualify.

### build_index

Build or update the embeddings index.

```python
def build_index(*, db_path: pathlib.Path | None = ..., embed_db_path: pathlib.Path | None = ..., rebuild: bool = ..., backend: str | None = ..., verbose: bool = ...) -> dict
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
def get_cost_coverage(conn: sqlite3.Connection | None = ..., *, db_path: pathlib.Path | None = ..., owner: str | None = ...) -> siftd.api.stats.CostCoverage | None
```

### get_stats

Get comprehensive database statistics.

```python
def get_stats(*, db_path: pathlib.Path | None = ..., owner: str | None = ...) -> DatabaseStats
```

**Returns:** DatabaseStats with counts, harnesses, workspaces, models, tools.

**Raises:**

- `FileNotFoundError`: If database does not exist.

### get_usage_by_model

Get token/cost breakdown grouped by model.

```python
def get_usage_by_model(*, db_path: pathlib.Path | None = ..., owner: str | None = ...) -> list[GroupUsage]
```

### get_usage_by_workspace

Get token/cost breakdown grouped by workspace.

```python
def get_usage_by_workspace(*, db_path: pathlib.Path | None = ..., owner: str | None = ...) -> list[GroupUsage]
```

### get_usage_summary

Get aggregate token/cost totals across all conversations.

```python
def get_usage_summary(*, db_path: pathlib.Path | None = ..., owner: str | None = ...) -> UsageSummary
```

### dict_to_stats

Deserialize a JSON dict back to DatabaseStats.

```python
def dict_to_stats(data: dict) -> DatabaseStats
```

### list_workspaces

List workspaces with conversation counts.

```python
def list_workspaces(conn: sqlite3.Connection | None = ..., n: int = ..., *, db_path: pathlib.Path | None = ..., owner: str | None = ...) -> list[Row]
```

**Parameters:**

- `conn`: Database connection. Opened from db_path if not provided.
- `n`: Maximum workspaces to return.

**Returns:** Rows with 'id' (workspace ULID), 'path', 'git_remote', 'convs', and 'last_activity' keys. The ULID 'id' is the workspace's stable identity (workspaces.id) — the read API addresses workspaces by it, not by the slash-containing path.

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
def read_stats_cache(*, db_path: pathlib.Path | None = ...) -> siftd.api.stats.DatabaseStats | None
```

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
def export_conversations(*, fidelity: Fidelity, id: list[str] | None = ..., last: int | None = ..., n: int = ..., workspace: str | None = ..., tag: list[str] | None = ..., no_tag: list[str] | None = ..., tag_kind: list[str] | None = ..., since: str | None = ..., before: str | None = ..., search: str | None = ..., db_path: pathlib.Path | None = ..., owner: str | None = ...) -> list[ExportedConversation]
```

### export_document

Export conversations as a complete document.

```python
def export_document(*, fidelity: Fidelity, format: str = ..., no_header: bool = ..., id: list[str] | None = ..., last: int | None = ..., n: int = ..., workspace: str | None = ..., tag: list[str] | None = ..., no_tag: list[str] | None = ..., tag_kind: list[str] | None = ..., since: str | None = ..., before: str | None = ..., search: str | None = ..., db_path: pathlib.Path | None = ..., owner: str | None = ...) -> ExportArtifact
```

**Parameters:**

- `fidelity`: Cross-stage rendering contract. Drives both fetch (via ``shows("tools")``) and render (placeholder vs. expanded thinking/tool blocks). Thinking blocks are always fetched so placeholders can render — see ``export_conversations``.
- `format`: Output format — "md" (markdown) or "json".
- `no_header`: Omit per-conversation metadata headers.
- `last`: Export N most recent conversations (takes precedence over n).

**Returns:** ExportArtifact with serialized content, media_type, and filename.

## Other

### Data Types

### BackfillRunResult

Result metadata for a backfill API run.

| Field | Type | Description |
|-------|------|-------------|
| `db_path` | `Path` |  |
| `operation` | `Literal[response_attributes, shell_tags, derivative_tags, filter_binary]` |  |
| `dry_run` | `bool` |  |
| `inserted_attributes` | `int` |  |
| `tagged_conversations` | `int` |  |
| `shell_tag_counts` | `dict[str, int]` |  |
| `filtered` | `int` |  |
| `skipped` | `int` |  |
| `errors` | `int` |  |
| `elapsed_ms` | `int` |  |

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
| `exchange_count` | `int` |  |
| `prompt_count` | `int` |  |
| `response_count` | `int` |  |
| `activity` | `list[int] \| None` |  |

### EventDetail

A single event with content, tags, and kind-specific data.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` |  |
| `kind` | `str` |  |
| `conversation_id` | `str` |  |
| `parent_id` | `str \| None` |  |
| `external_id` | `str \| None` |  |
| `timestamp` | `str \| None` |  |
| `tags` | `list[str]` |  |
| `content_blocks` | `list[dict[str, Any]]` |  |
| `kind_specific` | `dict[str, Any]` |  |
| `conversation` | `dict[str, Any] \| None` |  |
| `neighbors` | `dict[str, str \| None] \| None` |  |

### IngestRunResult

Result metadata for an ingest API run.

| Field | Type | Description |
|-------|------|-------------|
| `db_path` | `Path` |  |
| `db_created` | `bool` |  |
| `mode` | `Literal[ingest, rebuild_fts]` |  |
| `adapters` | `list[str]` |  |
| `scan_paths` | `list[str]` |  |
| `stats` | `siftd.ingestion.orchestration.IngestStats \| None` |  |
| `elapsed_ms` | `int` |  |
| `dropin_failures` | `list[tuple[Path, str]]` |  |

### HealthStatus

Serve health payload.

| Field | Type | Description |
|-------|------|-------------|
| `service` | `str` |  |
| `status` | `str` |  |
| `db_id` | `str` |  |
| `db_size_bytes` | `int` |  |
| `conversations` | `int` |  |

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
| `windows` | `int` |  |

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

#### PreflightError

Raised when a source database fails integrity pre-flight checks.

#### SchemaUpgradeRequiredError

Raised on read-only open of a stale-schema DB that cannot be auto-upgraded.

#### AdapterSelectionError

Raised when requested adapter names match no discovered adapters.

#### IndexCompatError

Raised when index metadata is incompatible with current backend configuration.

#### SyncError

Raised when a sync operation fails.

### Functions

### BackfillOperation

### run_backfill

Run a backfill operation with API-owned DB lifecycle.

```python
def run_backfill(*, db_path: Path, operation: Literal[response_attributes, shell_tags, derivative_tags, filter_binary] = ..., dry_run: bool = ...) -> BackfillRunResult
```

### audit_db_integrity

Run structural integrity checks on a database file.

```python
def audit_db_integrity(path: Path) -> list
```

**Raises:**

- `FileNotFoundError`: If ``path`` does not exist. Propagated from the doctor runner, which requires the DB for the structural checks.
- `Note`: embed_db_path defaults to the user's local embed DB, which is irrelevant for source preflight. Any future deep check that reads embed_db_path would need to be excluded from _PREFLIGHT_CHECKS or receive an alternate embed path here.

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
def create_database(db_path: pathlib.Path | None = ...) -> Connection
```

**Returns:** An open sqlite3.Connection with schema initialized.

### open_database

Open a database connection.

```python
def open_database(db_path: pathlib.Path | None = ..., *, read_only: bool = ..., auto_upgrade: bool = ...) -> Connection
```

**Parameters:**

- `db_path`: Path to the database file. If None, uses the default path.
- `read_only`: If True, open in read-only mode.

**Returns:** An open sqlite3.Connection with row_factory set.

**Raises:**

- `FileNotFoundError`: If read_only=True and database doesn't exist.
- `SchemaUpgradeRequiredError`: If read_only=True, schema is stale, and the file is not writable for an auto-upgrade.

### run_preflight

Audit a database and raise PreflightError on error-severity findings.

```python
def run_preflight(path: Path, label: str = ...) -> None
```

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
- `entity_type`: One of 'conversation', 'workspace', 'prompt', 'response', 'tool_call', 'exchange'.
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
def list_tags(db_path: pathlib.Path | None = ..., conn: sqlite3.Connection | None = ..., *, since: str | None = ..., before: str | None = ..., owner: str | None = ..., fidelity: painted.core.fidelity.Fidelity | None = ...) -> list[TagInfo]
```

**Parameters:**

- `db_path`: Path to database. Ignored if conn provided.
- `conn`: Existing connection to use.
- `since`: Only count associations where conversation started after this ISO date.
- `before`: Only count associations where conversation started before this ISO date.

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
def rename_tag(old_name: str = ..., new_name: str = ..., *, conn: sqlite3.Connection | None = ..., db_path: pathlib.Path | None = ..., commit: bool = ...) -> bool
```

**Parameters:**

- `old_name`: Current tag name.
- `new_name`: New tag name.
- `conn`: Database connection. Opened from db_path if not provided.
- `db_path`: Path to database. Ignored if conn provided.

**Returns:** True if renamed, False if old_name not found.

**Raises:**

- `ValueError`: If new_name already exists.

### get_event

Get a single event by ID (full or prefix).

```python
def get_event(id: str, *, db_path: pathlib.Path | None = ..., conn: sqlite3.Connection | None = ..., include_content: bool = ..., include_neighbors: bool = ..., owner: str | None = ...) -> siftd.api.events.EventDetail | None
```

**Parameters:**

- `id`: Event ULID, or a prefix of one.
- `db_path`: Path to database. Uses default if not specified.
- `conn`: Optional existing read-only connection. Caller retains ownership; if provided, db_path is ignored. Useful to avoid a second open after a smart-route probe.
- `include_content`: Include `content_blocks`. Default True.
- `include_neighbors`: Include `neighbors` (opt-in for cost). Default False.

**Returns:** EventDetail or None if no event matches.

**Raises:**

- `FileNotFoundError`: If the database does not exist.

### run_ingest

Run ingestion from discovered adapters.

```python
def run_ingest(*, db_path: Path, adapter_names: list[str] | None = ..., scan_paths: list[str] | None = ..., filter_binary: bool | None = ..., on_event: collections.abc.Callable[[siftd.ingestion.orchestration.IngestEvent], None] | None = ...) -> IngestRunResult
```

### run_rebuild_fts

Rebuild FTS index only (no ingestion).

```python
def run_rebuild_fts(*, db_path: Path) -> IngestRunResult
```

### get_health_status

Return database-backed health status for serve.

```python
def get_health_status(db_path: Path) -> HealthStatus
```

### record_push_log

Record a push event in the push_log table.

```python
def record_push_log(*, db_path: Path, identity: str, conversations: int, size_bytes: int, source_ip: str | None, push_id: str | None = ...) -> None
```

### merge_database

Merge a source database (slice) into the target database.

```python
def merge_database(target_db: Path, source_path: Path, *, rebuild_fts: bool = ..., dry_run: bool = ..., replace: bool = ..., before_commit: collections.abc.Callable[[sqlite3.Connection, dict], None] | None = ..., preflight: bool = ..., user_id: str | None = ...) -> dict
```

**Parameters:**

- `target_db`: Path to the main siftd database.
- `source_path`: Path to the source database to merge in.
- `rebuild_fts`: Whether to rebuild the FTS5 index after merge.
- `dry_run`: If True, compute counts but roll back all changes.
- `replace`: If True (default), replace stale conversations with newer versions from the source. If False, keep existing versions.
- `before_commit`: Optional callback(conn, stats) invoked after merge but before commit.  Runs in the same transaction as the merge, so any writes are atomic with the merge itself.
- `preflight`: If True (default), run structural integrity checks on the source before merging. Pass False when the caller has already run preflight (e.g. receive_database calls merge_database after its own preflight check).

**Returns:** Dict with counts of merged entities.

**Raises:**

- `FileNotFoundError`: If either database does not exist.
- `PreflightError`: If preflight=True and source fails integrity checks.

### receive_database

Create or merge a source database into the target.

```python
def receive_database(source_path: Path, target_db: Path, *, rebuild_fts: bool = ..., user_id: str | None = ..., push_id: str | None = ..., preflight: bool = ...) -> dict
```

**Parameters:**

- `source_path`: Path to the incoming database (e.g. a slice).
- `target_db`: Path to the target siftd database.
- `rebuild_fts`: Whether to rebuild the FTS5 index after merge.
- `user_id`: Authenticated user identity to stamp as conversation owner.
- `push_id`: Push log ID for provenance linking.

**Returns:** Dict with ``status`` ("created" or "merged") and merge stats.

**Raises:**

- `ValueError`: If source is not a valid SQLite database.
- `FileNotFoundError`: If source does not exist.
- `PreflightError`: If preflight=True and source fails integrity checks.

### slice_database

Export filtered conversations into a standalone SQLite database.

```python
def slice_database(source_db: Path, target_path: Path, *, workspace: str | None = ..., model: str | None = ..., since: str | None = ..., before: str | None = ..., tag: list[str] | None = ..., all_tags: list[str] | None = ..., no_tag: list[str] | None = ..., tag_kind: list[str] | None = ..., tool: str | None = ..., tool_tag: str | None = ..., search: str | None = ..., rebuild_fts: bool = ..., owner: str | None = ...) -> dict
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
