# Search

`strata ask` implements a three-stage hybrid retrieval pipeline. All processing happens locally — no API calls.

## Pipeline

### Stage 1: FTS5 recall

SQLite's full-text search narrows the corpus to ~80 conversations whose text matches the query vocabulary. This is fast and eliminates clearly irrelevant content before the expensive embedding step.

Control the candidate pool size with `--recall`:

```bash
strata ask --recall 200 "error"    # wider pool for common terms
```

Skip FTS5 entirely with `--embeddings-only` when your query is conceptual and keyword matching would miss the point:

```bash
strata ask --embeddings-only "approaches to state management"
```

### Stage 2: Embeddings rerank

Each candidate chunk is scored by cosine similarity against the query embedding. This captures semantic meaning — "token refresh" matches "credential renewal" even without shared words.

**Model**: `BAAI/bge-small-en-v1.5` (384 dimensions). Chosen empirically — `bge-base` (768d) performed worse on this corpus.

**Chunking**: Exchange-window strategy. Prompt+response pairs are atomic units, accumulated into ~256-token windows with 25-token overlap. This produces 0% truncation and places 86% of chunks in the model's sweet spot.

### Stage 3: MMR diversity reranking

Maximal Marginal Relevance prevents the results from being dominated by a single conversation. It applies a two-tier penalty:

- **Same conversation**: hard penalty (1.0) — suppresses duplicate chunks from the same session
- **Cross-conversation**: cosine similarity penalty — prefers results that add new information

The λ parameter (default 0.7) controls the balance:

```bash
strata ask --lambda 0.9 "error handling"    # favor relevance
strata ask --lambda 0.5 "error handling"    # favor diversity
strata ask --no-diversity "error handling"   # disable MMR entirely
```

### Bench results

Three-way comparison across 50 queries:

| Metric | Pure Similarity | Hybrid FTS5+Rerank | Hybrid+MMR (default) |
|--------|----------------|-------------------|----------------------|
| Avg Score | 0.7319 | 0.7218 | 0.7082 |
| Conv Redundancy | 0.1460 | 0.2040 | **0.1040** |
| Unique Conversations | 8.3 | 6.9 | **9.9** |
| Unique Workspaces | 4.9 | 4.3 | **6.6** |
| Temporal Span (days) | 24.4 | 22.7 | **34.1** |
| Topic Clusters | 3.8 | 3.4 | **4.7** |

MMR trades ~0.024 average score for substantially better diversity across every metric. FTS5 pre-filtering without MMR actually *regresses* diversity (narrows the candidate pool). MMR fixes that regression.

## Embedding backends

- **fastembed** (default): Local ONNX inference. No API key, no network. Uses `BAAI/bge-small-en-v1.5`.
- **ollama**: Local model server. Requires ollama running with an embedding model. Uses `nomic-embed-text`.

```bash
strata ask --index --backend fastembed
strata ask --index --backend ollama
```

## Indexing

The embeddings index is stored in a separate SQLite database (`~/.local/share/strata/embeddings.db`). It's derived data — can be rebuilt from the main database at any time.

Indexing is explicit. strata never auto-builds on search.

```bash
strata ask --index       # incremental — only new conversations
strata ask --rebuild     # full rebuild from scratch
```

Use `--embed-db PATH` to work with alternate embeddings databases (useful for benchmarking or testing different strategies).

## Search modes

**Default** — ranked chunks with snippet, score, workspace, date.

**`--thread`** — two-tier narrative output. Top conversations (above-mean score clusters) are expanded with role-labeled exchanges. The rest appear as a compact shortlist. Best mode for research.

**`--context N`** — show ±N exchanges around the matching chunk. Use when you found the right area and need surrounding discussion.

**`--full`** — complete prompt+response exchange. Useful for reproduction, too noisy for research.

**`--chrono`** — sort by time instead of relevance. Traces how a concept evolved across sessions.

**`--conversations`** — aggregate scores per conversation, rank whole conversations instead of chunks.

**`--first`** — return the chronologically earliest match above threshold. Finds when a concept was first discussed.

**`--refs [FILES]`** — file references from tool calls in matching conversations. Shows files as they were when the LLM read/wrote them.

**`--json`** — structured JSON output for machine consumption.

**`--format NAME`** — named formatter (built-in or drop-in plugin from `~/.config/strata/formatters/`).

## Filters

All filters compose with each other and with output modes.

| Flag | Purpose |
|------|---------|
| `-w SUBSTR` | Workspace path substring |
| `-m NAME` | Model name |
| `--since DATE` | After date (ISO format) |
| `--before DATE` | Before date |
| `-l TAG` | Conversation tag (OR, repeatable) |
| `--all-tags TAG` | Require all tags (AND, repeatable) |
| `--no-tag TAG` | Exclude tag (NOT, repeatable) |
| `--role user\|assistant` | Filter by source role |
| `--threshold SCORE` | Minimum relevance score |
| `-n N` | Max results |
| `--no-exclude-active` | Include results from active sessions |

## Benchmarking

The `bench/` directory evaluates retrieval quality:

```bash
# Build embeddings DB per strategy
python bench/build.py --strategy bench/strategies/exchange-window.json

# Run benchmark (50 queries, 10 groups)
python bench/run.py --strategy bench/strategies/exchange-window.json embeddings.db

# A/B comparison
python bench/run.py --rerank mmr embeddings.db
python bench/run.py --rerank relevance embeddings.db

# View results
python bench/view.py runs/latest.json          # stdout summary
python bench/view.py runs/latest.json --html   # HTML report with score-coded cards
```

Measures: retrieval scores, conversation diversity, temporal span, chrono degradation, cluster density, pairwise similarity, cross-query overlap.

Query groups: conceptual, philosophical, technical, specific, exploratory, cross-workspace, broad-then-narrow, temporal-trace, tagged-subset, research-workflow.
