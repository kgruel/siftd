# Autoresearch: siftd query speed

## Objective
Minimize wall-clock time for `siftd query` (10 results, no filters) against the real production DB at `~/.local/share/siftd/siftd.db`. This is the everyday "what did I work on?" command and currently takes ~3.6s.

## Metrics
- **Primary**: `total_ms` (ms, lower is better) — full wall clock for `siftd query`
- **Secondary**:
  - `startup_ms` — Python process startup + all imports before any SQL
  - `sql_ms` — SQLite query execution time for `list_conversations`
  - `attr_ms` — response_attributes subquery alone (the known hot path)

## How to Run
`./autoresearch.sh` — outputs `METRIC name=number` lines.

## Files in Scope
- `src/siftd/api/conversations.py` — `list_conversations` / `_list_conversations_impl`, the main SQL query
- `src/siftd/storage/queries.py` — `has_pricing_table` and other helpers called during query
- `src/siftd/storage/schema.sql` — add indexes here
- `src/siftd/storage/sqlite.py` — `open_database`, migrations
- `src/siftd/cli.py` — top-level dispatcher, eager imports
- `src/siftd/cli_query.py` — `cmd_query`, dispatches to `list_conversations`

## Off Limits
- `tests/` — must not be modified
- Any adapter files under `src/siftd/adapters/`
- The DB file itself (read-only for experiments; schema changes go via migration in sqlite.py)

## Constraints
- `./dev check` must pass (lint + tests excluding embeddings)
- No new third-party dependencies
- Output of `siftd query` must remain identical (same rows, same format)

## Known Bottlenecks (profiled)
1. **`response_attributes` scan — ~1930ms**: The subquery `SELECT response_id, MAX(...) FROM response_attributes WHERE key='cache_read_input_tokens' GROUP BY response_id` full-scans all 479K rows. The table has a UNIQUE index on `(response_id, key, scope)` but NO index on `key` alone. Adding `CREATE INDEX idx_response_attributes_key ON response_attributes(key, response_id, value)` should eliminate this.
2. **Import time — ~60ms**: `siftd.cli` eagerly imports `siftd.api` which pulls in all adapters (26ms), peek readers (5ms), etc. These aren't needed for `query`. Lazy imports under subcommand dispatch would help.
3. **Correlated scalar subqueries** for model name and prompt count — fast for limit=10 but don't scale.

## What's Been Tried
_(update as experiments run)_
