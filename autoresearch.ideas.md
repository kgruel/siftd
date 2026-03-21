# Autoresearch Ideas

## Storage Coverage Efficiency (current target: 0.48, down from 6.87)

### Further LOC compression
- The import block is ~100 lines (17% of total). Could use `from siftd.storage import sqlite as sq` style to shorten
- The `_conv()` factory is 20 lines — could be shortened
- Some test classes with single methods could be flattened

### Remaining uncovered lines (105 lines, mostly sqlite.py)
- **sqlite.py:225-392** (168 lines): `_migrate_add_cascade_deletes` — requires creating a legacy DB without CASCADE. Complex but would add ~168 covered lines
- **sqlite.py:159-176**: `_migrate_labels_to_tags` — requires old-style labels table
- **sqlite.py:73-76**: schema version check — requires DB with future version
- **fts.py:214-216, 225-226**: exception handling in FTS recall — need malformed FTS query
- **queries.py:176,246**: edge cases (no responses, empty exchange text)
- **sessions.py:41-43**: migration adding last_seen_at column

### Test speed optimization
- The `populated_db` fixture calls `store_conversation` which does many inserts. Consider a lighter fixture for tests that don't need full conversation data
- Could skip `open_database` migrations for test DBs by caching a template DB and copying it

## Non-storage ideas (future targets)
- Apply same metric to adapters/ (13% coverage, 1904 stmts)
- Apply same metric to api/ (23% coverage, 1709 stmts)
