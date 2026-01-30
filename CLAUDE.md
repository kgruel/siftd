Personal LLM usage analytics. SQLite storage, FTS5 search, semantic search via embeddings.

## Setup

```bash
source .venv/bin/activate    # always activate before running tools
uv run pytest tests/ -v      # run tests
```

## Conventions

- `commit=False` default on storage functions; caller controls transactions
- ULIDs for primary keys (except `content_blobs` which uses SHA256 hash)
- XDG paths: data `~/.local/share/siftd`, config `~/.config/siftd`
- Adapters: implement `can_handle()`, `parse()`, `discover()`, set `HARNESS_SOURCE`
- Queries: `~/.config/siftd/queries/*.sql` with `$var` substitution

## Structure

```
src/siftd/
├── adapters/       # Log parsing (claude_code, gemini, etc.)
├── storage/        # SQLite ops, schema, blobs
├── embeddings/     # Semantic search (optional)
└── cli.py          # Thin dispatcher to subcommands
tests/              # Pytest, mirrors src structure
```

## Before You're Done

1. Run tests: `uv run pytest tests/ -v`
2. Commit all changes including lock files
3. Update PROGRESS.json with status
