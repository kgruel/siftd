# Search

siftd provides two search mechanisms: keyword search (FTS5) and semantic search (embeddings). They solve different problems and work together in hybrid mode.

## The two kinds of search

**Keyword search** finds exact matches. Search for "authentication" and you'll find conversations that contain that word. Fast, precise, no setup required.

**Semantic search** finds meaning matches. Search for "handling expired credentials" and you'll find conversations about token refresh, session expiry, and credential renewal — even if those exact words don't appear. Requires building an embeddings index.

Most searches benefit from both: keywords narrow the candidates, semantics rank by meaning.

## Keyword search (FTS5)

FTS5 is SQLite's full-text search engine. When siftd ingests conversations, it indexes all text content — prompts and responses — into an FTS5 virtual table.

```bash
siftd search --fts "authentication"
```

FTS5 search is:
- **Fast** — indexed, sub-second even with thousands of conversations
- **Exact** — finds the literal terms you search for
- **Available immediately** — no extra setup, works after first ingest

FTS5 supports standard search operators:
- `authentication error` — both terms must appear (implicit AND)
- `"token refresh"` — exact phrase
- `auth*` — prefix matching
- `auth OR oauth` — either term

Use keyword search when you know the specific terms that appeared in the conversation.

## Semantic search (embeddings)

Semantic search uses vector embeddings to match by meaning. Each chunk of text is converted to a high-dimensional vector; similar meanings produce similar vectors.

```bash
siftd search "how did I handle session timeout"
```

This finds conversations about session expiry, token refresh, keepalive logic — anything semantically related, regardless of exact wording.

### Setting up embeddings

Embeddings require additional dependencies:

```bash
pip install siftd[embed]
```

Then build the index:

```bash
siftd search --index
```

This processes all conversations, chunking text into ~256-token windows and computing embeddings for each. The index is stored in a separate SQLite database (`~/.local/share/siftd/embeddings.db`).

Everything runs locally. No API calls, no data leaves your machine.

### How chunking works

Conversations are split into overlapping chunks of roughly 256 tokens each. Chunking happens at exchange boundaries — a chunk typically contains a prompt/response pair with surrounding context.

Why chunk instead of embedding entire conversations?
- Long conversations would dilute the signal (one great answer buried in a long session)
- Smaller chunks give more precise results (find the specific exchange, not just "somewhere in this conversation")
- Overlap ensures ideas that span exchanges aren't lost at boundaries

### Embedding backends

siftd supports multiple embedding backends:

| Backend | Model | Notes |
|---------|-------|-------|
| `fastembed` | bge-small-en-v1.5 | Default, runs on CPU, ~130MB |
| `ollama` | nomic-embed-text | Requires Ollama running locally |

The backend is selected automatically (fastembed by default). You can override:

```bash
siftd search --backend ollama "query"
```

## Hybrid search

By default, `siftd search` runs in hybrid mode:

1. **FTS5 recall** — keyword search finds candidate conversations (default: top 80)
2. **Embedding ranking** — semantic similarity scores each chunk within candidates
3. **Reranking** — results are diversified to avoid redundancy

This gives you the precision of keyword matching with the flexibility of semantic understanding.

```
Query: "how did I handle token refresh"
              │
              ▼
    ┌─────────────────────┐
    │  FTS5 recall (80)   │  Find conversations mentioning these terms
    └─────────────────────┘
              │
              ▼
    ┌─────────────────────┐
    │  Embedding search   │  Score chunks by semantic similarity
    └─────────────────────┘
              │
              ▼
    ┌─────────────────────┐
    │  MMR reranking      │  Diversify to reduce redundancy
    └─────────────────────┘
              │
              ▼
         Top 10 results
```

### Skipping FTS5

If keyword matching is too restrictive (your query uses different words than the conversations), use embeddings only:

```bash
siftd search --embeddings-only "concept I can't name precisely"
```

This searches all indexed conversations, not just FTS5 matches. Slower but more comprehensive.

### Adjusting recall

The `--recall` flag controls how many conversations FTS5 passes to the embedding stage:

```bash
siftd search --recall 200 "error handling"
```

Higher recall means more candidates for embedding search — useful when FTS5 might miss relevant conversations.

## Diversity vs relevance

Search results often cluster — the top 10 might all be from the same conversation or cover the same subtopic. MMR (Maximal Marginal Relevance) reranking balances relevance with diversity.

```bash
siftd search "architecture decisions"         # default: λ=0.7 (mostly relevance)
siftd search --lambda 0.5 "architecture"      # more diversity
siftd search --no-diversity "architecture"    # pure relevance, no MMR
```

The lambda parameter (0.0 to 1.0) controls the balance:
- `1.0` — pure relevance, highest-scoring chunks first
- `0.7` — default, mostly relevance with some diversity
- `0.5` — balanced
- `0.0` — pure diversity, maximize difference between results

MMR also suppresses multiple chunks from the same conversation, so you see a broader range of sessions.

## Recency boosting

By default, old and new conversations are treated equally. Enable recency boosting to favor recent results:

```bash
siftd search --recency "testing patterns"
```

Recency uses exponential decay:
- Today's results get up to 15% boost
- Boost decays with a 30-day half-life
- Old results are never penalized below their base score

Tune the decay:

```bash
siftd search --recency --recency-half-life 7 "urgent topic"   # faster decay
siftd search --recency --recency-max-boost 1.3 "topic"        # stronger boost
```

## Filtering

Both search modes support filters to narrow results:

```bash
siftd search -w myproject "auth"              # workspace contains "myproject"
siftd search -m claude-opus "design"          # model matches
siftd search --since 2025-01-01 "refactor"    # date range
siftd search -l research: "patterns"          # tagged conversations only
```

Filters apply before search, reducing the candidate set.

## Output modes

Control how results are displayed:

```bash
siftd search "query"                    # default: chunk snippets with scores
siftd search -v "query"                 # verbose: full chunk text
siftd search --full "query"             # complete prompt+response exchange
siftd search --context 2 "query"        # show ±2 exchanges around match
siftd search --thread "query"           # expand top hits into conversation threads
siftd search --conversations "query"    # rank whole conversations, not chunks
```

The `--thread` mode is particularly useful for research — it shows the top conversations expanded as narratives, with a shortlist of other relevant sessions.

## When to use which

| Situation | Approach |
|-----------|----------|
| Know the exact terms | `siftd search --fts "exact phrase"` |
| Remember the concept, not the words | `siftd search "concept description"` |
| Exploring a topic broadly | `siftd search --embeddings-only "topic"` |
| Finding diverse examples | `siftd search --lambda 0.5 "pattern"` |
| Recent work on a topic | `siftd search --recency "topic"` |
| Narrowing to a project | `siftd search -w project "query"` |

## Score breakdown

For debugging or understanding results, use JSON output to see score components:

```bash
siftd search --json "query" | jq '.results[0].breakdown'
```

```json
{
  "embedding_sim": 0.8234,
  "recency_boost": 1.0,
  "pre_mmr_score": 0.8234,
  "mmr_penalty": 0.1523,
  "mmr_rank": 1,
  "final_score": 0.7312,
  "fts5_matched": true,
  "fts5_mode": "and"
}
```

This shows how the final score was computed: raw embedding similarity, recency boost, MMR diversity penalty, and whether FTS5 matched.
