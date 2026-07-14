# siftd.doctor

<!-- TODO(preamble): authored in slice 3 -->
Health check system (per-check modules under doctor/checks/).

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
