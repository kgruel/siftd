# Storage

siftd stores everything in SQLite databases on your local filesystem. Understanding where data lives helps with backup, debugging, and understanding capacity.

## Where data lives

siftd follows the XDG Base Directory specification:

| Path | Purpose |
|------|---------|
| `~/.local/share/siftd/siftd.db` | Main database (conversations, tags, metadata) |
| `~/.local/share/siftd/embeddings.db` | Embeddings index (semantic search vectors) |
| `~/.config/siftd/` | Configuration, custom adapters, queries |
| `~/.cache/siftd/` | Reserved for cache files (currently unused; embedding backends manage their own caches) |

The main database contains everything except embeddings. You can delete `embeddings.db` and rebuild it with `siftd embed --rebuild` — it's derived data. `embed.db_path` in config overrides the default location, mirroring `db.path`.

```bash
siftd db path    # show all paths
```

## Why SQLite

SQLite gives you:

- **Portability** — single file, copy it anywhere
- **Durability** — ACID transactions, crash-safe
- **Queryability** — direct SQL access when you need it
- **No server** — runs in-process, no setup

The main database typically stays under 100MB even with thousands of conversations. Embeddings add ~500MB-1GB depending on volume.

## Two databases

siftd uses separate databases for conversations and embeddings:

**Main database (`siftd.db`):**
- Conversations, prompts, responses, tool calls
- Tags, workspaces, models, harnesses
- FTS5 full-text search index
- This is your primary data

**Embeddings database (`embeddings.db`):**
- Vector embeddings for semantic search
- Chunk metadata linking back to main database, plus per-conversation indexing state (a fingerprint of event count/timestamp/content that detects staleness, including appends to already-indexed conversations)
- Derived data — can be rebuilt from main database

Separation means you can:
- Back up just the main database (smaller, essential)
- Rebuild embeddings with a different model
- Delete embeddings to save space if you don't use semantic search

## Content deduplication

Tool call results can be large — a file read might return thousands of lines. The same content often appears multiple times (reading the same file in different sessions).

siftd stores large content in a content-addressable blob store:

```
tool_calls table                    content_blobs table
┌─────────────────────┐            ┌─────────────────────┐
│ id: 01JGK3...       │            │ hash: a1b2c3...     │
│ result_hash: ───────┼────────────│ content: "..."      │
│ ...                 │      ┌─────│ ref_count: 3        │
└─────────────────────┘      │     └─────────────────────┘
                             │
┌─────────────────────┐      │
│ id: 01JGK4...       │      │
│ result_hash: ───────┼──────┘
└─────────────────────┘
```

Content is keyed by SHA256 hash. If the same file content appears in 10 tool calls, it's stored once with `ref_count: 10`. When tool calls are deleted, the reference count decrements; blobs with zero references are garbage collected.

This keeps the database compact even when the same files are read repeatedly across sessions.

## File tracking

siftd tracks which log files have been ingested in the `ingested_files` table:

| Column | Purpose |
|--------|---------|
| `path` | Absolute path to the log file |
| `file_hash` | SHA256 hash of file contents |
| `conversation_id` | Which conversation it produced — `NULL` for a session-strategy source, whose row is a per-file marker for a container of many conversations |
| `error` | Parse error message, if any |

When you run `siftd ingest`:

1. Adapter discovers candidate files
2. For each file, check if path exists in `ingested_files`
3. If exists and hash matches, skip (already ingested)
4. If exists but hash differs, re-ingest (file changed)
5. If doesn't exist, ingest as new

This makes ingest idempotent — run it as often as you want without duplicating data.

## Transactions

Storage functions accept a `commit=False` parameter by default. The caller controls when to commit:

```python
# Batch multiple operations in one transaction
store_conversation(conn, conv1, commit=False)
store_conversation(conn, conv2, commit=False)
conn.commit()  # atomic: both or neither
```

This pattern enables:
- Atomic batch operations
- Rollback on error
- Better performance (fewer disk syncs)

The CLI commands handle transactions appropriately — you don't need to think about this unless using the library API.

## Direct SQL access

The database is standard SQLite. You can query it directly:

```bash
sqlite3 ~/.local/share/siftd/siftd.db
```

```sql
-- Recent conversations
SELECT id, started_at FROM conversations ORDER BY started_at DESC LIMIT 10;

-- Most active workspaces
SELECT w.path, COUNT(*) as count
FROM conversations c
JOIN workspaces w ON w.id = c.workspace_id
GROUP BY w.id ORDER BY count DESC;

-- Tool usage breakdown
SELECT t.name, COUNT(*) as count
FROM tool_calls tc
JOIN tools t ON t.id = tc.tool_id
GROUP BY t.id ORDER BY count DESC;
```

siftd also supports named SQL reports:

```bash
siftd report                 # list available reports
siftd report cost            # run the 'cost' report
```

Drop custom queries in `~/.config/siftd/queries/` as `.sql` files.

## The `siftd db` namespace

Database operations are grouped under `siftd db`:

| Command | Purpose |
|---------|---------|
| `db info` | Database file metadata (size, page count, journal mode, schema version, FTS5 status) |
| `db path` | Show database and config paths |
| `db vacuum` | Reclaim unused space |
| `db backup <path>` | Online backup (safe during concurrent access) |
| `db restore <path>` | Restore from a backup file |
| `db slice` | Export a filtered subset of the database |
| `db stats` | Conversation/prompt/response/tool call totals |

## Backup and restore

Back up the main database:

```bash
siftd db backup ~/backup/siftd-$(date +%Y%m%d).db
```

`db backup` uses SQLite's online backup API, which is safe for concurrent access — you can back up while siftd is running without risking corruption.

Restore from a backup:

```bash
siftd db restore ~/backup/siftd-20250115.db
```

The embeddings database can be rebuilt from the main database:

```bash
siftd embed --rebuild
```

## Database size

Typical sizes for reference:

| Content | Approximate size |
|---------|-----------------|
| 1,000 conversations | 10-50 MB |
| 10,000 conversations | 100-500 MB |
| Embeddings index | 500 MB - 1 GB |

The main drivers of size:
- **Tool call results** — file reads, command output (mitigated by deduplication)
- **Prompt/response text** — the actual conversation content
- **Embeddings** — fixed ~1.5KB per chunk, many chunks per conversation

If space is a concern:
- Delete old conversations you don't need
- Filter binary content during ingest (default behavior)
- Skip embeddings if you don't use semantic search

## Schema migrations

siftd handles schema migrations automatically on database open. Migrations are idempotent — running the same migration twice has no effect.

Current migrations:
- Labels → tags rename
- Add error column to ingested_files
- Add cascade deletes to foreign keys
- Add content_blobs table for deduplication
- Add tool_call_tags table

When you upgrade siftd, migrations run automatically on first use. No manual intervention needed.

## Health checks

Check database health:

```bash
siftd doctor
```

This runs checks like:
- FTS index consistency
- Orphaned records
- Stale ingested files
- Pending migrations

Fix issues:

```bash
siftd doctor fix
```

## Configuration

Configuration lives in `~/.config/siftd/config.toml`:

```bash
siftd config                  # show current config
siftd config path             # show config file location
siftd config set key value    # set a value
```

Common settings:
- `search.formatter` — default output format for search

## Custom resources

Drop-in customization directories:

| Directory | Purpose |
|-----------|---------|
| `~/.config/siftd/adapters/` | Custom log parsers |
| `~/.config/siftd/queries/` | Custom SQL queries |
| `~/.config/siftd/formatters/` | Custom output formatters |

Copy built-in resources to customize:

```bash
siftd copy adapter claude_code    # copy adapter to modify
siftd copy query cost             # copy query to modify
```
