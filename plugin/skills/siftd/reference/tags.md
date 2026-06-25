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
siftd tag list                               # list all tags with counts
```

## Renaming and deleting tags

**Rename** — updates all associations:
```bash
siftd tag rename old-name new-name
```

**Delete** — removes tag and all associations:
```bash
siftd tag delete unused-tag
siftd tag delete unused-tag --force          # skip confirmation
```

## Filtering by tags

Tags are used as filters on `siftd search` and `siftd query`. Three boolean modes:

**OR** (`-l` / `--tag`, repeatable) — match any:
```bash
siftd search -l research:auth -l research:security "tokens"
siftd query -l research:auth -l useful:pattern
```

**AND** (`--all-tags`, repeatable) — require all:
```bash
siftd search --all-tags research:auth --all-tags review "token rotation"
siftd query --all-tags research:auth --all-tags review
```

**NOT** (`--no-tag`, repeatable) — exclude:
```bash
siftd search --no-tag archived "error handling"
siftd query --no-tag archived -l review
```

Boolean modes compose:
```bash
# Tagged research:auth AND NOT archived
siftd query -l research:auth --no-tag archived

# Semantic search over (research:auth OR research:security) AND NOT archived
siftd search -l research:auth -l research:security --no-tag archived "token rotation"
```

## Tag conventions

Prefixed tags create namespaces:

| Prefix | Use | Examples |
|--------|-----|----------|
| `decision:*` | Key architectural/design decisions | `decision:auth`, `decision:schema` |
| `research:*` | Investigation findings worth preserving | `research:auth`, `research:migration` |
| `useful:*` | General bookmarks — patterns, examples | `useful:pattern`, `useful:example` |
| `rationale:*` | Why we chose X over Y | `rationale:jwt`, `rationale:queueing` |
| `genesis:*` | First discussion of a concept | `genesis:indexing`, `genesis:auth-flow` |

These conventions are shared with the project's CLAUDE.md. Consistent across all agents and sessions.

## Tool call tags

Separate from conversation tags. Applied automatically during ingestion (e.g., shell command categorization) or via backfill:

```bash
siftd backfill --shell-tags                  # categorize shell commands
```

Queried via `siftd query --tool-tag` (conversations) or `siftd tag list --on tool_call` (the category summary):
```bash
siftd query --tool-tag shell:test                    # conversations with test commands
siftd tag list --on tool_call                        # shell command category summary
siftd tag list --on tool_call --by-workspace         # breakdown by workspace
siftd tag list --on tool_call --prefix shell:        # filter by tag prefix
```
(The standalone `siftd tools` command was removed in 0.9.0 — use `tag list --on tool_call`.)

Tool tags are not manually applied — they're derived from tool call content.
