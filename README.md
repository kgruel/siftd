# strata

Every conversation you have with an LLM coding tool — Claude Code, Gemini CLI, Codex, Aider — produces a log file. Those files contain decisions you made, approaches you tried, problems you solved. When the session ends, that knowledge disappears into a directory you'll never open.

strata makes it searchable.

## Install

```bash
uv pip install .
```

## Getting started

strata reads the log files your tools already write. Point it at them and it builds a local SQLite database:

```bash
strata ingest
```

It automatically discovers logs from Claude Code, Gemini CLI, Codex CLI, and Aider in their default locations. Run it again anytime — it only processes new or changed files.

See what you have:

```bash
strata status
```

```
Conversations: 847
Prompts: 12,493
Responses: 14,271
Tool calls: 89,432
Workspaces: 31
Models: 8
```

Those 847 conversations are now queryable.

## Browsing conversations

`strata query` is a composable conversation browser. On its own, it lists recent conversations:

```bash
strata query
```

```
01JGK3M2P4Q5  2025-01-15 14:23  myproject     claude-sonnet-4    8p/12r  45.2k tok
01JGK2N1R3S4  2025-01-15 11:07  webapp        claude-sonnet-4    3p/5r   12.1k tok
01JGK1P0T2U3  2025-01-14 22:41  dotfiles      gemini-2.0-flash   2p/3r   4.3k tok
```

Filters narrow the view. You can combine any of them:

```bash
strata query -w myproject                              # by workspace
strata query -m opus --since 2025-01-01                # by model and date
strata query -s "error handling"                       # full-text search
strata query -t shell.execute                          # by tool usage
```

Drill into a conversation by passing its ID (prefix match works):

```bash
strata query 01JGK3
```

```
Workspace: myproject
Started: 2025-01-15 14:23
Model: claude-sonnet-4
Tokens: 45.2k (input: 38.1k / output: 7.1k)

[prompt] 14:23
  Add semantic search to the CLI

[response] 14:23 (2.1k in / 0.8k out)
  I'll help you add semantic search. Let me first understand...
  → file.read ×2, search.grep ×1

[prompt] 14:25
  Use fastembed for local embeddings

[response] 14:25 (8.4k in / 1.2k out)
  I'll integrate fastembed for local embedding generation...
  → file.edit ×3, shell.execute ×1
```

This is useful when you know roughly what you're looking for. But conversations pile up fast — after a few hundred, browsing and keyword search aren't enough. You need semantic search.

## Searching across conversations

`strata ask` finds conversations by meaning, not just keywords. First, build the embeddings index:

```bash
strata ask --index
```

This chunks your conversations into prompt+response pairs, embeds them locally (no API calls), and stores the vectors. Run it again after ingesting new data.

Now search:

```bash
strata ask "how did I handle token refresh"
```

```
01JGK3M2P4Q5  0.847  [RESPONSE]  2025-01-15  myproject
  The token refresh uses a sliding window — store the refresh token in...

01JFXN2R1K4M  0.812  [PROMPT  ]  2024-10-03  auth-service
  Can you add automatic token refresh? The current flow requires...
```

That second result is from three months ago, in a different project. strata found it because the meaning matched, even though the words were different.

### Focusing the search

The workspace filter (`-w`) is the single most impactful way to narrow results:

```bash
strata ask -w myproject "authentication flow"
```

You can also filter by model, date range, or role:

```bash
strata ask --since 2025-01-01 "database schema"
strata ask --role user "what should we do about"     # just your prompts
strata ask --role assistant "recommended approach"    # just LLM responses
```

### Reading results in context

Default output shows snippets. When you find something relevant, you'll want more context:

```bash
strata ask --thread "why we chose JWT"
```

`--thread` is the best mode for research — it expands the top conversations with full exchange timelines and lists the rest as a compact shortlist. Other modes:

```bash
strata ask --context 3 "token refresh"     # ±3 exchanges around the match
strata ask --full "schema migration"       # complete prompt+response exchange
strata ask --chrono "state management"     # sorted by time, not relevance
strata ask --first "event sourcing"        # earliest mention above threshold
```

## Preserving what you find

Search gets you to the right conversation. Tags let you keep it:

```bash
strata tag 01JGK3 research:auth
```

Now you can retrieve tagged conversations directly:

```bash
strata query -l research:auth
strata ask -l research:auth "token expiry"
```

Tags support prefix matching — `research:` matches `research:auth`, `research:perf`, and anything else in that namespace. Boolean filtering composes naturally:

```bash
strata query -l research:auth -l research:security    # either tag (OR)
strata query --all-tags research:auth --all-tags review # both tags (AND)
strata query --no-tag archived                          # exclude a tag
```

The workflow is: **search → find → tag → retrieve later**. Tags are how you build a curated layer on top of raw conversation data.

## What else

**Live sessions** — inspect active sessions without waiting for ingestion:

```bash
strata peek                          # list active sessions
strata peek c520 --last 10           # detail view
```

**Health checks** — diagnose issues:

```bash
strata doctor
```

**Raw SQL** — write your own queries:

```bash
strata query sql cost --var limit=20
```

**Library API** — use strata from Python:

```python
from strata import list_conversations, hybrid_search, get_stats
```

**Custom adapters** — add support for other tools:

```
~/.config/strata/adapters/my_tool.py
```

**Claude Code plugin** — agent DX with hooks and a bundled skill:

```bash
claude plugin marketplace add kaygee/strata
claude plugin install strata@strata
```

## Going deeper

| Topic | Doc |
|-------|-----|
| All CLI commands and flags | [docs/cli.md](docs/cli.md) |
| Configuration | [docs/config.md](docs/config.md) |
| Search pipeline, diversity tuning, benchmarking | [docs/search.md](docs/search.md) |
| Tag system and conventions | [docs/tags.md](docs/tags.md) |
| Library API reference | [docs/api.md](docs/api.md) |
| Data model and schema | [docs/data-model.md](docs/data-model.md) |
| Writing custom adapters | [docs/adapters.md](docs/adapters.md) |
| SQL queries and examples | [docs/queries.md](docs/queries.md) |
| Claude Code plugin | [docs/plugin.md](docs/plugin.md) |
