# Tags

Tags are user-defined labels applied to conversations, workspaces, or tool calls. They provide a lightweight annotation layer on top of raw conversation data — no schema changes, no LLM calls.

## Applying tags

```bash
# Tag a conversation (default entity type)
strata tag 01JGK3 important

# Tag recent conversations
strata tag --last important          # most recent
strata tag --last 3 review           # last 3

# Explicit entity types
strata tag workspace 01JGH... proj
strata tag tool_call 01JGK... slow
```

## Removing tags

```bash
strata tag --remove 01JGK3 important
```

## Browsing tags

```bash
# List all tags with counts
strata tags

# Drill into a specific tag
strata tags research:auth

# Filter by prefix
strata tags --prefix research:
```

## Managing tags

```bash
strata tags --rename old-name new-name
strata tags --delete unused-tag --force
```

`--force` is required for delete to prevent accidents.

## Boolean filtering

Tags compose with `query` and `ask` through three operators:

```bash
# OR — conversations with any of these tags (repeatable -l)
strata query -l research:auth -l research:security

# AND — conversations with all of these tags (repeatable --all-tags)
strata query --all-tags research:auth --all-tags review

# NOT — exclude conversations with this tag (repeatable --no-tag)
strata query --no-tag archived
```

All three operators are repeatable and compose with each other and with all other filters:

```bash
strata ask -l research:auth --no-tag archived -w myproject "token expiry"
```

## Prefix matching

A trailing colon acts as a namespace wildcard:

```bash
strata query -l research:          # matches research:auth, research:perf, etc.
strata ask -l useful: "patterns"   # matches useful:pattern, useful:example, etc.
```

This works on both conversation tags and tool tags (`--tool-tag`).

## Conventions

| Prefix | Use |
|--------|-----|
| `research:*` | Investigation findings worth preserving |
| `useful:*` | General bookmarks (useful:pattern, useful:example) |
| `shell:*` | Auto-applied tool call tags (shell:vcs, shell:test, etc.) |

The `shell:*` namespace is auto-populated at ingest time for tool calls. 13 categories cover 91% of 25k+ observed shell commands.

## Shell command tags

Tool calls are auto-tagged with `shell:*` categories during ingestion:

```bash
# Filter conversations by tool call tags
strata query --tool-tag shell:test
strata query --tool-tag shell:vcs

# Summary of tool usage
strata tools
strata tools --by-workspace
```

To backfill shell tags for data ingested before auto-tagging was added:

```bash
strata backfill --shell-tags
```

## Workflow

The intended workflow is:

1. **Search** — `strata ask "your question"`
2. **Find** — review results, drill into conversations with `strata query <id>`
3. **Tag** — mark useful results: `strata tag <id> research:topic`
4. **Retrieve** — come back later: `strata query -l research:topic` or `strata ask -l research:topic "refined query"`

Tags are how you build institutional memory from ephemeral conversations.
