# siftd

Ingest and query conversation logs from LLM coding tools. Stores in SQLite, searches via FTS5 and embeddings.

Warning: This project is under active development and breaking changes may occur.

## Install

```bash
pip install siftd              # core (query, tags, ingest)
pip install siftd[embed]       # with semantic search
```

## Usage

```bash
# Ingest logs from Claude Code, Gemini CLI, Codex, Aider
siftd ingest

# List recent conversations
siftd query -w .               # current workspace
siftd query --since 7d         # last week

# Semantic search (requires [embed])
siftd ask "how did I handle auth"
siftd ask -w myproject "error handling"

# Tag and filter
siftd tag 01JGK3 decision:auth
siftd query -l decision:
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

## License

MIT
