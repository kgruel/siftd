# Experiment design: search findability with tool content

## Motivation

siftd currently embeds only text blocks from prompts and responses. Tool use blocks (Read, Write, Bash, etc.), tool results, and thinking blocks are stored in the DB but not indexed in FTS5 or embedded. This means:

- "Find conversations where PRAGMA journal_mode was executed" → only found if discussed in text
- "Sessions with repeated Bash errors" → invisible to search
- "Conversations that read config.toml" → invisible unless explicitly mentioned

The hypothesis: including tool metadata in the search surface improves findability for tool-usage-oriented queries without degrading quality for conceptual/natural-language queries.

## What to measure

### Primary metric: recall@10
For queries with known-answer conversations, what fraction appear in the top 10 results?

This requires ground-truth labels — query/conversation pairs where we know the conversation is relevant.

### Secondary metrics
- **avg_top1** — cosine similarity of best result (must not regress for existing query types)
- **avg_redundancy** — conversation dedup quality (must not regress)
- **index_size_mb** — embedding DB size (track growth)
- **index_time_s** — time to rebuild embeddings (track growth)

## Ground-truth query construction

Build queries programmatically from the actual DB:

### Tool-usage queries (new category)
For each query, identify conversations that actually used the tool/pattern by querying `tool_calls` and `response_content`:

1. **Tool-by-name**: "conversations using the Read tool" → ground truth from `SELECT DISTINCT conversation_id FROM tool_calls WHERE tool_name = 'Read'`
2. **Tool-on-target**: "conversations that read config.toml" → ground truth from tool_calls WHERE input LIKE '%config.toml%'
3. **Error patterns**: "conversations with Bash errors" → ground truth from tool_calls WHERE status = 'error'
4. **Specific output**: "conversations where PRAGMA journal_mode appeared in tool output" → ground truth from content_blobs joined through tool_calls

### Existing query types (regression check)
Keep the existing 70 queries from bench/queries.json as a regression baseline.

## What to change in the pipeline

### Option A: Embed tool summaries alongside text
Add a new chunk_type "tool_summary" that concatenates tool metadata into embeddable text:
```
Tool: Bash
Input: git status
Result: (success, 42 chars)
---
Tool: Read
Input: src/siftd/config.py
Result: (success, 2847 chars)
```

Pros: Works with existing embedding infrastructure. Searchable via both FTS5 and semantic.
Cons: Increases chunk count significantly. Tool summaries may dilute embedding quality.

### Option B: Index tool content in FTS5 only
Add tool names, inputs, and result snippets to the FTS5 index without embedding them.
Use FTS5 for tool-specific recall, embeddings for semantic ranking.

Pros: No embedding cost increase. FTS5 is ideal for exact-match tool queries.
Cons: Requires re-enabling hybrid search for tool queries. Two search modes.

### Option C: Hybrid — tool metadata in FTS5, tool context in embeddings
- FTS5: Index tool names, input paths/commands, error messages
- Embeddings: Append a tool usage summary to the exchange text before chunking

Pros: Best of both. FTS5 handles exact matches, embeddings handle semantic.
Cons: Most complex. May need to revisit embeddings-only default.

## Recommended approach

**Start with Option A** — it's the simplest change (modify the chunker) and lets us measure the findability improvement before adding FTS5 complexity. If tool summaries in embeddings don't help for exact-match queries, pivot to Option B/C.

## Experiment structure

1. Build ground-truth dataset from actual DB (tool_calls table)
2. Baseline: run findability queries against current index → recall@10
3. Modify chunker to include tool summaries
4. Rebuild index
5. Re-run findability queries → measure recall@10 improvement
6. Re-run existing 70 queries → verify no regression

## Open questions

- How many tool calls per conversation on average? (determines chunk count growth)
- Should tool results be included or just tool name + input? (size vs signal trade-off)
- What token budget for tool summaries? (competing with exchange text for chunk space)
- Should tool summaries be separate chunks or appended to exchange chunks?
