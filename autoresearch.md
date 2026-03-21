# Autoresearch: Ingest Performance

## Objective
Optimize the wall-clock time of a full fresh `siftd ingest` — parsing conversation logs from all adapters (Claude Code, Gemini CLI, Pi Agent, Codex CLI, etc.) and storing them into a fresh SQLite database.

The workload is ~6,400 files → ~6,150 conversations → ~145k responses → ~111k tool calls, producing a ~790MB SQLite database. Baseline is ~138 seconds.

## Metrics
- **Primary**: `ingest_s` (seconds, lower is better)
- **Secondary**: `conversations` (count, should remain constant — correctness check)

## How to Run
`./autoresearch.sh` — outputs `METRIC name=number` lines.

## Profiling Summary (baseline)
From cProfile of the baseline (134s):

| Bottleneck | Time | Root cause |
|---|---|---|
| Git subprocess calls | 45s (34%) | `get_git_remote_url()` spawns 7,101 subprocess calls to `git remote get-url origin` |
| SQLite execute | 24.9s (19%) | 1.47M individual `conn.execute()` calls |
| SQLite commits | 13.6s (10%) | 6,419 individual `conn.commit()` — one per file |
| File hashing (SHA-256) | 13.8s (10%) | `compute_file_hash()` for all source files |
| ULID generation | 7.1s (5%) | 793k `ulid()` calls with `os.urandom()` per ID |
| JSON encode/decode | 13.8s (10%) | 604k `json.loads` + 408k `json.dumps` |
| Content blob hashing | 3.9s | `store_content()` SHA-256 for every tool result |
| Binary content filter | 3.9s | regex on every tool result (`has_large_base64`) |

## Files in Scope
- `src/siftd/ingestion/orchestration.py` — main ingest loop, per-file processing
- `src/siftd/storage/sqlite.py` — `store_conversation()`, all `insert_*` functions, `compute_file_hash()`
- `src/siftd/storage/blobs.py` — content-addressable blob storage (`store_content`)
- `src/siftd/storage/fts.py` — FTS5 index inserts
- `src/siftd/storage/conversation_stats.py` — materialized stats rebuild
- `src/siftd/ids.py` — ULID generation
- `src/siftd/git.py` — `get_canonical_workspace_identity()`, `get_git_remote_url()`
- `src/siftd/content/filters.py` — binary content detection
- `src/siftd/adapters/*.py` — individual adapter parsers
- `src/siftd/domain/models.py` — domain model classes
- `src/siftd/model_names.py` — model name parsing

## Off Limits
- Test files (must not modify tests)
- Schema SQL file (schema.sql)
- CLI layer (cli.py, cli_data.py, cli_*.py)
- Domain model definitions (domain/*.py)

## Constraints
- All existing tests must pass (`./dev test`)
- Ingested conversation count must remain unchanged (correctness)
- No new external dependencies
- Database output must be compatible (same schema, same data)

## What's Been Tried
### Wins (cumulative: 115s → 38s, -67%)
1. **Cache workspace identity** (115→77s): LRU cache on `get_canonical_workspace_identity` via workspace_cache dict passed through ingest
2. **WAL mode + SYNCHRONOUS=NORMAL** (77→56s): SQLite journal_mode=WAL, synchronous=NORMAL
3. **ULID optimization** (56→56s): Batch random bytes, unrolled encoding loops
4. **Binary filter length check** (small win): Skip regex for strings <500 chars
5. **Blob storage timestamp** (small win): Share timestamp across batch
6. **Vocabulary caching** (56→53s): Cache harness/provider/model/tool/tag lookups in-process
7. **SQLite cache + mmap** (53→48s): cache_size=-64000 (64MB), mmap_size=256MB
8. **hashlib.file_digest** (48→46s): Faster file hashing via Python 3.11+ API
9. **temp_store=MEMORY** (46→45s): In-memory temp tables
10. **SYNCHRONOUS=OFF during ingest** (45→39s): Skip fsync during bulk operations
11. **Deferred FK checks** (39→38s): defer_foreign_keys=ON during ingest

### Tried and Discarded
- Batch commits with savepoints: correct but slower due to savepoint overhead
- Batch commits without savepoints: fast but loses data on errors
- 1MB hash buffer for file hashing: no improvement
- Inline insert functions: in noise range
- Disable WAL autocheckpoint: WAL grows too large, final checkpoint slow
- 8KB page size: in noise range
- Tool/model caching per-conversation: in noise range
