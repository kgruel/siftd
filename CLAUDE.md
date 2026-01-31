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

## Setup

```bash
source .venv/bin/activate
uv run pytest tests/ -v
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

## Before you're done

1. Run tests: `uv run pytest tests/ -v`
2. Commit all changes including lock files
