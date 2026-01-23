# tbd-v2 — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Current State

### What exists
- **Domain model**: `Conversation → Prompt → Response → ToolCall` dataclass tree (`src/domain/`)
- **Three adapters**: `claude_code` (file dedup), `gemini_cli` (session dedup), `codex_cli` (file dedup)
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
- **Logs command**: composable conversation browser with filters, drill-down, and multiple output formats
  - Filters: `-w` workspace, `-m` model, `-t` tool, `-l` label, `-q` FTS5 search, `--since`/`--before`
  - Output: default (short, one-line with truncated ID), `-v` (full table), `--json`
  - Drill-down: `tbd logs <id>` shows conversation timeline (prompts, responses, tool calls)
  - IDs: 12-char prefix, copy-pasteable for drill-down
- **Query runner**: `.sql` files in `~/.config/tbd/queries/`, `$var` substitution, missing var detection
- **CLI**: `ingest`, `status`, `search`, `logs`, `queries`, `label`, `labels`, `backfill`, `path`, `ask`
- **XDG paths**: data `~/.local/share/tbd`, config `~/.config/tbd`, queries `~/.config/tbd/queries`, adapters `~/.config/tbd/adapters`

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
│   │   └── gemini_cli.py       # JSON parser, session dedup, discover()
│   ├── embeddings/
│   │   ├── __init__.py         # Re-exports get_backend
│   │   ├── base.py             # EmbeddingBackend protocol + fallback chain resolver
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
    └── test_models.py          # Model name parsing tests
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

---

## Remaining Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| `tbd ask` tuning | Next session | Needs manual testing — verify chunking, ranking, backend selection work in practice |
| Pricing table migration | Open | Schema defines `pricing` table but it doesn't exist in live DB. Needs migration or re-create. |
| `workspaces.git_remote` | Deferred | Could resolve via `git remote -v`. Not blocking queries yet. |
| More adapters (Copilot, Cursor, Aider) | Ready | Plugin system means these can be drop-in `.py` files in `~/.config/tbd/adapters/` |
| `tbd enrich` | Deferred | Only justified for expensive ops (LLM-based labeling). |
| Billing context | Deferred | API vs subscription per workspace. Needed for precise cost, not approximate. |

See `ROADMAP.md` for phased priorities.

---

*Updated: 2026-01-23*
*Origin: Redesign from tbd-v1, see `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md`*
