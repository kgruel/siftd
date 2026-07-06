# Embedding subsystem rebuild — design v2 (2026-07-04)

Status: REVIEW-FOLDED. v1 reviewed by a 4-lens adversarial panel (grounding-correctness,
architecture, surface/UX, retrieval-engineering); 1 critical + 13 major findings folded
below. Forks F1–F3 resolved by user. Successor to the deferred
"search-substrate-redesign" session (0.11.0 embed-first-class line). Grounding: six
territory maps + provider research (scratchpad `grounding/*.json`).

## Goals (user-set)

1. Embeddings available **by default** when the user provides an API key for an embedding
   service — no heavy local deps in base install. Config-driven, caveats-integrated.
2. `[embed]` extra = **local** embedding support (the heavier lift, opt-in).
3. Clean up the CLI surface around index management.
4. Tuned, sensible default search both **without** embeddings (FTS) and **with** them
   (hybrid becomes the default engine).
5. Lower priority: bench as a usable toolbox; stats/overview of embedding config/storage.

## Resolved forks (user, 2026-07-04)

- **F1:** numpy → base dependencies. **Approved.**
- **F2:** the verb is **`siftd embed`** (also independently recommended by review —
  `index` collides with the established "FTS index" vocabulary in docs and `siftd ingest
  --rebuild-fts`).
- **F3:** RRF fusion as the hybrid default, gated on bench validation. **Approved.**

## Ground truth that shapes the design

- **OpenAI `/v1/embeddings` is the universal wire target** — all surveyed providers
  (Voyage ~native, OpenAI, Jina native, Mistral native, Gemini compat, Cohere compat) and
  all four local servers (Ollama, llama.cpp, vLLM, LM Studio) speak it. `httpx` is
  already a base dep. One generic client covers the matrix.
- **Query/document asymmetry is the #1 silent-quality trap** (param for Voyage/Cohere/
  Gemini-001; literal text prefixes for nomic/qwen3; no-op for OpenAI/Mistral/bge).
- **Current incremental indexing is broken**: any-chunk-row ⇒ permanently "indexed";
  appends invisible. The live index is 173MB with zero chunks and nothing noticed.
- **No ANN needed**: ≤40k chunks × 1024-dim float32 ≈ 160MB matrix, tens of ms brute
  force. Matryoshka gives headroom.
- **"Hybrid" today is candidate-narrowing only**; fts scores (`abs(rank)`, unbounded) and
  cosine scores are incomparable; `--threshold` meaningless in fts mode.
- **Two `hybrid_search` impls**: live `api/search.py:1011`; legacy `siftd/search.py:336`
  dead in production (one test import) — but `siftd/search.py` hosts shared primitives
  imported by the live engine AND ~8 test files.
- `embeddings_available()` == "fastembed importable"; chunking hard-instantiates a bge
  tokenizer regardless of backend; Ollama backend unbatched.
- Seams to ride: `resolve_search_mode()`; `index_meta` K/V; `credentials.
  resolve_token_ref()`; `_CONFIG_SCHEMA` + doctor validators; caveat registry;
  3-lane tests; `siftd/data/pricing.toml` as the reference-data precedent.

## Architecture

### 1. Backend layer: one remote client + one local engine

```
siftd/embeddings/
├── base.py        # protocol + deterministic resolution (rewritten)
├── remote.py      # NEW: generic OpenAI-compat client (httpx, base install)
├── fastembed_backend.py  # local ONNX ([embed] extra), adapted
├── chunker.py     # tokenizer-decoupled (estimator), same strategies
├── indexer.py     # rewritten lifecycle (fingerprints, per-batch txns)
└── availability.py  # embedding_status(): config/deps-driven, no probing
siftd/data/embed_presets.toml   # NEW: provider presets as reference data
```

**`RemoteBackend` (base install, zero new deps).** Generic OpenAI-compatible
`POST {base_url}/embeddings` on httpx: Bearer auth, array input, `dimensions`
passthrough, retry/backoff, batching within provider item/token ceilings.

**Presets are data, not code** (pricing.toml precedent): `siftd/data/embed_presets.toml`
carries `{base_url, default_model, default_dimensions, intent_style}` per preset; code
dispatches only on the `intent_style` enum (`none | param:input_type | param:task |
prefix`). Users can extend via `custom` without code changes.

| preset    | base_url                                        | default model            | default dim | intent_style |
|-----------|--------------------------------------------------|--------------------------|-------------|--------------|
| `voyage`  | api.voyageai.com/v1                              | `voyage-4-lite`          | 1024        | param:input_type |
| `openai`  | api.openai.com/v1                                | `text-embedding-3-small` | 1536        | none |
| `gemini`  | generativelanguage.googleapis.com/v1beta/openai  | `gemini-embedding-001`   | 3072        | none (compat layer) |
| `jina`    | api.jina.ai/v1                                   | `jina-embeddings-v3`     | 1024        | param:task |
| `mistral` | api.mistral.ai/v1                                | `mistral-embed`          | 1024        | none |
| `ollama`  | localhost:11434/v1                               | (must set `embed.model`) | (probe)     | prefix (per model family) |
| `custom`  | (must set `embed.base_url`)                      | (must set `embed.model`) | (probe)     | prefix via config |

Dimension resolution: config `embed.dimensions` > preset default > learned from first
real embedding response (ollama/custom; recorded to index_meta thereafter). Preset
default models/dims verified against provider docs at implementation time.

**Dissolution:** `ollama_backend.py` deleted — Ollama is a preset of the generic client
(array input fixes its unbatched-POST-per-chunk for free). Cohere deferred (compat
endpoint works via `custom`).

**Backend protocol (breaking, intent-aware):**

```python
class EmbeddingBackend(Protocol):
    name: str        # "remote:voyage", "remote:ollama", "fastembed"
    model: str
    dimension: int
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
```

Intent required at the call site (indexer → documents, search → query); backend maps to
param/prefix/no-op. The duplicated Protocol in `api/search.py` is deleted.
**`FastembedBackend`** keeps `bge-small-en-v1.5` (prefix-free by design — safest local
default), adapted to the protocol.

### 2. Config (new `[embed]` table)

```toml
[embed]
backend = "voyage"              # voyage|openai|gemini|jina|mistral|ollama|fastembed|custom|off
api_key = "env:VOYAGE_API_KEY"  # credentials.resolve_token_ref grammar (env:/file:/literal)
model = "voyage-4-lite"         # optional; preset default otherwise
dimensions = 1024               # optional; provider matryoshka truncation
base_url = ""                   # custom/self-hosted override
auto_index = true               # incremental embed at end of ingest (steady-state only, §3)
db_path = ""                    # optional embeddings.db override (mirrors db.path)
query_prefix = ""               # custom/ollama prefix-style models
document_prefix = ""
```

- `_CONFIG_SCHEMA` entries + doctor validator (backend membership, key resolvability,
  dimensions sanity — `ui.theme` exemplar). `embed.db_path` closes the existing
  paths asymmetry (db.path overridable, embeddings_db_path hardcoded) now, not later.
- API key rides `resolve_token_ref()` — no third env-grammar copy. (Pre-existing residue:
  `serve/auth.py` duplicates `env:` inline; dissolved in the slice that touches secrets.)

**Resolution is deterministic — no probing chain.** `embed.backend` set ⇒ that backend;
*configuration* failures (bad key ref, missing model for ollama/custom) are errors, not
fallthrough. Unset ⇒ `fastembed` if importable, else none. Remote is only ever active by
explicit config — configuring it *is* the data-egress opt-in. `get_backend()`'s
try-in-order chain + module-global `preferred`-keyed cache die.

**Availability:** `embedding_status() -> EmbedStatus {backend_name|None, usable, reason}`
— answers "is a backend *configured/installed*", NOT reachability (see §5 for
runtime-failure degrade). `resolve_search_mode(requested, has_embeddings=status.usable)`
call sites unchanged. All "install siftd[embed]" strings become status-aware.

### 3. Index lifecycle

**Fingerprint staleness** (fixes the append bug). New embed-DB table:

```sql
indexed_state(conversation_id TEXT PRIMARY KEY, fingerprint TEXT,
              chunk_count INT, indexed_at TEXT)
```

Fingerprint = `(event_count, max(events.timestamp), event_content_count)` per
conversation — cheap SQL; the `event_content` count catches block-level appends to
existing events (ingest is INSERT-OR-IGNORE on both events and (event_id, block_index)),
which an events-only fingerprint would miss. Stale/new ⇒ delete conversation's chunks →
re-chunk → re-embed → upsert state, **committed per batch**. Prune rides the same diff.

**Identity meta writes FIRST.** backend/model/dimension/schema_version land in the
*first* commit, before any chunk rows — an interrupted build is always self-describing.
Additionally `_validate_incremental_compat` treats "chunks exist but identity meta
absent" as rebuild-required (closes the None-short-circuit backend-mixing hole that
per-batch commits would otherwise open).

**Chunking decoupled from fastembed.** Token estimator (chars/4-class heuristic) replaces
the mandatory bge ONNX tokenizer load. Safety margins per backend: fastembed target
256 / max **384** estimator-tokens (bge hard 512 ceiling vs estimator error on
token-dense content); remote presets keep 256/512 (8k–32k ceilings) + provider truncation
enabled. Chunk strategies (exchange-window + tool_summary) carry over — with one change:
**`source_ids` widens to all constituent event ids** (prompt + response events per
window; see §5 — required for the fusion bridge; costs nothing since v2 is
rebuild-only). tool_summary keeps `source_ids=[]` (vector-only; FTS never indexes tool
content, so no bridge exists to need).

**Build-on-ingest — steady-state only.** Post-rollup hook in `ingest_all()`: when a
backend is configured + `auto_index=true` **and an index already exists**, run the
incremental indexer over new/stale conversations through the existing progress consumer.
If no index exists (or backlog > ~200 conversations), *skip* inline work and emit a
caveat: "N conversations awaiting embedding — run `siftd embed`" — the first-run backlog
(potentially ~40k chunks against a 3-RPM free tier) must never hang ingest. Failures
(rate limit, network) never fail ingest; caveat + hint, state stays consistent. **First
auto-index run emits a one-time notice** ("auto-indexing sends new conversation content
to <provider>; disable with embed.auto_index=false") — configuration is consent, but the
first egress is announced (explicit over implicit).

**Embed-DB schema v2** (adds `indexed_state`, widened source_ids semantics).
SCHEMA_VERSION 1→2; derived data ⇒ no migration: v1 detected on open → rebuild-required
error with fix command. New doctor check for the empty-index-file case (chunks=0, file
large ⇒ rebuild/vacuum hint).

### 4. CLI re-home: `siftd embed`

```
siftd embed              # incremental build (new + stale + prune)
siftd embed --rebuild    # from scratch
siftd embed --status     # stats/overview surface (§6)
siftd embed --embed-db PATH   # power-user override
```

Coexists with `siftd install embed` (install the capability vs run the indexer; help
text cross-references). **Not an Operation deliberately**: embed build/status mutate/read
machine-local derived state with the local key — nothing to delegate, no wire path
(unlike search's shared-data query surface). `cli/embed.py` routes **only through
`siftd.api`** (`api.build_index`, new `api.embed_status`) — zero new architecture
violations.

`siftd search` loses `--index/--rebuild/--backend/--embed-db` (hard error + redirect
hint, `--context`→`--around` precedent). `--backend` dies everywhere — backend is config.
Wire: `backend`/`embed_db` leave the search OpSpec (+ `test_op_route_parity` update).

**String/dispatch sweep (same slice, completeness gate = grep `'siftd search --'` over
src/ + tests/ + docs/):**
- `cli/data.py:_FIX_REGISTRY` — doctor `--fix` dispatches on the EXACT fix_command
  literal; keys `"siftd search --index"`/`"--rebuild"` → re-key to `siftd embed`
  equivalents, move `_fix_search_*` bodies/labels.
- **Four** doctor checks (embeddings_available, embeddings_stale, embeddings_compat,
  **orphaned_chunks**) fix_commands.
- `storage/embeddings.py` IndexCompatError strings **and** `embeddings/indexer.py`
  IncrementalCompatError strings. The `--backend {stored}` remediations become *config
  instructions* ("set embed.backend = ..."), not flag substitutions.
- `cli/install.py` next-step hint; serve route error strings; `cli/search.py` help/
  ambiguous-args block; `api/caveats.py` fix_command.

### 5. Search defaults & scoring

**No embeddings:** `auto` → `fts` (mechanics unchanged; findability-hardened). Score
normalization: `abs(rank)/(1+abs(rank))` — bounded (0,1), **monotone increasing with
match quality** (bm25 rank is negative; more negative = better; v1's `1/(1+abs(rank))`
was inverted). Test asserts top SQL-ordered hit carries max normalized score.

**With embeddings:** `auto` → `hybrid` = **RRF rank fusion**, fully specified:

- **Two ranked lists over the filter-resolved candidate set:** (a) *vector*: chunks by
  cosine, **MMR applied here** (before fusion — MMR is a vector-list reranker; its
  same-conversation penalty and diversity operate on real embeddings only); (b)
  *keyword*: `search_content` FTS hits bridged to chunks.
- **Exact bridge (no fuzzy join):** chunk `source_ids` now carries all constituent event
  ids (§3), so an FTS hit on prompt OR response text maps to its covering chunk(s); the
  chunk's keyword rank = best bm25 rank among its bridged hits. FTS hits with **no**
  covering chunk (unindexed conversation, uncovered event) enter as FTS-only entrants.
  Dedup rule: a bridged hit never also appears as an entrant.
- **Fuse:** `fused = Σ 1/(60 + rank_i)` over the two lists. FTS-only entrants have no
  vector and **never pass through MMR** (they're exact-keyword hits; bounded count).
  `SearchChunk.score` = fused score; `ScoreBreakdown` gains `vector_rank`,
  `keyword_rank`, `fused_score`, keeps `embedding_sim`.
- **Threshold/first rewired to similarity, not score** (process_search_view changes —
  NOT "unchanged as today"): `--threshold` and `first_mention` (0.65 default) test
  `embedding_sim` where present; FTS-only entrants are exempt from similarity
  thresholds (thresholding them out would delete RRF's reason to exist). Recency
  weighting applies to the vector list pre-fusion.
- **Why RRF:** exact identifiers (error strings, function names, flags) live
  overwhelmingly in *response* text; narrow-then-rank can never let a strong keyword hit
  outrank a mediocre-cosine hit. RRF is scale-free — dissolves the bm25-vs-cosine
  incomparability.
- **Runtime remote failure ⇒ truthful degrade** (the new failure class remote backends
  introduce): if query-embed fails (network/rate-limit/revoked key) the query degrades to
  fts and the **envelope `mode` field is re-derived from the actually-executed engine
  after the failure** — never the pre-resolved value (owner-scoped requests get zero
  caveats, so the envelope field is the only honest channel there). Local CLI adds a
  caveat hint distinguishing "backend down" from "not configured". Deterministic
  *selection* (no silent model switching) is preserved; *reachability* blips degrade a
  single query, they don't hard-fail search. Config errors (bad key ref) remain errors.
- **Validation gate (moved into slice 4, not optional slice 6):** minimum bench rewire —
  route `bench/run.py` + the findability recall@10 smoke through the live
  `api.search.hybrid_search` (they currently call `storage.search_similar` directly and
  cannot measure fusion at all) — then A/B narrow-vs-RRF on the real corpus. If RRF
  materially regresses conceptual queries, ship narrowing as default and keep RRF behind
  `--mode`; measurement decides.

**Dissolution sweep (engine):** delete legacy `siftd.search.hybrid_search` +
`fts5_passthrough` (G2: delete). **`siftd/search.py` remains the shared-primitives
home** — mmr_rerank, apply_temporal_weight, filter_conversations, etc. stay put (module
has a real job; ~8 test files import primitives from it and the architecture taxonomy
names it; only the dead entry point is residue). Repoint `test_findability_review.py`;
drop the vestigial module-level `resolve_candidates` wrapper in `api/search.py`. Delete
`_mmr_rerank_python` (numpy is guaranteed in base now).

### 6. Truthfulness surfaces

- **`siftd embed --status`**: backend/model/dimension/provider, chunk counts by type,
  coverage (indexed/total + stale count), DB size, built_at.
- **`siftd status`**: embeddings line becomes backend-aware (backend, model, coverage %).
- **Caveats:** `embeddings-stale` → fingerprint-based, points at `siftd embed`; new
  producers: "auto-index skipped (backlog)" and "remote backend unreachable — degraded
  to fts". Engine `mode` stays the first-class owner-safe envelope field.
- **Serve delegation is server-authoritative** (documented, not implicit): a served
  instance uses ITS `[embed]` config; a delegated search downgrades truthfully via the
  envelope mode field. The client's key is never sent; server-side semantic search
  requires server-side config.
- **Doctor:** 4 checks updated + empty-index-file check + `[embed]` config validator.
- **Docs:** `docs/concepts/search.md` privacy claim rewritten *conditionally and
  prominently* (local-only guarantee holds iff backend ∈ {fastembed, ollama, custom-local,
  off}); install/storage/serve/data-model touch-ups; api.md regen in embed venv.

### 7. Packaging & architecture hygiene

- **numpy → base deps** (F1 approved). `[embed]` keeps fastembed/onnxruntime/tokenizers/
  huggingface-hub.
- **Lazy-import rationale restated**: `_LAZY_SEARCH_NAMES` machinery stays for *startup
  latency* (numpy import is tens of ms on `siftd query`/`tag` paths), no longer "numpy
  might be absent" — the three "avoid pulling in numpy" comments rewritten in the same
  slice. Pure-Python MMR fallback deleted (dead once numpy is guaranteed); `math.py`
  in-function numpy imports stay (latency).
- **Architecture ratchet**: 4 of 5 `KNOWN_VIOLATIONS` are already stale (verified — no
  live import matches); the rebuild touches exactly those files. Delete the stale
  entries, drop the ratchet cap to the real count, and `cli/embed.py` adds zero new
  violations (api-only imports).
- Remote-backend tests are **base-lane** (fake httpx transport, no network/extras);
  `embeddings` mark keeps meaning "needs fastembed".

### 8. Bench (low priority, last, optional)

Slice 4 already pulls the *minimum* rewire (live-engine routing) forward as the RRF gate.
The fuller refresh stays optional: strategy JSON gains backend/model/dimensions/prefix
fields via the new config resolution; commit one baseline run JSON; archive
`findability.py` (findings live in production + `test_findability_review.py`). Bench
stays an unpackaged, non-CI instrument.

## Slices (each: implement → 4-lane green → adversarial review → commit)

1. **Backend core** — protocol, RemoteBackend + presets-as-data, fastembed adapter,
   `[embed]` config + validator, `embedding_status()`, deterministic resolution, delete
   ollama_backend.py, numpy→base, lazy-rationale comment sweep, stale KNOWN_VIOLATIONS
   prune. *Test residue:* test_embeddings_base_edges (rewrite: resolution semantics),
   test_embeddings_ollama_backend_edges (→ test_embeddings_remote_edges, fake-httpx),
   test_embeddings.py (backend-name pins, protocol), test_embeddings_fastembed_backend_
   edges + test_search.py fake backends (embed/embed_one → new protocol),
   test_embeddings_availability, tests/architecture/test_imports (violations prune).
2. **Index lifecycle + CLI re-home** — schema v2 + indexed_state + identity-meta-first,
   fingerprints, per-batch txns, prune, chunker decoupling + widened source_ids,
   `siftd embed` verb + search-flag removal + full §4 string/dispatch sweep,
   `--status`. *Test residue:* test_embeddings_indexer_edges, test_chunker,
   test_op_route_parity (backend/embed_db keys), test_caveats/test_doctor/
   test_api_doctor (fix strings), CLI help snapshots ×3 pythons (search loses flags,
   embed command added).
3. **Ingest integration** — steady-state auto_index hook, backlog caveat, first-egress
   notice, failure caveats. *Test residue:* ingestion orchestration tests, caveat tests.
4. **Engine** — RRF per §5 (MMR-before-fusion, bridge, threshold rewire, fts
   normalization, runtime-degrade truthfulness), bench minimum-rewire + A/B gate, legacy
   engine deletion. *Test residue:* test_findability_review repoint, test_search/
   test_mmr_focus (fusion composition), new normalization/threshold/degrade tests.
5. **Truthfulness residue check** — status/doctor/caveats/docs sweep remainder.
6. **Bench fuller refresh** — optional.

## Decided (flagged, not asked)

- Voyage-first preset; OpenAI second (cost, free tier, input_type).
- Remote active only via explicit config (privacy stance); first-egress runtime notice.
- `auto_index=true` steady-state only; first-run backlog goes through explicit
  `siftd embed`.
- Brute-force cosine stays; no ANN at this scale.
- Cohere native deferred (`custom` covers its compat endpoint).
- Embed DB stays separate SQLite (derived, rebuildable); v1→v2 = rebuild, no migration.
- `siftd/search.py` survives as primitives home; only the dead engine is deleted.
- Serve delegation server-authoritative for embed config.

## OUTCOMES (2026-07-04)

All four implementation slices landed; this section is the arc's factual record.

- **F3 bench gate fired against narrow, not RRF.** The pre-committed rule (§5, §8) ran
  on a 275-conversation / 12,346-chunk A/B: RRF's `avg_top1` improved +106%
  (0.359→0.742) but tool-findability `recall@10` — the gate's only known-answer
  relevance metric — regressed −42% (0.200→0.117); identifier queries tied. Per the
  rule, narrow-then-rank **stays the shipped hybrid default**. RRF is fully implemented
  (`_fuse_hybrid`, exact `source_ids` bridge, MMR-before-fusion) but dormant behind
  `SIFTD_HYBRID_STRATEGY=rrf`, an experiment-only env knob deliberately absent from
  `--help`, config, and these docs. It dies or gets promoted by a future tuned re-gate
  (candidate angle: `avg_top1`'s structural bias toward RRF, since it ranks over the
  full candidate set rather than an FTS-narrowed one — noted in `_hybrid_strategy`'s
  docstring — makes it not a clean verdict on its own; a re-run would want a metric
  that isolates fusion quality from candidate-set size).
- **Auto-index hook lives in `api/ingest.py`, not `ingestion/`** — the architecture
  boundary forbids `ingestion/` → `embeddings/` imports, so the steady-state hook
  (§3) sits in the same post-rollup position at the API layer instead. No ratchet
  exception was needed; `cli/embed.py` adds zero new `KNOWN_VIOLATIONS`.
- **The verb is `siftd embed`** (F2), coexisting with `siftd install embed`
  (capability vs. execution). `siftd search` lost `--index`/`--rebuild`/`--backend`/
  `--embed-db` outright (hard error + redirect hint).
- **Slice SHAs:** slice 1 (backend core) `eb75d951`; slice 2 (index lifecycle + CLI
  re-home) `690258bb`; slice 3 (ingest integration) `ad42a300`; slice 4 (RRF engine +
  bench gate + legacy dissolution) `01c35092`. Slice 5 (this docs/residue pass) is
  uncommitted pending review.
- **Truthfulness residue found + fixed:** `siftd status`'s "Features" → "Embeddings"
  line (`cli/meta.py`) still read "installed"/"not installed" — accurate language for
  the old fastembed-only world, false now that a configured remote backend (zero local
  install) also satisfies `embeddings_available()`. Relabeled to "configured"/"not
  configured" with a hint covering both paths; not touched by slices 1–4. This was a
  string-level fix only — the fuller "backend/model/coverage %" `siftd status` line
  §6 originally sketched is a separate, larger surface (`siftd embed --status` already
  covers that detail) and stays out of scope for this pass.
- **Bench fuller refresh (§8) remains open** — optional, unpackaged, non-CI, tracked
  separately from this docs slice.
