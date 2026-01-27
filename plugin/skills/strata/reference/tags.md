# Tag Management — Full Reference

Tags are user-applied labels on conversations (and other entities). They enable instant retrieval without re-searching.

## Applying tags

**By conversation ID:**
```bash
strata tag 01HX... research:auth
```

**By recency:**
```bash
strata tag --last research:auth               # most recent conversation
strata tag --last 3 review                    # last 3 conversations
```

**Explicit entity type** (conversations are default):
```bash
strata tag workspace 01HY... production       # tag a workspace
strata tag tool_call 01HZ... slow             # tag a tool call
```

## Removing tags

**`-r` / `--remove`:**
```bash
strata tag -r 01HX... research:auth           # remove tag from conversation
strata tag -r --last research:auth            # remove from most recent
```

## Listing tags

```bash
strata tags                                   # list all tags with counts
```

## Renaming and deleting tags

**Rename** — updates all associations:
```bash
strata tags --rename old-name new-name
```

**Delete** — removes tag and all associations:
```bash
strata tags --delete unused-tag
strata tags --delete unused-tag --force       # skip confirmation
```

## Filtering by tags

Tags are used as filters on `strata ask` and `strata query`. Three boolean modes:

**OR** (`-l` / `--tag`, repeatable) — match any:
```bash
strata ask -l research:auth -l research:security "tokens"
strata query -l research:auth -l useful:pattern
```

**AND** (`--all-tags`, repeatable) — require all:
```bash
strata ask --all-tags research:auth --all-tags review "token rotation"
strata query --all-tags research:auth --all-tags review
```

**NOT** (`--no-tag`, repeatable) — exclude:
```bash
strata ask --no-tag archived "error handling"
strata query --no-tag archived -l review
```

Boolean modes compose:
```bash
# Tagged research:auth AND NOT archived
strata query -l research:auth --no-tag archived

# Semantic search over (research:auth OR research:security) AND NOT archived
strata ask -l research:auth -l research:security --no-tag archived "token rotation"
```

## Tag conventions

Prefixed tags create namespaces:

| Prefix | Use | Examples |
|--------|-----|----------|
| `research:*` | Investigation findings worth preserving | `research:auth`, `research:migration` |
| `useful:*` | General bookmarks — patterns, examples | `useful:pattern`, `useful:example` |

These conventions are shared with the project's CLAUDE.md. Consistent across all agents and sessions.

## Tool call tags

Separate from conversation tags. Applied automatically during ingestion (e.g., shell command categorization) or via backfill:

```bash
strata backfill --shell-tags                  # categorize shell commands
```

Queried via `strata query --tool-tag` and `strata tools`:
```bash
strata query --tool-tag shell:test            # conversations with test commands
strata tools                                  # shell command category summary
strata tools --by-workspace                   # breakdown by workspace
strata tools --prefix shell:                  # filter by tag prefix
```

Tool tags are not manually applied — they're derived from tool call content.
