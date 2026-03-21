# Autoresearch Ideas

## Storage Coverage Efficiency (current: 0.74, baseline: 6.87)

### Remaining 36 uncovered lines

**Dead code (can't be covered):**
- queries.py:529 — `return None, None` after aggregate query (fetchone always returns Row)
- queries.py:570 — same pattern for `fetch_last_ingest_time`
- queries.py:176 — likely same pattern (responses empty branch)
- queries.py:246 — exchange with no text (would need prompt+response with zero text blocks)

**Require external infra/mocking (git, filesystem):**
- sqlite.py:603-621 — workspace git_remote lookup/update (needs real git repo)
- sqlite.py:1012-1014 — `get_worktree_branch` call (needs git worktree)

**Low-value internal paths:**
- sqlite.py:365 — `continue` in cascade migration for missing tables
- sqlite.py:512 — `ensure_push_log_table` (server-only feature)
- sqlite.py:556-557, 562-563 — `except (ImportError, AttributeError)` in cache clearing
- sqlite.py:686-697 — `get_or_create_tool` with kwargs (used internally, tested indirectly)
- sqlite.py:786 — `continue` when canonical tool not found in ensure_tool_aliases
- sqlite.py:939 — `json.dumps(filtered_data)` when filter_binary modifies data (needs base64 content)
- fts.py:214, 225-226 — `except Exception: pass` in recall (both AND and OR phases)
- tool_search.py:78 — `conn.commit()` in ensure_tool_search_tables (never called with commit=True)

### LOC compression opportunities
- Migration test schemas are bulky (~350 LOC for 12 tests). Could share a common base schema dict
- The LEGACY_SCHEMA constant in TestMigrateAddCascadeDeletes is ~80 lines
- Full migration integration test recreates most of the schema (~60 lines of CREATE TABLE)

## Non-storage ideas (future targets)
- Apply same metric to adapters/ (13% coverage, 1904 stmts)
- Apply same metric to api/ (23% coverage, 1709 stmts)
