# tbd-v2 — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Current State

### What exists
- **Domain model**: `Conversation → Prompt → Response → ToolCall` dataclass tree (`src/domain/`)
- **Seven adapters**: `claude_code` (file dedup), `gemini_cli` (session dedup), `codex_cli` (file dedup), `cline` (file dedup), `goose` (session dedup), `cursor` (session dedup), `aider` (file dedup)
- **Adapter plugin system**: built-in + drop-in (`~/.config/tbd/adapters/*.py`) + entry points (`tbd.adapters`)
- **Ingestion**: orchestration layer with adapter-controlled dedup, `--path` for custom dirs
- **Storage**: SQLite with schema, ULIDs, schemaless attributes
- **Tool canonicalization**: 15+ canonical tools (`file.read`, `shell.execute`, etc.), cross-harness aliases
- **Model parsing**: raw names decomposed into family/version/variant/creator/released
- **Provider tracking**: derived from adapter's `HARNESS_SOURCE`, populated on responses during ingestion
- **Cache tokens**: `cache_creation_input_tokens`, `cache_read_input_tokens` extracted into `response_attributes`
- **Cost tracking**: flat `pricing` table (model+provider → rates), approximate cost via query-time JOIN
- **Labels**: manual labeling via CLI (`tbd label`), conversation and workspace scopes
- **FTS5**: full-text search on prompt+response text content
- **Semantic search**: `tbd ask` — embeddings in separate SQLite DB, Ollama/fastembed backends, incremental indexing
  - Explicit `--index`/`--rebuild` required (no auto-build)
  - `--embed-db PATH` for alternate embeddings databases
- **Logs command**: composable conversation browser with filters, drill-down, and multiple output formats
  - Filters: `-w` workspace, `-m` model, `-t` tool, `-l` label, `-q` FTS5 search, `--since`/`--before`
  - Output: default (short, one-line with truncated ID), `-v` (full table), `--json`
  - Drill-down: `tbd logs <id>` shows conversation timeline (prompts, responses, tool calls)
  - IDs: 12-char prefix, copy-pasteable for drill-down
- **Query runner**: `.sql` files in `~/.config/tbd/queries/`, `$var` substitution, missing var detection
- **CLI**: `ingest`, `status`, `search`, `logs`, `queries`, `label`, `labels`, `backfill`, `path`, `ask`
- **XDG paths**: data `~/.local/share/tbd`, config `~/.config/tbd`, queries `~/.config/tbd/queries`, adapters `~/.config/tbd/adapters`

### Benchmarking framework (`bench/`)
- **Corpus analysis**: `bench/corpus_analysis.py` — profiles token distribution using fastembed's tokenizer (with `no_truncation()` for true counts)
- **Chunker**: `src/embeddings/chunker.py` — token-aware splitting via `semantic-text-splitter` (Rust). Uses fastembed's tokenizer directly.
- **Strategies**: `bench/strategies/*.json` — v1 (char filters) and v2 (token-aware chunking with model constraints)
- **Build**: `bench/build.py --strategy <file>` — builds embeddings DB per strategy. Supports `--sample N` (conversation subset) and `--dry-run` (stats without embedding).
- **Runner**: `bench/run.py --strategy <file> <embed_db>...` — runs 25 queries, stores full chunk text + token counts in results
- **Inspector**: `bench/inspect.py <run.json> [--html]` — stdout summary or self-contained HTML report with score-coded cards, opens in browser
- **Queries**: `bench/queries.json` — 25 queries across 5 groups (conceptual, philosophical, technical, specific, exploratory)
- **Design doc**: `docs/dev/embed-bench-feature.md` — full pipeline design (corpus analysis → hypothesis → strategy → build → run → review)

### Data (current ingestion)
- ~5,289 conversations, 135k+ responses, 68k+ tool calls across 270+ workspaces
- ~710MB database at `~/.local/share/tbd/tbd.db`
- Harnesses: Claude Code (Anthropic), Codex CLI (OpenAI), Gemini CLI (Google)
- Models: Opus 4.5, Haiku 4.5, Sonnet 4.5, Gemini 3 pro/flash, GPT-5.2
- Top workspace: `gruel.network` (~700 conversations, 68M tokens)

### Files
```
tbd-v2/
├── tbd                         # CLI entry point
├── queries/
│   └── cost.sql                # Approximate cost by workspace
├── bench/
│   ├── queries.json            # 25 benchmark queries (5 groups)
│   ├── corpus_analysis.py      # Token distribution profiling (fastembed tokenizer)
│   ├── run.py                  # Benchmark runner (stores chunk text + token counts)
│   ├── build.py                # Strategy-based embeddings DB builder (--sample, --dry-run)
│   ├── inspect.py              # Run viewer: stdout summary or HTML report
│   ├── strategies/             # Strategy definitions (v1: char filters, v2: token-aware)
│   └── runs/                   # Benchmark output (gitignored)
├── docs/
│   ├── adapter-research-reference.md  # Pointer to tbd-v1 research
│   └── dev/
│       └── embed-bench-feature.md     # Embedding bench pipeline design
├── src/
│   ├── cli.py                  # argparse commands
│   ├── paths.py                # XDG directory handling
│   ├── models.py               # Model name parser
│   ├── domain/
│   │   ├── models.py           # Dataclasses (Conversation, Prompt, Response, etc.)
│   │   ├── protocols.py        # Adapter/Storage protocols
│   │   └── source.py           # Source(kind, location, metadata)
│   ├── adapters/
│   │   ├── __init__.py         # Adapter exports
│   │   ├── registry.py         # Plugin discovery (built-in + drop-in + entry points)
│   │   ├── claude_code.py      # JSONL parser, TOOL_ALIASES, cache token extraction
│   │   ├── codex_cli.py        # JSONL parser, OpenAI Codex sessions
│   │   ├── gemini_cli.py       # JSON parser, session dedup, discover()
│   │   ├── cline.py            # JSON parser, VS Code extension tasks, Anthropic API format
│   │   ├── goose.py            # SQLite parser, session-based, tool request/response pairing
│   │   ├── cursor.py           # SQLite KV parser, two-phase lookup, schema version detection
│   │   └── aider.py            # JSONL/markdown parser, chat history files
│   ├── embeddings/
│   │   ├── __init__.py         # Re-exports get_backend
│   │   ├── base.py             # EmbeddingBackend protocol + fallback chain resolver
│   │   ├── chunker.py          # Token-aware splitting (semantic-text-splitter + fastembed tokenizer)
│   │   ├── ollama_backend.py   # Local Ollama embedding models
│   │   └── fastembed_backend.py # Local ONNX inference via fastembed
│   ├── ingestion/
│   │   ├── discovery.py        # discover_all()
│   │   └── orchestration.py    # ingest_all(), IngestStats, dedup strategies
│   └── storage/
│       ├── schema.sql          # Full schema + FTS5 + pricing table
│       ├── sqlite.py           # All DB operations, backfills, label functions
│       └── embeddings.py       # Embeddings DB schema + cosine similarity search
└── tests/
    ├── test_models.py          # Model name parsing tests
    └── test_chunker.py         # Token-aware chunking smoke tests
```

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| `commit=False` default | Caller controls transaction boundaries |
| Adapter-controlled dedup | Claude/Codex=file (one convo per file), Gemini=session (latest wins) |
| Adapters pluggable, storage not | Adapters are plural and stateless; storage is the gravity well (SQL files, FTS5, attributes) |
| Module-level adapter conventions | Adapters are stateless parsers — constants + functions, no class instantiation needed |
| Hybrid plugin discovery | Drop-in dir for iteration, entry points for packaging — same pattern as queries |
| Domain objects as dataclasses | Simple, no ORM, protocol-based interfaces |
| FTS5 for text search | Native SQLite, no deps, prompt+response text only (skip thinking/tool_use) |
| Embeddings in separate DB | Expensive to compute, treat as persistent derived data, keep main DB clean |
| Brute-force cosine similarity | Correct first; ANN (faiss/hnswlib) only if corpus grows past ~100k chunks |
| Ollama → fastembed fallback | Prefer what's already running; fastembed as zero-config local fallback |
| Queries as .sql files | User-extensible, `string.Template` for var substitution |
| Tool canonicalization | Aliases enable cross-harness queries, unknown tools still tracked |
| Model parsing at ingest | Regex decomposition, structured fields enable family/variant queries |
| Cost at query time | No stored redundancy, immediate price updates, pricing table JOIN |
| Attributes for variable metadata | Avoids schema sprawl for provider-specific fields |
| Approximate cost, labeled explicitly | Flat pricing is useful now; precision deferred until billing context matters |
| Manual labels first | Auto-classification deferred until usage patterns justify LLM cost |
| `logs` as primary interface | Composable flags for 80% case, `queries` stays for power users (raw SQL) |
| Short mode as default | Dense one-liners with IDs; verbose table via `-v` |
| `search` folded into `logs -q` | FTS5 composes with other filters instead of being a separate command |
| No auto-build on `ask` | Explicit `--index` required. Indexing is expensive, shouldn't surprise the user. |
| Tokenizer from embedding model | Chunker uses the same tokenizer the model uses for embedding. No mismatch possible. Swap model → tokenizer follows. |
| Exchange as minimum unit | Prompt + response pair is the atomic chunk. Short exchanges accumulate into windows. Respects conversation structure. |
| External chunker library | `semantic-text-splitter` (Rust) — solved problem, not hand-rolled. Its `capacity=(target, max)` maps to strategy params. |
| WIP branches for sessions | Session work (handoff updates, tests, scratch) goes in `wip/*`, subtasks merge to main. |

---

## `tbd ask` Tuning — Status

### Problem
Baseline (43k chunks, bge-small-en-v1.5, 384-dim) produces flat score distribution:
- Avg 0.7486, variance 0.001, spread 0.029
- System doesn't meaningfully discriminate relevant from irrelevant
- Specific vocabulary queries work (XDG: 0.89, ULIDs: 0.85), broad queries return filler

### Root cause analysis (this session)
The v1 "strategies" (min-100, min-200-response-only, concat-response) were invalidated:
- **min-100**: made things slightly worse (avg 0.7377, spread 0.027)
- **concat-response**: no-op — data already has 1 text block per response, nothing to concatenate
- All were char-based filters, not actual chunking strategies

**Corpus analysis** (using fastembed's actual tokenizer with `no_truncation()`) revealed:
- 69% of chunks are <64 tokens — too small for meaningful embeddings
- 7.2% exceed 512 tokens — silently truncated by model (content lost)
- Median: 31 tokens. Mean: 193 tokens. Max: 29,577 tokens. Bimodal distribution.
- Benchmark queries average 12.8 tokens — massive query-chunk size asymmetry
- bge-small sweet spot is 128-256 tokens; only 18% of corpus falls in that range

### Current approach: exchange-window strategy
Pair prompt + response into "exchanges" (minimum meaningful unit), accumulate into token-bounded windows:

1. **Minimum unit**: prompt + its response = one exchange
2. **Accumulate**: fill a window with exchanges until hitting `target_tokens` (256)
3. **Oversized**: if a single exchange > `max_tokens` (512), split with `semantic-text-splitter`
4. **Conversation-bound**: never merge across conversations
5. **Tokenizer**: fastembed's paired tokenizer (exact same one used during embedding)

### Pipeline (the iteration loop)
```
Corpus analysis → Hypothesis → Strategy → Build → Run → Inspect → Repeat
```

Fast iteration via:
- `--dry-run`: chunk stats without embedding (seconds)
- `--sample N`: subset of N conversations (minutes vs 30+ for full)
- `--html` on inspect: visual review of top-K results

### In-flight subtasks
| Subtask | What | Status |
|---------|------|--------|
| `exchange-window-build` | Implement v2 build with exchange-window strategy | Building (embedding full corpus) |
| `bench-fast-iteration` | Add --sample and --dry-run flags | Building |

### What's next
1. Merge in-flight subtasks
2. Run exchange-window-256 benchmark, inspect HTML
3. Compare against baseline — does the token distribution shift improve discrimination?
4. If not: the model (bge-small, 384-dim) is the ceiling, not chunking. Next lever is a larger model.

### Key dependencies
- `fastembed` 0.7.4 — embedding + tokenizer (bundled, model-agnostic)
- `semantic-text-splitter` 0.29.0 — Rust-based chunker, accepts `tokenizers.Tokenizer` directly
- `tokenizers` 0.22.2 — HuggingFace Rust tokenizer (fastembed dependency)

---

## Remaining Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| `tbd ask` exchange-window experiment | In flight | Two subtasks building. First real experiment with token-aware chunking. |
| `tbd ask` model ceiling question | Blocked on above | If exchange-window doesn't improve discrimination, try bge-base (768-dim) or larger |
| New adapters: test & ingest | Next | 4 new adapters merged (cline, goose, cursor, aider) — need real ingestion test |
| Pricing table migration | Open | Schema defines `pricing` table but it doesn't exist in live DB. Needs migration or re-create. |
| `workspaces.git_remote` | Deferred | Could resolve via `git remote -v`. Not blocking queries yet. |
| `tbd enrich` | Deferred | Only justified for expensive ops (LLM-based labeling). |
| Billing context | Deferred | API vs subscription per workspace. Needed for precise cost, not approximate. |

See `ROADMAP.md` for phased priorities.

---

*Updated: 2026-01-23*
*Origin: Redesign from tbd-v1, see `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md`*
