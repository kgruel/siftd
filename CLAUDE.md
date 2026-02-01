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
- Drop-in adapters via `~/.config/siftd/adapters/`

## Development

```bash
./dev setup        # Setup worktree (venv + deps)
./dev setup --embed  # Setup with embeddings (downloads model)
./dev lint         # Run ty + ruff
./dev test         # Run tests (excluding embeddings)
./dev test-all     # Run all tests
./dev docs         # Generate reference docs
./dev docs --check # Verify docs aren't stale
./dev check        # Lint + test (CI equivalent)
```

## Structure

```
src/siftd/
├── adapters/       # Log parsing per tool
├── storage/        # SQLite ops, schema, content blobs
├── embeddings/     # Semantic search (optional [embed] extra)
├── search.py       # Hybrid FTS5 + vector search
├── cli.py          # Thin dispatcher
└── cli_*.py        # Subcommand implementations
tests/              # Pytest, mirrors src structure
```

## Conventions

- `commit=False` default on storage functions; caller controls transactions
- ULIDs for primary keys (except `content_blobs` which uses SHA256 hash)
- XDG paths: data `~/.local/share/siftd`, config `~/.config/siftd`
- Adapters: implement `can_handle()`, `parse()`, `discover()`, set `ADAPTER_INTERFACE_VERSION = 1`
- Queries: `~/.config/siftd/queries/*.sql` with `$var` or `:var` substitution
- CLI is thin dispatcher; logic lives in `cli_*.py` submodules or `search.py`/`api.py`

## CLI Quick Reference

```bash
siftd ingest              # Import conversation logs from all adapters
siftd search "<query>"    # Semantic search (requires embeddings)
siftd query               # List recent conversations
siftd query -w proj -s "error"  # Filter by workspace, FTS5 search
siftd query <id>          # View conversation detail
siftd peek                # View live/recent sessions (bypasses DB)
siftd tag <id> <tag>      # Tag a conversation
siftd export --last       # Export most recent session
```

Run `siftd <cmd> --help` for full options.

## Before you're done

1. Run: `./dev check`
2. Commit all changes including lock files
