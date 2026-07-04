# Search

siftd provides two search mechanisms: keyword search (FTS5) and semantic search (embeddings). They solve different problems and work together in hybrid mode.

## The two kinds of search

**Keyword search** finds exact matches. Search for "authentication" and you'll find conversations that contain that word. Fast, precise, no setup required.

**Semantic search** finds meaning matches. Search for "handling expired credentials" and you'll find conversations about token refresh, session expiry, and credential renewal — even if those exact words don't appear. Requires building an embeddings index.

Most searches benefit from both: keywords narrow the candidates, semantics rank by meaning.

## Keyword search (FTS5)

FTS5 is SQLite's full-text search engine. When siftd ingests conversations, it indexes all text content — prompts and responses — into an FTS5 virtual table.

```bash
siftd search --mode fts "authentication"
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

Note: siftd's FTS5 index uses the Porter stemmer (so "writing" can match "write"). If you upgrade from an older version, the next `siftd ingest` may rebuild the FTS index once to apply the tokenizer change.

Scores are normalized to a bounded 0–1 range (`abs(rank)/(1+abs(rank))` over SQLite's bm25 rank), monotone increasing with match quality — the best hit always carries the highest score, and `--threshold` means the same thing whether or not embeddings are in play.

## Semantic search (embeddings)

Semantic search uses vector embeddings to match by meaning. Each chunk of text is converted to a high-dimensional vector; similar meanings produce similar vectors.

```bash
siftd search "how did I handle session timeout"
```

This finds conversations about session expiry, token refresh, keepalive logic — anything semantically related, regardless of exact wording.

### Two backend tiers

siftd resolves an embedding backend from `~/.config/siftd/config.toml`'s `[embed]` table (or falls back to the local backend if it's installed):

- **Remote — base install, no extra dependencies.** Set `embed.backend` to a provider preset (`voyage`, `openai`, `gemini`, `jina`, `mistral`, `ollama`, or `custom`) and `embed.api_key`. siftd speaks the provider's OpenAI-compatible `/embeddings` endpoint over `httpx`, which is already a base dependency.
- **Local — the `[embed]` pip extra.** `siftd install embed` pulls in `fastembed`, `onnxruntime`, and a bundled ONNX model (`bge-small-en-v1.5`) that runs on CPU. No network calls, no account, no key.

Resolution is deterministic, not a probing chain: if `embed.backend` is set, that backend is used, and a bad config (unresolvable key, missing model for `ollama`/`custom`) is an error — it never silently falls back to something else. If `embed.backend` is unset, siftd uses the local `fastembed` backend when it's installed, otherwise embeddings are unavailable and search runs FTS5-only. **Configuring a remote backend is what turns on embeddings** — it's never activated implicitly.

### Privacy: when does data leave your machine?

**The local-only guarantee holds only when the backend is `fastembed`, or `ollama`/`custom` pointed at a `localhost` server, or embeddings are off.** In those cases, everything runs locally — no API calls, no data leaves your machine.

**Configuring any other remote preset (`voyage`, `openai`, `gemini`, `jina`, `mistral`, or a `custom` endpoint that isn't local) sends conversation content to that provider**: full text at index time (`siftd embed`, or auto-indexing on ingest) and your query text at search time. This is an explicit opt-in — setting `embed.backend` to a remote preset *is* the consent — but siftd also surfaces a one-time notice the first time auto-indexing actually sends content off-machine, before that first request goes out.

### Building the index

```bash
siftd embed
```

This processes new and changed conversations, chunking text into ~256-token windows and computing embeddings for each. The index is stored in a separate SQLite database (`~/.local/share/siftd/embeddings.db`). Run it again any time — it's incremental: only new or changed conversations are re-embedded, and conversations that no longer exist are pruned.

```bash
siftd embed --rebuild      # rebuild the whole index from scratch (e.g. after a backend switch)
siftd embed --status       # backend, model, coverage, staleness, size
```

If `embed.auto_index` is enabled (the default) and an index already exists, `siftd ingest` incrementally embeds new conversations at the end of each run — you don't normally need to run `siftd embed` yourself after the first build. The first run always goes through an explicit `siftd embed`: a first-time backlog can be tens of thousands of chunks, and auto-indexing deliberately skips it rather than hanging `ingest` against a rate-limited provider.

### How chunking works

Conversations are split into overlapping chunks of roughly 256 tokens each. Chunking happens at exchange boundaries — a chunk typically contains a prompt/response pair with surrounding context.

Why chunk instead of embedding entire conversations?
- Long conversations would dilute the signal (one great answer buried in a long session)
- Smaller chunks give more precise results (find the specific exchange, not just "somewhere in this conversation")
- Overlap ensures ideas that span exchanges aren't lost at boundaries

### Embedding backends

| Backend | Model | Notes |
|---------|-------|-------|
| `fastembed` | bge-small-en-v1.5 | Local, CPU, ~130MB. Default when no `embed.backend` is configured and the `[embed]` extra is installed. |
| `voyage` | voyage-4-lite | Remote (Voyage AI). Needs `embed.api_key`. |
| `openai` | text-embedding-3-small | Remote (OpenAI). Needs `embed.api_key`. |
| `gemini` | gemini-embedding-001 | Remote (Google, OpenAI-compatible endpoint). Needs `embed.api_key`. |
| `jina` | jina-embeddings-v3 | Remote (Jina AI). Needs `embed.api_key`. |
| `mistral` | mistral-embed | Remote (Mistral). Needs `embed.api_key`. |
| `ollama` | (set `embed.model`) | A local Ollama server by default (`localhost:11434`) — no key needed, no egress. Can point at a remote Ollama host via `embed.base_url`. |
| `custom` | (set `embed.model`) | Any OpenAI-compatible `/embeddings` endpoint — set `embed.base_url`. Local-only if the endpoint is. |

The backend is config-driven (`embed.backend`) — there's no per-search override. Switching backends changes the vector space, so `siftd embed --status` warns when the index was built with a different backend than the one currently configured, and `siftd embed --rebuild` is required to switch cleanly.

## Hybrid search

By default, `siftd search` runs in hybrid mode:

1. **FTS5 recall** — keyword search finds candidate conversations (default: top 80)
2. **Embedding ranking** — semantic similarity scores each chunk within candidates
3. **Reranking** — recency weighting, then MMR diversification

This gives you the precision of keyword matching with the flexibility of semantic understanding. FTS5 recall is a filter, not a scorer: it decides which conversations are eligible, and embedding similarity does the ranking within that set. A chunk with a strong exact-keyword match but a weak conceptual match to the rest of the query can't out-rank a chunk with a stronger cosine similarity — narrowing happens before ranking, not alongside it.

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
siftd search --mode semantic "concept I can't name precisely"
```

This searches all indexed conversations, not just FTS5 matches. Slower but more comprehensive.

### Adjusting recall

The `--recall` flag controls how many conversations FTS5 passes to the embedding stage:

```bash
siftd search --recall 200 "error handling"
```

Higher recall means more candidates for embedding search — useful when FTS5 might miss relevant conversations.

### When embeddings aren't reachable

`--mode auto` (the default) resolves to hybrid when a backend is configured and usable, otherwise FTS5 — and that resolution is reported honestly: the output's `mode` field (and the `[engine]` tag in terminal output) always names the engine that actually ran, never `auto`.

A remote backend introduces a failure mode a local one doesn't have: the provider can be unreachable, rate-limited, or the key can be revoked. If that happens mid-query, siftd degrades that single search to FTS5 rather than failing it outright — and still reports the truth: `mode` reflects `fts`, not the `hybrid` you asked for, and a caveat distinguishes "backend unreachable" from "no backend configured." A misconfigured backend (bad key reference, missing required model) is a different case and remains a hard error — silent model-switching is never acceptable, but a transient network blip shouldn't take search down.

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
siftd search --around "query" --turns -2:+2  # window ±2 turns around the match
siftd search --view thread "query"           # expand top hits into conversation threads
siftd search --view conversations "query"    # rank whole conversations, not chunks
```

The `--view thread` view is particularly useful for research — it shows the top conversations expanded as narratives, with a shortlist of other relevant sessions.

## When to use which

| Situation | Approach |
|-----------|----------|
| Know the exact terms | `siftd search --mode fts "exact phrase"` |
| Remember the concept, not the words | `siftd search "concept description"` |
| Exploring a topic broadly | `siftd search --mode semantic "topic"` |
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

This shows how the final score was computed: raw embedding similarity, recency boost, MMR diversity penalty, and whether FTS5 matched. `--threshold` and `--select=first` test `embedding_sim` directly (not `final_score`) for chunks that went through the embedding stage — that's the cosine-similarity scale ("0.7+ on-topic, 0.6–0.7 tangential, below 0.6 noise" as a rough guide). In FTS5-only mode, `embedding_sim` is absent and the normalized bm25 score (see above) is what's tested instead.
