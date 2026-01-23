# tbd-v2 — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Current State

### What exists
- **Domain model**: `Conversation → Prompt → Response → ToolCall` dataclass tree (`src/domain/`)
- **Two adapters**: `claude_code` (file dedup), `gemini_cli` (session dedup), both with model extraction
- **Ingestion**: orchestration layer with adapter-controlled dedup, `--path` for custom dirs
- **Storage**: SQLite with 19-table schema, ULIDs, schemaless attributes
- **Tool canonicalization**: 15 canonical tools (`file.read`, `shell.execute`, etc.), cross-harness aliases
- **Model parsing**: raw names decomposed into family/version/variant/creator/released
- **FTS5**: full-text search on prompt+response text content (~45k rows indexed)
- **Query runner**: `.sql` files in `~/.config/tbd/queries/`, `$var` substitution
- **CLI**: `ingest`, `status`, `search`, `queries`, `path`
- **XDG paths**: data `~/.local/share/tbd`, config `~/.config/tbd`, queries `~/.config/tbd/queries`

### Data (current ingestion)
- 5,258 conversations, 135k responses, 68k tool calls
- ~656MB database at `~/.local/share/tbd/tbd.db`
- Models: 80% Opus 4.5, 14% Haiku 4.5, 4.5% Sonnet 4.5, plus Gemini 3 pro/flash
- Top workspace: `gruel.network` at 61M tokens

### Files
```
tbd-v2/
├── tbd                         # CLI entry point
├── src/
│   ├── cli.py                  # argparse commands
│   ├── paths.py                # XDG directory handling
│   ├── models.py               # Model name parser
│   ├── domain/
│   │   ├── models.py           # Dataclasses (Conversation, Prompt, Response, etc.)
│   │   ├── protocols.py        # Adapter/Storage protocols
│   │   └── source.py           # Source(kind, location, metadata)
│   ├── adapters/
│   │   ├── claude_code.py      # JSONL parser, TOOL_ALIASES, discover()
│   │   └── gemini_cli.py       # JSON parser, session dedup, discover()
│   ├── ingestion/
│   │   ├── discovery.py        # discover_all()
│   │   └── orchestration.py    # ingest_all(), IngestStats, dedup strategies
│   └── storage/
│       ├── schema.sql          # Full schema + FTS5 virtual table
│       └── sqlite.py           # All DB operations, commit=False default
└── tests/
    └── test_models.py          # Model name parsing tests
```

---

## Open: Cost/Pricing Design

### Context
We have model + token counts per response. We want "how much did workspace X cost?"

### What's settled
- Cost is query-time computation (not stored enrichment)
- Provider per response is derivable from adapter's `HARNESS_SOURCE`
- `responses.provider_id` exists in schema but isn't populated yet
- Pricing data is user-provided (you know your billing arrangement)

### Proposed pricing table
```sql
CREATE TABLE pricing (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES models(id),
    provider_id TEXT NOT NULL REFERENCES providers(id),
    effective_date TEXT NOT NULL,
    input_per_mtok REAL,
    output_per_mtok REAL,
    cache_read_per_mtok REAL,
    cache_creation_per_mtok REAL,
    UNIQUE (model_id, provider_id, effective_date)
);
```

### Unresolved
User expressed "not sure this fits for me." Tension points:

1. **Is per-token pricing the right model?** The user's billing may be subscription/credits, not pure per-token. Token-level costing may be meaningless in that context.

2. **Provider axis complexity**: For direct API usage (Claude Code → Anthropic, Gemini CLI → Google), provider is trivially known. The `(model_id, provider_id)` key adds generality for OpenRouter/proxy cases that may not apply.

3. **Temporal dimension**: `effective_date` handles price changes over time. But has Anthropic pricing changed enough to justify this? Maybe a single rate per model is sufficient.

4. **Cache tokens**: Separate pricing for cache_read vs cache_creation adds precision but also complexity. The adapter doesn't extract these yet (would go in response_attributes).

### Questions to resolve next session
- Is per-response cost actually useful, or is "monthly spend by model" sufficient?
- Does the user pay per-token at all? (Subscription vs API billing)
- Should pricing just be a flat lookup (model → input_rate, output_rate) without provider/temporal dims?
- Should we start with attributes-based approach (store cost as computed attribute) for flexibility?

---

## Other Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| `providers` table | Schema exists, not populated | Needs adapter to set provider_id on responses |
| `*_attributes` tables | Purpose clarified | For cache tokens, provider-specific metadata |
| `labels` / `*_labels` | Schema exists, no write path | Future: manual or auto-classification |
| `workspaces.git_remote` | Column exists, empty | Could resolve via `git remote -v` |
| Queries UX | TODO in code | Unsubstituted `$vars` produce confusing errors |
| More adapters | Future | Codex CLI, Copilot, Cursor, Aider |
| Cache token extraction | Designed, not built | `response_attributes` with scope="provider" |
| `tbd enrich` | Only justified for expensive ops | Auto-labeling (LLM), not arithmetic |

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| `commit=False` default | Caller controls transaction boundaries |
| Adapter-controlled dedup | Claude=file (one convo per file), Gemini=session (latest wins) |
| Domain objects as dataclasses | Simple, no ORM, protocol-based interfaces |
| FTS5 for text search | Native SQLite, no deps, prompt+response text only (skip thinking/tool_use) |
| Queries as .sql files | User-extensible, `string.Template` for var substitution |
| Tool canonicalization | Aliases enable cross-harness queries, unknown tools still tracked |
| Model parsing at ingest | Regex decomposition, structured fields enable family/variant queries |
| Cost at query time | No stored redundancy, immediate price updates, pricing table JOIN |
| Attributes for variable metadata | Avoids schema sprawl for provider-specific fields |

---

*Updated: 2026-01-22*
*Origin: Redesign from tbd-v1, see `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md`*
