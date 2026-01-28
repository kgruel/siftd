# strata

Ingest and query conversation logs from LLM coding tools. Stores in SQLite, searches via FTS5 and embeddings.

## Install

```bash
pip install strata              # core (query, tags, ingest)
pip install strata[embed]       # with semantic search
```

## Usage

```bash
# Ingest logs from Claude Code, Gemini CLI, Codex, Aider
strata ingest

# List recent conversations
strata query -w .               # current workspace
strata query --since 7d         # last week

# Semantic search (requires [embed])
strata ask "how did I handle auth"
strata ask -w myproject "error handling"

# Tag and filter
strata tag 01JGK3 decision:auth
strata query -l decision:
```

## Supported Tools

- Claude Code
- Gemini CLI
- Codex CLI
- Aider

## Commands

| Command | Description |
|---------|-------------|
| `ingest` | Import conversation logs |
| `query` | List/filter conversations |
| `ask` | Semantic search |
| `tag` | Apply tags to conversations |
| `peek` | View conversation contents |
| `doctor` | Check configuration |

## Documentation

- [CLI Reference](docs/cli.md)
- [Configuration](docs/config.md)
- [Search Pipeline](docs/search.md)
- [Python API](docs/api.md)

## License

MIT
