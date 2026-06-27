# Documentation

## Concepts

How siftd works under the hood.

- [Data Model](concepts/data-model.md) — conversations, prompts, responses, tool calls
- [Adapters](concepts/adapters.md) — parsing logs from different tools
- [Search](concepts/search.md) — FTS5 keyword search, embeddings, hybrid mode
- [Tags](concepts/tags.md) — metadata, naming conventions, auto-tagging
- [Storage](concepts/storage.md) — SQLite, deduplication, backup/restore
- [Sync](concepts/sync.md) — push/pull between machines, delta tracking, SSH and local-path transport
- [Serve](concepts/serve.md) — HTTP server for teams: auth, push attribution, remote query
- [Web UI](concepts/web-ui.md) — the local browser front-end: reading, dashboards, URL-addressable views

## Guides

- [Installation](guides/install.md) — installing siftd and the Claude Code plugin
- [Using siftd with agents](guides/agents.md) — record + recall: hooks, the `/siftd` skill, tagging conventions
- [Writing Adapters](guides/writing-adapters.md) — build a custom log parser

## Reference

- [CLI](reference/cli.md) — all commands and flags
- [API](reference/api.md) — library usage
- [Schema](reference/schema.md) — database tables and columns
- [Config](reference/config.md) — config.toml keys, types, and defaults
