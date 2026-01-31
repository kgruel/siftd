# siftd

Searchable index for AI coding conversations.

siftd aggregates conversation logs from Claude Code, Gemini CLI, Codex, and Aider into a SQLite database with full-text and semantic search. Find decisions, rationale, and solutions from your development history.

> Under active development. Breaking changes may occur.

## Install

```bash
pip install siftd              # core
pip install siftd[embed]       # with semantic search
```

## Workflow

### Basic: Ingest and Query

```bash
# Import logs from all supported tools
siftd ingest

# List recent conversations
siftd query

# Filter by workspace or time
siftd query -w myproject
siftd query --since 2024-01-01

# Full-text search
siftd query -s "error handling"

# View a conversation
siftd query 01JGK3
```

### Intermediate: Semantic Search and Tags

Requires `siftd[embed]`. Build the index first:

```bash
siftd ask --index
```

Then search by meaning, not just keywords:

```bash
# Find relevant conversations
siftd ask "how did I handle authentication"

# Refine results
siftd ask -w myproject "auth flow"       # filter by workspace
siftd ask --context 2 "error handling"   # show surrounding exchanges
siftd ask --thread "design decision"     # narrative view

# Tag useful findings
siftd tag 01JGK3 decision:auth
siftd tag --last research:api

# Retrieve by tag
siftd query -l decision:auth
siftd ask -l research: "authentication"
```

### Advanced: Export and Plugins

Export conversations for PR review or context:

```bash
siftd export --last                       # most recent session
siftd export --last 3 -o context.md      # last 3 to file
siftd export -l decision:auth            # tagged conversations
```

Write custom adapters for other tools:

```bash
# Copy template to config dir
siftd copy adapter template

# Edit ~/.config/siftd/adapters/template.py
# Set NAME, DEFAULT_LOCATIONS, parse() logic

# Verify discovery
siftd adapters
siftd ingest -v
```

See [Writing Adapters](docs/writing-adapters.md) for the full SDK.

## Commands

| Command | Description |
|---------|-------------|
| `ingest` | Import conversation logs |
| `query` | List/filter conversations, run SQL queries |
| `ask` | Semantic search (requires [embed]) |
| `tag` | Apply tags to conversations |
| `peek` | View live sessions (bypasses SQLite) |
| `export` | Export conversations for review workflows |
| `doctor` | Run health checks |

## Documentation

- [CLI Reference](docs/reference/cli.md)
- [API Reference](docs/reference/api.md)
- [Schema Reference](docs/reference/schema.md)
- [Writing Adapters](docs/writing-adapters.md)

## License

MIT
