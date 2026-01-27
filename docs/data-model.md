# Data Model

strata stores conversation data in normalized SQLite. The schema is fixed for core entities and extensible via attribute tables.

## Entity hierarchy

```
conversations
  └── prompts (user messages)
        └── responses (assistant messages)
              └── tool_calls
```

A conversation is one coding session. Prompts and responses alternate. Tool calls are nested under the response that invoked them.

## Core tables

| Table | Purpose |
|-------|---------|
| `conversations` | Session-level: workspace, harness, timestamps, external ID |
| `prompts` | User input with ordering |
| `responses` | Assistant output with token usage, model, provider |
| `tool_calls` | Tool invocations with input, result, status, timing |
| `prompt_content` | Ordered content blocks in prompts |
| `response_content` | Ordered content blocks in responses |

## Vocabulary tables

Vocabulary tables normalize repeated values. Each row is unique and referenced by foreign key.

| Table | Purpose |
|-------|---------|
| `harnesses` | CLI tools: claude_code, gemini_cli, codex_cli, aider |
| `models` | Parsed model names with family, version, variant, creator, released |
| `providers` | API providers: anthropic, google, openrouter |
| `tools` | Canonical tool names: file.read, shell.execute, etc. |
| `tool_aliases` | Raw tool name → canonical name mapping per harness |
| `workspaces` | Project directories (paths) |
| `pricing` | Token pricing by model+provider for cost approximation |

### Tool canonicalization

Different CLI tools use different names for the same operation. strata maps them to 16 canonical names:

`file.read`, `file.edit`, `file.write`, `file.list`, `search.grep`, `search.glob`, `shell.execute`, `shell.stdin`, `web.fetch`, `web.search`, `notebook.edit`, `code.diff`, `task.create`, `task.status`, `mcp.call`, `memory.update`

Unknown tools are still tracked — they just don't have a canonical mapping.

### Model parsing

Raw model names (e.g., `claude-sonnet-4-20250514`) are decomposed at ingest time into structured fields: family, version, variant, creator, released. This enables queries like "all conversations with Opus models" without string matching.

## Extension tables

| Table | Purpose |
|-------|---------|
| `tags` | User-defined labels (name, created_at) |
| `conversation_tags` | Tag → conversation join table |
| `workspace_tags` | Tag → workspace join table |
| `tool_call_tags` | Tag → tool_call join table |
| `*_attributes` | Key-value metadata per entity type |

Attribute tables store variable metadata without schema changes. Used for provider-specific fields like cache tokens (`cache_creation_input_tokens`, `cache_read_input_tokens`).

## Search tables

| Table | Location | Purpose |
|-------|----------|---------|
| `content_fts` | Main DB | FTS5 full-text index over prompt and response text |
| `chunks` | Embeddings DB | Dense vector index for semantic search |
| `index_meta` | Embeddings DB | Strategy and backend metadata |

The embeddings database is separate from the main database. It's derived data — expensive to compute, independently rebuildable. See [search.md](search.md) for details.

## Ingestion tracking

| Table | Purpose |
|-------|---------|
| `ingested_files` | Maps file path → hash → conversation ID. Tracks errors. |

The `ingested_files` table enables deduplication:
- **Same hash**: skip (no changes)
- **Different hash**: delete old conversation, re-ingest
- **Error recorded**: skip until file hash changes (prevents retry loops)

## Cost model

Cost is computed at query time via JOIN against the `pricing` table:

```sql
SELECT
    r.input_tokens * p.input_per_million / 1e6 +
    r.output_tokens * p.output_per_million / 1e6 AS approx_cost
FROM responses r
JOIN pricing p ON p.model_id = r.model_id AND p.provider_id = r.provider_id
```

Labeled as approximate — flat pricing doesn't account for caching, batching, or subscription tiers.

## Paths

All data lives under XDG directories:

| Path | Content |
|------|---------|
| `~/.local/share/strata/strata.db` | Main database |
| `~/.local/share/strata/embeddings.db` | Embeddings (derived) |
| `~/.config/strata/queries/*.sql` | User SQL queries |
| `~/.config/strata/adapters/*.py` | Drop-in adapters |
| `~/.config/strata/formatters/*.py` | Drop-in formatters |
| `~/.config/strata/config.toml` | Configuration |

## IDs

All primary keys are ULIDs — lexicographically sortable, time-ordered, globally unique.

Conversation IDs are displayed as 12-character prefixes in CLI output. Prefix matching works for all ID-based lookups.
