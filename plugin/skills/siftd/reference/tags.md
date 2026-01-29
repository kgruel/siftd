# Tag Management — Full Reference

Tags are user-applied labels on conversations (and other entities). They enable instant retrieval without re-searching.

## Applying tags

**By conversation ID:**
```bash
siftd tag 01HX... research:auth
```

**By recency:**
```bash
siftd tag --last research:auth               # most recent conversation
siftd tag --last 3 review                    # last 3 conversations
```

**Explicit entity type** (conversations are default):
```bash
siftd tag workspace 01HY... production       # tag a workspace
siftd tag tool_call 01HZ... slow             # tag a tool call
```

## Removing tags

**`-r` / `--remove`:**
```bash
siftd tag -r 01HX... research:auth           # remove tag from conversation
siftd tag -r --last research:auth            # remove from most recent
```

## Listing tags

```bash
siftd tags                                   # list all tags with counts
```

## Renaming and deleting tags

**Rename** — updates all associations:
```bash
siftd tags --rename old-name new-name
```

**Delete** — removes tag and all associations:
```bash
siftd tags --delete unused-tag
siftd tags --delete unused-tag --force       # skip confirmation
```

## Filtering by tags

Tags are used as filters on `siftd ask` and `siftd query`. Three boolean modes:

**OR** (`-l` / `--tag`, repeatable) — match any:
```bash
siftd ask -l research:auth -l research:security "tokens"
siftd query -l research:auth -l useful:pattern
```

**AND** (`--all-tags`, repeatable) — require all:
```bash
siftd ask --all-tags research:auth --all-tags review "token rotation"
siftd query --all-tags research:auth --all-tags review
```

**NOT** (`--no-tag`, repeatable) — exclude:
```bash
siftd ask --no-tag archived "error handling"
siftd query --no-tag archived -l review
```

Boolean modes compose:
```bash
# Tagged research:auth AND NOT archived
siftd query -l research:auth --no-tag archived

# Semantic search over (research:auth OR research:security) AND NOT archived
siftd ask -l research:auth -l research:security --no-tag archived "token rotation"
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
siftd backfill --shell-tags                  # categorize shell commands
```

Queried via `siftd query --tool-tag` and `siftd tools`:
```bash
siftd query --tool-tag shell:test            # conversations with test commands
siftd tools                                  # shell command category summary
siftd tools --by-workspace                   # breakdown by workspace
siftd tools --prefix shell:                  # filter by tag prefix
```

Tool tags are not manually applied — they're derived from tool call content.
