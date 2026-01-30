# siftd

AI coding tools generate conversations that contain valuable context—decisions, rationale, debugging sessions, code explanations. But these conversations live in tool-specific formats, scattered across your filesystem, difficult to search, and easy to forget.

siftd aggregates conversation logs from your coding tools into a single searchable index. Find that auth decision you made three weeks ago. See how you solved a similar bug before. Build a knowledge base from your own development history.

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

- [CLI Reference](docs/reference/cli.md)
- [API Reference](docs/reference/api.md)
- [Schema Reference](docs/reference/schema.md)

## License

MIT
