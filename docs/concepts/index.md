# Concepts

These docs explain how siftd works under the hood. Understanding these concepts helps you query effectively, extend siftd, and debug issues.

## Start here

1. **[Data Model](data-model.md)** — The core hierarchy: conversations contain prompts, prompts have responses, responses include tool calls. Understanding this structure tells you what you can query.

2. **[Adapters](adapters.md)** — How log files from different tools become conversations. Covers discovery, parsing, normalization, and writing custom adapters.

3. **[Search](search.md)** — Two search mechanisms: FTS5 for keywords, embeddings for meaning. How hybrid search combines them, plus tuning diversity and recency.

4. **[Tags](tags.md)** — Lightweight metadata for marking what matters. Naming conventions, auto-applied tags, and building institutional memory.

5. **[Storage](storage.md)** — Where data lives, why SQLite, content deduplication, backup/restore, and direct SQL access.

## The flow

```
Log files (Claude Code, Aider, Gemini CLI, ...)
    │
    ▼
Adapters parse → Conversations (data model)
    │
    ▼
Storage writes → SQLite (siftd.db)
    │
    ├─► FTS5 index (keyword search)
    │
    └─► Embeddings (semantic search, optional)

Tags annotate conversations for retrieval
```

## Reference docs

For complete specifications, see:

- [CLI Reference](../reference/cli.md) — all commands and flags
- [API Reference](../reference/api.md) — library usage
- [Schema Reference](../reference/schema.md) — database tables and columns
- [Writing Adapters](../guides/writing-adapters.md) — full adapter implementation guide
