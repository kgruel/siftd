# DB storage research: siftd

Date: 2026-02-10
Branch: db-storage-research

## Baseline constraints (from codebase)
- Local SQLite is the source of truth. Schema uses FTS5 (`content_fts`), triggers, and content-addressed blobs (`content_blobs`).
- IDs are ULIDs; most tables are append-only with a few updates (workspace git remotes, tag renames) and deletions (conversation delete, pending tags cleanup).
- Embeddings live in a separate SQLite DB and are explicitly rebuildable.
- CLI already provides `siftd status` (stats summary) and `siftd doctor` (data integrity checks, incl. FTS stale/integrity).
- Custom SQL queries (builtin and user-defined) assume SQLite syntax and FTS5.

These constraints point to: keep local SQLite, treat FTS/embeddings as derived, and make any remote/portable flow opt-in and robust against partial compatibility.

## 1. Remote hosting options (Turso/libSQL, LiteFS, Postgres sync-push)

### Option A: Turso / libSQL (SQLite-compatible with replication)
**Fit**
- libSQL uses the SQLite API and file format, so the existing schema and SQLite-focused query files remain compatible. [1]
- Turso preloads common extensions including FTS5, so FTS-backed queries should run remotely if you execute them against a Turso/libSQL database. [2]
- Embedded replicas read from a local SQLite file and sync writes to a remote primary; they can run offline and sync later, but you must not open the local file directly while the replica is running. [3]

**Trade-offs / risks**
- Multi-writer is not the default; embedded replicas are read-local/write-remote, so concurrency across devices is limited without additional coordination.
- Adds a new client dependency (libsql client) and operational complexity (credentials, network, latency).
- FTS and triggers are compatible, but you still have to manage migrations and schema versioning across local and remote.

**Recommendation**
- Best fit for a future "optional remote host" story because it preserves SQLite semantics and FTS5. Keep it as an opt-in advanced mode; do not make it the default storage layer.
- If pursued, scope first iteration to **read-only remote** or **push-only** sync (local writes, remote read analytics).

### Option B: LiteFS (filesystem-level SQLite replication)
**Fit**
- LiteFS replicates SQLite files at the filesystem layer (FUSE). It keeps SQLite semantics intact and works with existing schema, FTS5, and custom SQL.
- It is designed for a single writable primary with async replication to replicas; replicas are read-only. [4] [5]

**Trade-offs / risks**
- Write throughput is limited (~100 transactions/sec) due to FUSE overhead. [5]
- Replication is async; if the primary fails before replication, data loss is possible. [5]
- Operational constraints (running a primary, handling failover) are non-trivial for personal tooling; writes on a non-primary return errors. [4] [5]

**Recommendation**
- Good fit for a **self-hosted, always-on primary** (server or Fly.io) where siftd runs in one place and clients read from replicas. Not a good fit for casual multi-device personal sync.
- Keep as "advanced deployment" documentation, not a default path.

### Option C: Postgres sync-push (analytics sink)
**Fit**
- Postgres can be used as a centralized analytics store (especially for team analysis), with logical replication commonly used to consolidate data for reporting. [6]
- This aligns with the "don't replace local SQLite" principle: keep SQLite as source, push to Postgres for team/BI use.

**Trade-offs / risks**
- Postgres does not support SQLite FTS5 or SQLite-specific virtual tables; you would need to transform or rebuild search indexes (or treat FTS as derived and exclude it from sync).
- Logical replication conflicts stop replication and require manual intervention, which is a risk if you attempt bidirectional sync. [7]

**Recommendation**
- Treat Postgres as **append-only analytics sink** driven by explicit export/sync push.
- Avoid bidirectional replication; keep Postgres as read-mostly destination to avoid conflict semantics.

## 2. DB UX commands: propose `siftd db`

Goal: separate "operational DB tasks" from `doctor` (data quality) and `status` (summary stats).

### Proposed command surface
```
siftd db info                 # show db path, size, page_size, wal mode, fts status
siftd db stats                # row counts + size by table/index, top tables by size
siftd db schema [--table T]   # show schema or table DDL
siftd db vacuum [--full]      # VACUUM, VACUUM INTO backup, or PRAGMA optimize
siftd db backup <file>        # consistent backup snapshot
siftd db restore <file>       # replace local db (prompt/confirm)
siftd db check                # integrity_check, foreign_key_check, fts integrity
siftd db fts rebuild           # rebuild FTS index
siftd db embeddings stats      # embeddings db size, chunk count (if installed)
```

### Implementation notes (low risk)
- **Stats/size breakdown**: use `PRAGMA page_count`, `page_size`, `freelist_count`, and optionally `dbstat` virtual table if available for per-table page usage.
- **Schema introspection**: `sqlite_master` or `pragma table_info` + `pragma index_list`.
- **Health checks**: wrap `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, and FTS integrity. Avoid overlapping with `doctor` by keeping this operational.
- **Backup**: use SQLite backup API (or `VACUUM INTO`) to guarantee a consistent snapshot even while the DB is in use.

### UX philosophy
- `siftd status` stays the fast, human summary.
- `siftd db` is explicit and opt-in for operators. Include `--json` outputs for automation.

## 3. Multi-device portability (export/import/merge)

### Minimal viable story
- **Export**: `siftd db backup <file>` to produce a full SQLite snapshot (plus optional embeddings DB export). Embeddings should default to "not exported" since it is derived.
- **Import**: `siftd db restore <file>` to replace local DB; run `siftd ingest --rebuild-fts` as a safety net if needed.

### Merge story (deliberate opt-in)
Merging is trickier because of unique constraints and device-local artifacts.

- **Append-only tables**: conversations/prompts/responses/tool_calls/content can be merged with `INSERT OR IGNORE` on primary key IDs.
- **Natural-key conflicts**: `conversations` are unique on `(harness_id, external_id)`; if two devices ingested the same session, IDs differ but natural keys collide. On merge, detect duplicates and choose a winner; emit a conflict report.
- **Device-local tables**: `ingested_files` should be skipped or namespaced by machine ID to avoid path collisions. `active_sessions`/`pending_tags` should be dropped on merge.
- **Tags/workspaces**: merge by stable keys (tag name, git remote) and run existing workspace-merge logic to dedupe paths.
- **Derived tables**: rebuild `content_fts` after merge; ignore embeddings and regenerate.

### Minimal conflict resolution policy
- First writer wins (deterministic by `ended_at` or ULID time) with a conflict report output.
- Provide `--dry-run` to surface collisions before touching data.

## 4. Sync-push architecture (team analysis)

### Minimal API surface
```
siftd sync push --dest <url|dsn> [--since <cursor>] [--redact <profile>]
siftd sync status --dest <url|dsn>
```

### Data contract recommendations
- **Source DB is SQLite**; sync is **push-only**.
- **Cursor model**: store a `sync_state` table locally with last-synced ULID per table (or per entity type). ULIDs are sortable by time, so they can be used as a stable cursor.
- **Idempotent upserts**: use `INSERT ... ON CONFLICT DO UPDATE` with primary keys and natural keys for vocabulary tables (models/tools/providers). For append-only tables, `ON CONFLICT DO NOTHING`.
- **Derived data**: exclude `content_fts` and embeddings; rebuild on the server if needed.

### Export format
- **NDJSON** is the minimal viable format: streamable, line-delimited, easy to ingest, and friendly for incremental sync.
- **Parquet** is a future optimization for large teams or BI pipelines; defer until data sizes justify.

### Privacy/redaction
- Add a `--redact` profile (e.g., `none`, `hash-content`, `strip-content`) that determines whether prompt/response text is included. Default should be conservative (strip or hash) for team sync.
- Redaction is applied at export; store hashes to preserve dedup/searchability without raw content.

## 5. What NOT to do (explicit avoid list)
- **Do not replace local SQLite** or require network availability for core usage.
- **Do not attempt bidirectional sync** (conflict semantics are hard; Postgres logical replication conflicts halt replication). [7]
- **Do not sync derived indexes** (FTS/embeddings); rebuild instead.
- **Do not require multi-writer replicas** as a baseline; keep multi-device sync explicit and opt-in.
- **Do not overfit a single hosting provider**; keep sync push generic (destination-agnostic) with a simple data contract.

## Concrete recommendations (short list)
1. Keep SQLite as the local DB. Document optional Turso/libSQL hosting as advanced mode; LiteFS only for server-grade setups; Postgres as analytics sink.
2. Add `siftd db` with operational tooling (stats/size, schema, backup/restore, vacuum/optimize, integrity checks).
3. Provide `siftd db backup/restore` as the default portability story; treat merge as an advanced, opt-in path with conflict reports.
4. Implement `siftd sync push` as **push-only NDJSON** with idempotent upserts and a local cursor; add privacy/redaction profiles.
5. Explicitly defer bidirectional sync, live replication, and syncing derived indexes.

## References
1. https://docs.turso.tech/libsql
2. https://docs.turso.tech/features/sqlite-extensions
3. https://docs.turso.tech/features/embedded-replicas
4. https://fly.io/docs/litefs/how-it-works
5. https://fly.io/docs/litefs/faq
6. https://www.postgresql.org/docs/current/logical-replication.html
7. https://www.postgresql.org/docs/current/logical-replication-conflicts.html
