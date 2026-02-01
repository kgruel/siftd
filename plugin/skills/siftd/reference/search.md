# siftd search — Full Reference

Semantic search over past conversations. Hybrid retrieval: FTS5 recall → embeddings rerank.

## Output modes

Control how results render. Modes are mutually exclusive except where noted.

**Default** — ranked chunks with snippet, score, workspace, date:
```bash
siftd search "error handling"
```

**`--thread`** — two-tier narrative: top conversations expanded, rest as shortlist:
```bash
siftd search "why we chose JWT" --thread
```
Best mode for research. Shows the reasoning in context, not isolated chunks.

**`--context N`** — show ±N exchanges around the matching chunk:
```bash
siftd search "token refresh" --context 3
```
Use when you found the right area but need the surrounding discussion.

**`-v` / `--verbose`** — full chunk text instead of snippet:
```bash
siftd search -v "the chunking algorithm"
```
Use when you need exact wording to quote or verify.

**`--full`** — complete prompt+response exchange:
```bash
siftd search --full "schema migration"
```
Dumps entire exchanges. Useful for reproduction, too noisy for research. Prefer `--thread`.

**`--refs [FILES]`** — file references from tool calls in matching conversations:
```bash
siftd search --refs "authelia setup"              # all file refs
siftd search --refs HANDOFF.md "setup"            # only refs to specific file
siftd search --refs HANDOFF.md,schema.sql "setup" # comma-separated file filter
```
Shows files as they were when the LLM read/wrote them — point-in-time snapshots, no git needed.

**`--by-time`** — sort by time instead of relevance score:
```bash
siftd search --by-time "state management"
siftd search --by-time --since 2024-06 "state management"
```
Traces how a concept evolved across sessions.

**`--json`** — structured JSON output:
```bash
siftd search --json "error handling"
```
For machine consumption, piping to `jq`, or integration with other tools.

**`--format NAME`** — named formatter (built-in or drop-in plugin):
```bash
siftd search --format compact "error handling"
```

## Filters

Narrow the candidate set before ranking. All filters compose with each other and with output modes.

**`-w` / `--workspace SUBSTR`** — filter by workspace path substring:
```bash
siftd search -w myproject "auth flow"
siftd search -w myproject --thread "auth flow"      # workspace + output mode
```
The single most impactful filter. Always use when you know the project.

**`-m` / `--model NAME`** — filter by model name:
```bash
siftd search -m claude-3-opus "architecture"
```

**`--since DATE` / `--before DATE`** — date range:
```bash
siftd search --since 2025-01 "migration"                    # after date
siftd search --since 2025-01 --before 2025-06 "migration"   # window
```
Dates are ISO format or YYYY-MM-DD.

**`-l` / `--tag NAME`** — filter by conversation tag (OR logic, repeatable):
```bash
siftd search -l research:auth "token expiry"                # single tag
siftd search -l research:auth -l research:security "tokens" # either tag (OR)
```

**`--all-tags NAME`** — require all specified tags (AND logic, repeatable):
```bash
siftd search --all-tags research:auth --all-tags review "token rotation"
```

**`--no-tag NAME`** — exclude conversations with tag (NOT logic, repeatable):
```bash
siftd search --no-tag archived "error handling"
```

**`--threshold SCORE`** — cut results below relevance score:
```bash
siftd search --threshold 0.7 "event sourcing"
```
Scores: 0.7+ on-topic, 0.6-0.7 tangential, <0.6 noise.

**`-n` / `--limit N`** — max results (default 10):
```bash
siftd search -n 20 "error handling"
```

## Search modes

Change the unit of search or the ranking strategy.

**`--first`** — return chronologically earliest match above threshold:
```bash
siftd search --first "event sourcing"
```
Finds when a concept was first discussed. Combine with `--threshold` to control noise.

**`--conversations`** — aggregate scores per conversation, rank whole conversations:
```bash
siftd search --conversations "state management"
```
Returns conversations, not chunks. Use when you want to find which session discussed a topic most.

**`--embeddings-only`** — skip FTS5 recall, pure embeddings search:
```bash
siftd search --embeddings-only "chunking strategy"
```
Bypasses the FTS5 pre-filter. Useful when FTS5 terms don't match your semantic intent.

**`--recall N`** — FTS5 conversation recall limit (default 80):
```bash
siftd search --recall 200 "error"
```
Widens the candidate pool from FTS5 before embeddings rerank. Increase for broad/common terms.

## Diversity tuning

MMR (Maximal Marginal Relevance) reranking is on by default. It suppresses same-conversation duplicates and promotes cross-conversation diversity.

**`--lambda FLOAT`** — tune relevance vs diversity balance (default 0.7):
```bash
siftd search --lambda 0.9 "error handling"    # more relevance, less diversity
siftd search --lambda 0.5 "error handling"    # more diversity, less relevance
```
1.0 = pure relevance (no diversity penalty). 0.0 = pure diversity (ignore relevance).

**`--no-diversity`** — disable MMR, use pure relevance ranking:
```bash
siftd search --no-diversity "error handling"
```
Equivalent to `--lambda 1.0`. Use when you want the highest-scoring chunks regardless of redundancy.

## Other options

**`--no-exclude-active`** — include results from currently active sessions:
```bash
siftd search --no-exclude-active "current discussion"
```
Active sessions are excluded by default to avoid self-referential results.

**`--index`** — build/update embeddings index:
```bash
siftd search --index
```

**`--rebuild`** — rebuild embeddings index from scratch:
```bash
siftd search --rebuild
```

**`--backend NAME`** — embedding backend (ollama, fastembed):
```bash
siftd search --backend ollama "error handling"
```

**`--embed-db PATH`** — alternate embeddings database path:
```bash
siftd search --embed-db /path/to/alt.db "query"
```

## Composition examples

Filters, modes, and search options compose freely:

```bash
# Research a decision in a specific project, narrative view
siftd search -w myproject --thread "why we chose JWT"

# Trace evolution of an idea over time in one workspace
siftd search -w myproject --by-time --since 2024-06 "state management"

# High-relevance results only, with file references
siftd search --threshold 0.7 --refs "schema migration"

# Search tagged conversations with context
siftd search -l research:auth --context 2 "token rotation"

# Find earliest mention across all workspaces
siftd search --first --threshold 0.65 "event sourcing"

# Cross-workspace comparison
siftd search -w projectA "caching strategy"
siftd search -w projectB "caching strategy"

# Exclude archived conversations, narrative view
siftd search --no-tag archived --thread "authentication redesign"

# Date-filtered search
siftd search --since 2025-01 "what should we do about"
```
