# siftd.doctor

The doctor runs a set of independent health checks over the database, config,
and adapter sources and reports `Finding`s (`siftd doctor`, or the
`siftd.api.doctor` wrapper). Each check is one module under `checks/`, exposing a
class that satisfies the `Check` protocol defined in `checks/__init__.py`. A
check declares its identity and lane as class attributes — `name`,
`description`, `cost` (`"fast" | "slow" | "deep"`), plus `has_fix`,
`requires_db`, and `requires_embed_db` — and implements
`run(ctx: CheckContext) -> list[Finding]`. The human-readable description in the
generated table below is the class attribute, and the class docstring is the
long-form explanation; keep both truthful when you edit a check.

Checks are registered by instance in the `BUILTIN_CHECKS` list in
`checks/__init__.py`. That list is the single source of truth — `runner.py`
enumerates it, and the table below is generated from it (never from a docstring
heuristic). `CheckContext` carries the read-only DB connections and a shared,
lazily-populated adapter discovery pass (`get_adapters` / `discover_sources`) so
the slow-lane checks that reconcile discovered files against the DB
(`ingest-pending`, `adapter-stale`) walk each adapter's log directories once per
run rather than once per check.

`cost` is a lane, not a label: `runner.py` runs only `fast` checks under
`--fast`, includes `deep` checks (the expensive DB-integrity walks) only when
explicitly asked, and runs `fast` + `slow` by default. `view.py` renders
progress and `fixes.py` holds the advisory fix commands (findings carry a
`fix_command` but doctor never executes it).

To add a check: create `checks/<name>.py` with a class carrying the attributes
above and a `run()`, append an instance to `BUILTIN_CHECKS`, then run
`./dev docs` to refresh the table. Choose the smallest honest `cost` — a check
that only reads a cached count is `fast`; one that walks the filesystem is
`slow`; one that scans full tables for integrity is `deep`.

<!-- gen:begin checks -->
<sub>generated from the doctor check registry — run <code>./dev docs</code></sub>

| Check | Module | Cost | Description |
|-------|--------|------|-------------|
| `adapter-stale` | [checks/adapter_stale.py](checks/adapter_stale.py) | slow | Adapters with on-disk files newer than the last ingest |
| `config-valid` | [checks/config_valid.py](checks/config_valid.py) | fast | Configuration file syntax and values |
| `cost-coverage` | [checks/cost_coverage.py](checks/cost_coverage.py) | fast | Conversations with tokens but missing cost data |
| `db-blob-orphans` | [checks/db_blob_orphans.py](checks/db_blob_orphans.py) | deep | content_blobs with ref_count=0 not garbage-collected by triggers |
| `db-blob-refcount-drift` | [checks/db_blob_refcount_drift.py](checks/db_blob_refcount_drift.py) | deep | content_blobs ref_count out of sync with event_tool_call references |
| `db-fk-integrity` | [checks/db_fk_integrity.py](checks/db_fk_integrity.py) | deep | Foreign key constraint violations in the main database |
| `db-trigger-presence` | [checks/db_trigger_presence.py](checks/db_trigger_presence.py) | deep | Blob ref-count triggers present in sqlite_master |
| `drop-ins-valid` | [checks/drop_ins_valid.py](checks/drop_ins_valid.py) | fast | Drop-in adapters, formatters, and queries load without errors |
| `embed-config` | [checks/embed_config.py](checks/embed_config.py) | fast | Embedding backend usability and pending egress disclosure |
| `embeddings-available` | [checks/embeddings_available.py](checks/embeddings_available.py) | fast | Embedding support installation status |
| `embeddings-compat` | [checks/embeddings_compat.py](checks/embeddings_compat.py) | fast | Embedding index matches current backend configuration |
| `embeddings-stale` | [checks/embeddings_stale.py](checks/embeddings_stale.py) | fast | Conversations not indexed in embeddings database |
| `freelist` | [checks/freelist.py](checks/freelist.py) | fast | SQLite freelist pages (reclaimable with VACUUM) |
| `fts-integrity` | [checks/fts_integrity.py](checks/fts_integrity.py) | fast | FTS5 search index integrity |
| `fts-stale` | [checks/fts_stale.py](checks/fts_stale.py) | fast | FTS5 search index out of sync with content tables |
| `ingest-errors` | [checks/ingest_errors.py](checks/ingest_errors.py) | fast | Files that failed ingestion (recorded with error) |
| `ingest-pending` | [checks/ingest_pending.py](checks/ingest_pending.py) | slow | Files discovered by adapters but not yet ingested |
| `orphaned-chunks` | [checks/orphaned_chunks.py](checks/orphaned_chunks.py) | fast | Embedding chunks referencing deleted conversations |
| `pending-tags` | [checks/pending_tags.py](checks/pending_tags.py) | fast | Pending tags for sessions that may never be ingested |
| `pricing-provenance` | [checks/pricing_provenance.py](checks/pricing_provenance.py) | fast | Priced models lacking version-controlled reference provenance |
| `schema-current` | [checks/schema_current.py](checks/schema_current.py) | fast | Database schema migrations are up to date |
| `workspace-identity` | [checks/workspace_identity.py](checks/workspace_identity.py) | fast | Workspace identity via git remote (dedup detection) |
<!-- gen:end -->
