# Tags

Tags are lightweight metadata you attach to conversations. They're how you mark something as worth remembering — a decision made, a pattern discovered, a topic to revisit.

## Why tags exist

Search finds things. Tags remember why they mattered.

You search for "authentication" and find 15 conversations. Three of them contain the actual decision about how to handle tokens. Without tags, you'd have to re-evaluate all 15 next time. With tags, you mark the important ones:

```bash
siftd tag 01JGK3 decision:auth
siftd tag 01JFXN decision:auth
siftd tag 01JGK1 decision:auth
```

Next time, skip the search:

```bash
siftd query -l decision:auth
```

Tags are the synthesis layer — they encode your judgment about what matters.

## How tags work

Tags are just strings. No schema, no predefined categories. Create whatever makes sense:

```bash
siftd tag 01JGK3 important
siftd tag 01JGK3 auth-decision
siftd tag 01JGK3 decision:authentication
siftd tag 01JGK3 2025-q1-review
```

Tags are created on first use. If you typo a tag name, you've created a new tag. Use `siftd tags` to see what exists.

Tags can be applied to:
- **Conversations** — most common, marks an entire session
- **Workspaces** — marks a project directory
- **Tool calls** — marks a specific action (used by auto-tagging)

## Naming conventions

Freeform tags get messy fast. Prefixed namespaces help:

| Prefix | Purpose | Examples |
|--------|---------|----------|
| `decision:` | Architectural choices | `decision:auth`, `decision:caching` |
| `research:` | Exploration, learning | `research:oauth`, `research:testing` |
| `pattern:` | Reusable approaches | `pattern:error-handling`, `pattern:fixtures` |
| `review:` | Needs follow-up | `review:security`, `review:perf` |
| `project:` | Cross-cutting project tags | `project:launch`, `project:migration` |

The colon is just convention — siftd treats `decision:auth` as a single string. But the prefix lets you query by namespace:

```bash
siftd query -l decision:       # all decision:* tags
siftd query -l research:       # all research:* tags
siftd search -l pattern: "error"  # search within pattern-tagged conversations
```

## Applying tags

Tag a conversation by ID (prefix match works):

```bash
siftd tag 01JGK3 decision:auth
```

Tag the most recent conversation:

```bash
siftd tag -n 1 decision:auth
```

Tag multiple recent conversations:

```bash
siftd tag -n 3 review:needed
```

Apply multiple tags at once:

```bash
siftd tag 01JGK3 decision:auth important reviewed
```

## Removing tags

```bash
siftd tag --remove 01JGK3 decision:auth
siftd tag -r -n 1 reviewed    # remove from most recent
```

## Querying by tag

Filter conversations:

```bash
siftd query -l decision:auth              # exact tag
siftd query -l decision:                  # prefix match (all decision:*)
siftd query -l research: -l pattern:      # OR — either namespace
siftd query --all-tags important --all-tags reviewed   # AND — must have both
siftd query -l research: --no-tag archived             # combine with exclusion
```

Search within tagged conversations:

```bash
siftd search -l research: "authentication"   # semantic search, filtered to research:*
siftd search -l decision:auth "token expiry" # search within specific tag
```

## Managing tags

List all tags with counts:

```bash
siftd tags
```

```
decision:auth       3 conversations
decision:caching    2 conversations
pattern:testing     5 conversations
research:oauth      1 conversation
shell:test          847 tool calls
shell:vcs           312 tool calls
```

Drill into a specific tag:

```bash
siftd tags decision:auth
```

Rename a tag (updates all associations):

```bash
siftd tags --rename auth-decision decision:auth
```

Delete a tag:

```bash
siftd tags --delete old-tag          # refuses if tag has associations
siftd tags --delete old-tag --force  # deletes tag and all associations
```

## Auto-applied tags

siftd automatically applies some tags during ingest:

### Shell command categories

Every `shell.execute` tool call gets categorized:

| Tag | Commands |
|-----|----------|
| `shell:test` | pytest, jest, cargo test, go test |
| `shell:vcs` | git, gh |
| `shell:build` | make, npm run build, cargo build |
| `shell:lint` | ruff, eslint, prettier |
| `shell:deps` | pip, npm, cargo add |

Query by shell category:

```bash
siftd query --tool-tag shell:test     # conversations that ran tests
siftd query --tool-tag shell:vcs      # conversations that used git
siftd tools --by-workspace            # see patterns per project
```

### Derivative conversations

Conversations that run `siftd search` or `siftd query` get tagged `siftd:derivative`. These are excluded from search by default — otherwise your searches would find previous search results.

```bash
siftd search "topic"                        # excludes derivative
siftd search --include-derivative "topic"   # includes them
```

## Tags as institutional memory

Tags accumulate value over time. Early on, you're just marking things. After a few months:

```bash
siftd query -l decision:           # all architectural decisions, ever
siftd search -l research: "caching" # everything you've explored about caching
siftd export -l pattern:testing    # all your testing patterns, ready to share
```

Tags let you build a curated layer on top of raw conversations. An agent working on a new project can search your tagged decisions. A new team member can review your research tags. The synthesis persists.

## Tags vs search

| Use tags when... | Use search when... |
|------------------|-------------------|
| You know it's important | You're exploring |
| You'll need it again | You might need it once |
| You want instant retrieval | You can afford to re-find |
| You're building a collection | You're answering a question |

The workflow: search to find, tag to remember, query tags to retrieve.

## Entity types

While conversations are the most common target, tags work on other entities:

```bash
siftd tag workspace 01HY... project:core      # tag a workspace
siftd tag tool_call 01HZ... slow              # tag a specific tool call
```

Workspace tags apply to the directory. All conversations in that workspace inherit the tag for filtering purposes.

Tool call tags are mainly used by auto-tagging (shell categories), but you can add your own to mark specific actions.
