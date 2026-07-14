# siftd.storage

<!-- TODO(preamble): authored in slice 3 -->
SQLite ops, schema, content blobs.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [attributes.py](attributes.py) | Read/write operations for the polymorphic attributes table (schema v4). |
| [blobs.py](blobs.py) | Content-addressable blob storage for deduplication. |
| [conversation_stats.py](conversation_stats.py) | Materialized conversation stats table. |
| [embeddings.py](embeddings.py) | Embeddings storage for semantic search. |
| [events.py](events.py) | Writer and reader functions for the polymorphic events schema (schema v4). |
| [filters.py](filters.py) | Dynamic WHERE clause builder for conversation filters. |
| [fts.py](fts.py) | FTS5 full-text search operations for siftd storage. |
| [migrate_workspaces.py](migrate_workspaces.py) | Migration script for workspace git remote identity. |
| [queries.py](queries.py) | Centralized SQL read queries for siftd storage. |
| [search_log.py](search_log.py) | Search-log storage: capture executed searches and later 'opened' signals. |
| [sessions.py](sessions.py) | Live session tracking and pending tag storage. |
| [sql_helpers.py](sql_helpers.py) | SQL helper utilities for query building and result processing. |
| [sqlite.py](sqlite.py) | SQLite storage adapter for siftd. |
| [tags.py](tags.py) | Tag CRUD operations for siftd storage. |
| [usage_rollup.py](usage_rollup.py) | Usage rollup — the keystone derived-tier fact table. |
<!-- gen:end -->
