# Embedding Benchmark Feature

Systematic evaluation of chunking strategies for `strata ask` retrieval quality.

## Goal

Given a natural language query, retrieve the most relevant chunks from ~5k conversations of coding assistant dialogue (mixed code + natural language, multi-turn).

## The Loop

```
Corpus Analysis → Hypothesis → Strategy → Build → Run → Review → Repeat
```

Each iteration tests one variable. The loop produces evidence for what improves retrieval discrimination, not just aggregate scores.

---

## Step 0: Corpus Analysis

Profile the existing data relative to the embedding model's constraints. This grounds all downstream decisions.

**Outputs:**
- Token length distribution of current chunks (not chars — tokens as the model sees them)
- Count/percentage of chunks exceeding model's `max_seq_length` (512 tokens for bge-small-en-v1.5)
- Distribution shape: uniform, bimodal, long-tail?
- Query token lengths from the benchmark query set (to measure query-chunk asymmetry)
- Content type breakdown: prose-heavy vs code-heavy vs mixed

**Why first:** A strategy targeting 256 tokens is meaningless if we don't know that N% of chunks are silently truncated at 512. Truncation is invisible — the model doesn't error, it just clips.

---

## Step 1: Hypothesis

A testable claim connecting a cause to an expected effect on retrieval quality.

**Structure:** "[Mechanism] causes [observed problem], so [intervention] will [measurable improvement]."

**Examples:**
- "Chunks exceeding 512 tokens are silently truncated, losing discriminating content. Splitting at 256 tokens will increase score spread."
- "Query-chunk size asymmetry (queries ~20 tokens, chunks ~200 tokens) compresses cosine similarity. Smaller chunks will widen the score distribution."
- "Long responses bury relevant sentences in boilerplate. Recursive splitting will surface them as individual retrievable units."

**Anti-examples (what we had before):**
- "Filtering short chunks improves discrimination" — no mechanism, no connection to model behavior.

---

## Step 2: Strategy Definition

A strategy encodes how to transform raw conversation data into embeddable chunks, aware of the model that will embed them.

**Schema:**
```json
{
  "name": "descriptive-name",
  "hypothesis": "The testable claim this strategy evaluates",
  "model": {
    "name": "bge-small-en-v1.5",
    "max_seq_length": 512,
    "dimension": 384
  },
  "chunking": {
    "method": "recursive | sentence | fixed | turn-boundary",
    "target_tokens": 256,
    "max_tokens": 512,
    "overlap_tokens": 0,
    "separators": ["\n\n", "\n", ". ", " "],
    "code_block_handling": "atomic | split-at-lines",
    "chunk_types": ["prompt", "response"]
  },
  "filters": {
    "min_tokens": 10,
    "exclude_patterns": []
  }
}
```

**Key properties:**
- Size is in **tokens**, not chars. The model operates on tokens.
- `max_tokens` must equal or be less than `model.max_seq_length`. This is validated at build time.
- `target_tokens` is the desired chunk size. Chunks may be smaller (short turns) but never larger than `max_tokens`.
- `overlap_tokens` controls how much context bleeds between adjacent chunks from the same turn.
- `method` controls how long turns are split when they exceed `target_tokens`.
- Model info is recorded so runs are reproducible and results are interpretable.

---

## Step 3: Build

Transform raw text → sized chunks → embeddings → stored DB.

**Pipeline:**
1. Extract text from main DB (by chunk_types)
2. Apply chunking method (split long turns, respect code blocks, apply overlap)
3. **Validate**: token-count each chunk, flag/reject any exceeding `max_tokens`
4. **Record stats**: chunk count, token distribution (min/max/mean/p50/p95), truncation count
5. Embed in batches
6. Store chunks + embeddings + build metadata

**Build metadata stored in DB:**
- Strategy (full JSON)
- Chunk token distribution stats
- Truncation count (chunks that would have exceeded max without splitting)
- Timestamp, backend model, dimension

**Tokenization:** Use the model's actual tokenizer (or a compatible fast tokenizer) for token counting. Character heuristics (chars/4) are unreliable, especially with code.

---

## Step 4: Run

Query the embeddings DB with the benchmark query set, record results.

**Recorded per run:**
- All strategy + build metadata (so runs are self-describing)
- Per-query: top-K results with scores, chunk text, chunk token length
- Aggregate: avg score, variance, spread (top1 - top5 avg), score distribution
- Query token lengths

**Key addition:** Record chunk token lengths alongside scores so we can correlate "did smaller chunks score differently than larger ones?"

---

## Step 5: Review

Quantitative + qualitative evaluation.

**Quantitative:**
- Score distribution: avg, variance, spread (same as now)
- Per-group breakdown (conceptual, technical, specific, etc.)
- Comparison table across runs

**Qualitative (new):**
- For N sample queries, inspect the actual top-5 retrieved chunks
- Are the right chunks ranking high? (Relevance judgment, even if manual)
- Identify failure modes: truncated chunks ranking high? Boilerplate scoring well?
- Check if score improvements correspond to better actual retrieval

**Qualitative matters because:** Aggregate metrics can mask reality. If all chunks score 0.73-0.76 regardless of relevance, improving the average from 0.74 to 0.76 means nothing. We need to verify that score differences correspond to relevance differences.

---

## Current State

### What exists (`bench/`)
- `bench/strategies/*.json` — filter-only params (min_chars, chunk_types, concat). Not real chunking strategies.
- `bench/build.py` — extracts blocks, filters by char length, embeds. No splitting, no token awareness.
- `bench/run.py` — queries DBs, computes aggregate metrics.
- `bench/queries.json` — 25 queries across 5 groups.

### Results so far
| Config | Chunks | Avg Score | Variance | Spread |
|--------|--------|-----------|----------|--------|
| baseline (min 20 chars) | 43,053 | 0.7486 | 0.0010 | 0.029 |
| min-100 chars | 24,558 | 0.7377 | 0.00087 | 0.027 |

- Flat score distribution — system doesn't discriminate relevant from irrelevant
- Filtering short chunks made things slightly worse
- concat-response was a no-op (data already 1 block per response)
- No token counting was done — unknown how many chunks are truncated

### Gaps
1. No token awareness (chars everywhere, model uses tokens)
2. No splitting (long turns embedded as-is, silently truncated)
3. No corpus profiling (don't know data shape vs model constraints)
4. Strategy params too narrow (filters, not chunking configs)
5. No qualitative evaluation (never inspected actual retrieved content)
6. Model constraints not encoded in strategy

---

## Implementation Order

1. **Corpus analysis script** — tokenize existing chunks, produce distribution stats, measure truncation. This tells us if truncation is the dominant problem.
2. **Strategy schema v2** — token-aware, with chunking method + model constraints.
3. **Build v2** — recursive splitting, token validation, stats recording.
4. **Run updates** — record chunk token lengths, model params.
5. **Review tooling** — qualitative inspection mode (show top-K chunk text for sample queries).

Step 1 is the immediate next action. It produces evidence that informs what hypothesis to test first.
