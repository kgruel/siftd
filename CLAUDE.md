Personal LLM usage analytics. Ingests conversation logs from CLI coding tools into SQLite, queries via FTS5 full-text search and semantic search (optional embeddings).

## What it does

- **Ingest**: Discovers and parses conversation logs from multiple AI coding tools
- **Search**: FTS5 for keyword search, embeddings for semantic similarity
- **Query**: Filter by workspace, model, date, tags; export for review workflows
- **Analyze**: Tool usage patterns, cost tracking, session history

## Supported adapters

- Claude Code (`~/.claude/projects`)
- Aider (`~/.aider`)
- Gemini CLI (`~/.gemini/tmp`)
- Codex CLI (`~/.codex/sessions`)
- VSCode Chat (`~/.config/Code/User/History/chat/`)
- Pi Coding Agent (`~/.pi/agent/sessions`)
- OpenCode (`~/.local/share/opencode/opencode.db`)
- Copilot CLI (`~/.local/state/.copilot/session-state`)
- Drop-in adapters via `~/.config/siftd/adapters/`

## Development

```bash
./dev setup          # Setup worktree (venv + deps)
./dev setup --embed  # Setup with embeddings (downloads model)
./dev lint           # Run ty + ruff (with autofix)
./dev test           # Run base tests (excludes embeddings, serve, slow lanes)
./dev test-all       # Run all tests including embeddings
./dev docs           # Generate reference docs
./dev docs --check   # Verify docs aren't stale
./dev check          # Lint + test (CI equivalent, quiet by default)
./dev agent <template> <path>  # Launch agent with prompt template
```

Commands are discovered from `scripts/*.sh`. Add a command by creating `scripts/<name>.sh` with `# DESC: description` at the top.

## Structure

```
src/siftd/
├── adapters/       # Log parsing per tool (SDK in adapters/sdk.py)
├── api/            # Public API layer (CLI and serve consume this)
├── cli/            # CLI package — thin dispatcher + per-command modules
├── content/        # Content-block helpers (binary filtering)
├── data/           # Version-controlled reference data (pricing.toml)
├── doctor/         # Health check system (per-check modules)
├── domain/         # Domain models (Conversation, Usage, events)
├── embeddings/     # Semantic search (optional [embed] extra)
├── ingestion/      # Ingest orchestration over adapters
├── output/         # Format registry, terminal/markdown/json/html renderers
├── peek/           # Live session introspection (bypasses DB)
├── serialization/  # Serve-layer JSON formatting (architecture boundary)
├── serve/          # HTTP server (optional [serve] extra) — routes, auth, htmx UI
├── storage/        # SQLite ops, schema, content blobs
├── search.py       # Hybrid FTS5 + vector search, MMR reranking
├── config.py       # Config management (~/.config/siftd/config.toml)
└── safecall.py     # Unified exception handling
tests/              # Pytest, mirrors src structure
```

## Conventions

- `commit=False` default on storage functions; caller controls transactions
- ULIDs for primary keys (except `content_blobs` which uses SHA256 hash)
- XDG paths: data `~/.local/share/siftd`, config `~/.config/siftd`
- Adapters: implement `can_handle()`, `parse()`, `discover()`, set `ADAPTER_INTERFACE_VERSION = 1`
- Queries: `~/.config/siftd/queries/*.sql` with `$var` or `:var` substitution
- CLI is a package; logic lives in `cli/<command>.py` submodules
- API layer (`api/`) is the boundary — CLI and serve both consume it, neither touches storage directly
- Operation IR: `dispatch()` in `api/dispatch.py` — normalize→execute→render pipeline for all query commands

## CLI Quick Reference

```bash
siftd ingest              # Import conversation logs from all adapters
siftd search "<query>"    # Hybrid search; falls back to FTS5 without embeddings
siftd query               # List recent conversations
siftd search -w proj "error"    # Search content, filtered by workspace
siftd show <id>           # View conversation detail
siftd peek                # View live/recent sessions (bypasses DB)
siftd tag <id> <tag>      # Tag a conversation
siftd export --last       # Export most recent session
```

Run `siftd <cmd> --help` for full options.

## Before you're done

1. Run: `./dev check`
2. Commit all changes including lock files
