# Autoresearch: Search Findability with Tool Content

## Objective

siftd currently embeds only text blocks from prompts and responses. Tool calls (Read, Write, Bash, etc.), their inputs, and results are stored in the DB but not indexed in FTS5 or embedded. This means tool-oriented queries like "conversations where pyproject.toml was read" or "sessions with bash errors" are invisible to semantic search.

We're measuring whether including tool metadata in the search surface improves **findability** for tool-usage-oriented queries.

## Metrics

- **Primary**: `recall_at_10` (fraction of relevant conversations found in top-10 results, averaged over 8 tool-usage queries; higher is better)
- **Secondary**: `hit_at_10` (fraction of queries where at least 1 relevant conversation was in top-10)

## How to Run

`./autoresearch.sh` — outputs `METRIC recall_at_10=X.XXXX` and `METRIC hit_at_10=X.XXXX`

- Builds embeddings DB limited to 500 conversations (~30-60s)
- Runs 8 ground-truth tool-usage queries
- Measures recall@10 for each

## Ground-Truth Queries

Generated programmatically from `tool_calls` table in `bench/findability.py`:

1. "conversations where files were read" → tool_name = 'file.read'
2. "conversations where shell commands were executed" → tool_name = 'shell.execute'
3. "conversations with tool errors or failures" → status = 'error'
4. "conversations editing source files" → tool_name = 'file.edit'
5. "conversations reading pyproject.toml configuration" → input LIKE '%pyproject.toml%'
6. "conversations running git commands" → tool_name='shell.execute' AND input LIKE '%git%'
7. "conversations searching with grep" → tool_name = 'search.grep'
8. "conversations writing new files" → tool_name = 'file.write'

## Files in Scope

- `src/siftd/embeddings/chunker.py` — primary target: add tool summary extraction
- `bench/findability.py` — ground-truth recall@10 benchmark (do not break logic)
- `autoresearch.sh` — build + run script
- `bench/build.py` — embeddings DB builder (may need to extend for new chunk types)
- `bench/strategies/` — strategy JSON files

## Off Limits

- `src/siftd/storage/` — storage schema/queries (don't modify)
- `src/siftd/cli*.py` — CLI code
- `tests/` — test files (not required to pass here, but don't break them gratuitously)
- `bench/findability.py` ground-truth SQL — this defines correctness, don't game it

## Constraints

- Keep autoresearch.sh total time under 5 minutes
- MAX_CONVS=500 for speed; can increase if build time is OK

## What's Been Tried

### Baseline
The baseline uses `exchange-window` strategy (prompt+response only, no tool data). Expects recall_at_10 ≈ 0 because tool usage patterns are not in the text index.

### Ideas to Try
1. **Tool summaries appended to exchange chunks** (Option A from design doc): add a brief summary of tool calls per conversation/exchange to the embedded text
2. **Separate tool_summary chunks per conversation**: one chunk per conversation summarizing all tools used (tool name + input key info)
3. **FTS5 integration**: index tool names and inputs in FTS5 (doesn't affect embeddings recall)
4. **Tool call count/type metadata prepended**: simple prefix showing which tools were used
