# siftd.storage

This is the only layer that talks to SQLite directly. Everything above it — `api/`, `cli/`, `serve/` — reaches data through the API layer and must not import `storage`; that boundary is what keeps the schema swappable. The modules here own connection management and migrations ([sqlite.py](sqlite.py)), the polymorphic events and attributes tables, FTS5 full-text search, the content-addressable blob store, and the derived rollup/stats tables.

Three invariants govern any change in this folder. First, write functions take `commit=False` by default: the caller controls the transaction, so batch operations stay atomic — don't sprinkle `conn.commit()` inside store helpers. Second, primary keys are ULIDs everywhere except `content_blobs`, which is keyed by the SHA256 hash of its content (that is what makes deduplication content-addressable). Third, schema changes follow a fixed process: bump `SCHEMA_VERSION` in [sqlite.py](sqlite.py) (currently 12), update [schema.sql](schema.sql), and register a function in the `MIGRATIONS` dict where version *N* migrates a database from *N-1* to *N*; migrations run automatically and idempotently on open. Note that derived-tier tables ([usage_rollup.py](usage_rollup.py), [conversation_stats.py](conversation_stats.py)) are rebuildable projections of the event facts, not sources of truth — treat them accordingly.

See [Storage](../../../docs/concepts/storage.md) for where data lives, deduplication, and backup/restore, and [Data Model](../../../docs/concepts/data-model.md) for the conversation hierarchy these tables persist.

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
