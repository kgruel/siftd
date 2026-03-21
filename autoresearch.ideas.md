# Autoresearch Ideas

## Ingest performance (current target: 38s, down from 115s)

### Batch commits (high potential, complex)
- 6,419 individual commits cost ~5.5s even with sync=OFF
- Batch N files per commit (e.g., 100-200) would reduce to ~32-64 commits
- Challenge: errors in one file roll back the whole batch
- Tried savepoints: overhead negated the savings
- Possible approach: use Python 3.12+ autocommit mode with explicit BEGIN/COMMIT,
  catch errors per-file and re-execute just the failed file's rollback via savepoint

### Reduce JSON round-trips
- Adapters parse JSONL → Python dicts, then `store_conversation` re-encodes with `json.dumps`
- 605k json.loads (5.2s) + 408k json.dumps (1.5s) = 6.7s total
- Could pass raw JSON strings through for content blocks instead of parse→re-encode
- Requires adapter interface change (return raw JSON for blocks)

### Streaming JSONL parser
- `load_jsonl` reads entire file then parses each line
- For large files, a streaming approach could overlap I/O and parsing
- Most files are small though, so benefit may be marginal

### executemany for bulk inserts
- Currently each row is a separate `conn.execute()` call (1M total, 8.8s)
- Could collect rows per table and use `executemany` for prompt_content,
  response_content, content_fts, tool_calls
- Requires restructuring store_conversation to collect-then-flush

### Skip file hashing for unchanged mtime+size
- Currently hash every file even when mtime matches
- Could trust mtime+size pair as "unchanged" signal and skip SHA-256
- Risk: rare cases where content changes without mtime change (e.g., NFS)

## Non-ingest ideas (future targets)
- **Query startup**: lazy-import adapters only for ingest/peek commands (~20-30ms)
- **Denormalize conversation stats**: add prompt_count, response_count, total_tokens
  columns to conversations table to avoid response table scan on listing
