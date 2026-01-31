# siftd ask — Full Reference

Semantic search over past conversations. Hybrid retrieval: FTS5 recall → embeddings rerank.

## Output modes

Control how results render. Modes are mutually exclusive except where noted.

**Default** — ranked chunks with snippet, score, workspace, date:
```bash
siftd ask "error handling"
```

**`--thread`** — two-tier narrative: top conversations expanded, rest as shortlist:
```bash
siftd ask "why we chose JWT" --thread
```
Best mode for research. Shows the reasoning in context, not isolated chunks.

**`--context N`** — show ±N exchanges around the matching chunk:
```bash
siftd ask "token refresh" --context 3
```
Use when you found the right area but need the surrounding discussion.

**`-v` / `--verbose`** — full chunk text instead of snippet:
```bash
siftd ask -v "the chunking algorithm"
```
Use when you need exact wording to quote or verify.

**`--full`** — complete prompt+response exchange:
```bash
siftd ask --full "schema migration"
```
Dumps entire exchanges. Useful for reproduction, too noisy for research. Prefer `--thread`.

**`--refs [FILES]`** — file references from tool calls in matching conversations:
```bash
siftd ask --refs "authelia setup"              # all file refs
siftd ask --refs HANDOFF.md "setup"            # only refs to specific file
siftd ask --refs HANDOFF.md,schema.sql "setup" # comma-separated file filter
```
Shows files as they were when the LLM read/wrote them — point-in-time snapshots, no git needed.

**`--chrono`** — sort by time instead of relevance score:
```bash
siftd ask --chrono "state management"
siftd ask --chrono --since 2024-06 "state management"
```
Traces how a concept evolved across sessions.

**`--json`** — structured JSON output:
```bash
siftd ask --json "error handling"
```
For machine consumption, piping to `jq`, or integration with other tools.

**`--format NAME`** — named formatter (built-in or drop-in plugin):
```bash
siftd ask --format compact "error handling"
```

## Filters

Narrow the candidate set before ranking. All filters compose with each other and with output modes.

**`-w` / `--workspace SUBSTR`** — filter by workspace path substring:
```bash
siftd ask -w myproject "auth flow"
siftd ask -w myproject --thread "auth flow"      # workspace + output mode
```
The single most impactful filter. Always use when you know the project.

**`-m` / `--model NAME`** — filter by model name:
```bash
siftd ask -m claude-3-opus "architecture"
```

**`--since DATE` / `--before DATE`** — date range:
```bash
siftd ask --since 2025-01 "migration"                    # after date
siftd ask --since 2025-01 --before 2025-06 "migration"   # window
```
Dates are ISO format or YYYY-MM-DD.

**`-l` / `--tag NAME`** — filter by conversation tag (OR logic, repeatable):
```bash
siftd ask -l research:auth "token expiry"                # single tag
siftd ask -l research:auth -l research:security "tokens" # either tag (OR)
```

**`--all-tags NAME`** — require all specified tags (AND logic, repeatable):
```bash
siftd ask --all-tags research:auth --all-tags review "token rotation"
```

**`--no-tag NAME`** — exclude conversations with tag (NOT logic, repeatable):
```bash
siftd ask --no-tag archived "error handling"
```

**`--threshold SCORE`** — cut results below relevance score:
```bash
siftd ask --threshold 0.7 "event sourcing"
```
Scores: 0.7+ on-topic, 0.6-0.7 tangential, <0.6 noise.

**`-n` / `--limit N`** — max results (default 10):
```bash
siftd ask -n 20 "error handling"
```

## Search modes

Change the unit of search or the ranking strategy.

**`--first`** — return chronologically earliest match above threshold:
```bash
siftd ask --first "event sourcing"
```
Finds when a concept was first discussed. Combine with `--threshold` to control noise.

**`--conversations`** — aggregate scores per conversation, rank whole conversations:
```bash
siftd ask --conversations "state management"
```
Returns conversations, not chunks. Use when you want to find which session discussed a topic most.

**`--embeddings-only`** — skip FTS5 recall, pure embeddings search:
```bash
siftd ask --embeddings-only "chunking strategy"
```
Bypasses the FTS5 pre-filter. Useful when FTS5 terms don't match your semantic intent.

**`--recall N`** — FTS5 conversation recall limit (default 80):
```bash
siftd ask --recall 200 "error"
```
Widens the candidate pool from FTS5 before embeddings rerank. Increase for broad/common terms.

## Diversity tuning

MMR (Maximal Marginal Relevance) reranking is on by default. It suppresses same-conversation duplicates and promotes cross-conversation diversity.

**`--lambda FLOAT`** — tune relevance vs diversity balance (default 0.7):
```bash
siftd ask --lambda 0.9 "error handling"    # more relevance, less diversity
siftd ask --lambda 0.5 "error handling"    # more diversity, less relevance
```
1.0 = pure relevance (no diversity penalty). 0.0 = pure diversity (ignore relevance).

**`--no-diversity`** — disable MMR, use pure relevance ranking:
```bash
siftd ask --no-diversity "error handling"
```
Equivalent to `--lambda 1.0`. Use when you want the highest-scoring chunks regardless of redundancy.

## Other options

**`--no-exclude-active`** — include results from currently active sessions:
```bash
siftd ask --no-exclude-active "current discussion"
```
Active sessions are excluded by default to avoid self-referential results.

**`--index`** — build/update embeddings index:
```bash
siftd ask --index
```

**`--rebuild`** — rebuild embeddings index from scratch:
```bash
siftd ask --rebuild
```

**`--backend NAME`** — embedding backend (ollama, fastembed):
```bash
siftd ask --backend ollama "error handling"
```

**`--embed-db PATH`** — alternate embeddings database path:
```bash
siftd ask --embed-db /path/to/alt.db "query"
```

## Composition examples

Filters, modes, and search options compose freely:

```bash
# Research a decision in a specific project, narrative view
siftd ask -w myproject --thread "why we chose JWT"

# Trace evolution of an idea over time in one workspace
siftd ask -w myproject --chrono --since 2024-06 "state management"

# High-relevance results only, with file references
siftd ask --threshold 0.7 --refs "schema migration"

# Search tagged conversations with context
siftd ask -l research:auth --context 2 "token rotation"

# Find earliest mention across all workspaces
siftd ask --first --threshold 0.65 "event sourcing"

# Cross-workspace comparison
siftd ask -w projectA "caching strategy"
siftd ask -w projectB "caching strategy"

# Exclude archived conversations, narrative view
siftd ask --no-tag archived --thread "authentication redesign"

# Date-filtered search
siftd ask --since 2025-01 "what should we do about"
```
