# siftd query — Full Reference

List, filter, and drill into conversations. Also runs user-defined SQL queries.

## Listing conversations

**Default** — 10 most recent conversations:
```bash
siftd query
```

**`-v` / `--verbose`** — full table with all columns:
```bash
siftd query -v
```

**`-n` / `--count N`** — number of conversations (0 = all):
```bash
siftd query -n 20
siftd query -n 0                               # everything
```

**`--oldest`** — sort direction:
```bash
siftd query --oldest                           # chronological (default: newest first)
```

**`--stats`** — summary totals after list:
```bash
siftd query --stats
siftd query -w myproject --stats               # stats for filtered set
```

**`--json`** — JSON output:
```bash
siftd query --json
siftd query -w myproject --json | jq '.[].started_at'
```

## Filters

All filters compose with each other.

**`-w` / `--workspace SUBSTR`** — workspace path substring:
```bash
siftd query -w myproject
```

**`-m` / `--model NAME`** — model name:
```bash
siftd query -m claude-3-opus
```

**`--since DATE` / `--before DATE`** — date range:
```bash
siftd query --since 2025-01
siftd query --since 2025-01 --before 2025-06
```

**`-s` / `--search QUERY`** — FTS5 full-text search:
```bash
siftd query -s "error handling"
siftd query -s "error handling" -w myproject    # FTS5 + workspace
```
Uses SQLite FTS5 syntax. Different from `siftd ask` which uses semantic/embedding search.

**`-l` / `--tag NAME`** — filter by conversation tag (OR, repeatable):
```bash
siftd query -l research:auth
siftd query -l research:auth -l research:security
```

**`--all-tags NAME`** — require all tags (AND, repeatable):
```bash
siftd query --all-tags research:auth --all-tags review
```

**`--no-tag NAME`** — exclude tag (NOT, repeatable):
```bash
siftd query --no-tag archived
```

**`-t` / `--tool NAME`** — filter by canonical tool name:
```bash
siftd query -t shell.execute                   # conversations with shell commands
```

**`--tool-tag NAME`** — filter by tool call tag (supports prefix matching):
```bash
siftd query --tool-tag shell:test              # conversations with test commands
siftd query --tool-tag shell:vcs               # conversations with git commands
siftd query --tool-tag shell:                  # any shell-categorized command (prefix match)
siftd query -w myproject --tool-tag shell:test # combine with workspace filter
```

## Conversation drill-down

Pass a conversation ID to see its full timeline:
```bash
siftd query 01HX...
```

Shows prompts, responses, and tool calls in chronological order with tags displayed.

## SQL queries

Run user-defined `.sql` files from `~/.config/siftd/queries/`:

**List available queries:**
```bash
siftd query sql
```

**Run a query:**
```bash
siftd query sql cost
```

**Run with variables:**
```bash
siftd query sql cost --var ws=myproject
```

### Variable syntax

Two syntaxes are supported:

| Syntax | Behavior | Use for |
|--------|----------|---------|
| `$var` or `${var}` | Text substitution | Table names, column names, structural SQL |
| `:var` | Parameterized (safe) | String values, dates, numbers |

**Template variables (`$var`)** are replaced before execution. Use for structural elements:
```sql
SELECT * FROM $table WHERE $column IS NOT NULL
```

**Parameter variables (`:var`)** are passed to SQLite as named parameters with automatic quoting. Use for values:
```sql
SELECT * FROM conversations
WHERE workspace LIKE '%' || :ws || '%'
  AND started_at > :since
```

**Mixed example:**
```sql
-- $table for structure, :filter for value
SELECT * FROM $table WHERE name = :filter
```
```bash
siftd query sql myquery --var table=conversations --var filter="project's name"
```

The `:var` syntax handles quotes and special characters safely — no SQL injection risk for values.

## Composition examples

```bash
# Recent conversations in a project with test commands
siftd query -w myproject --tool-tag shell:test --since 2025-01

# All tagged research conversations, oldest first
siftd query -l research:auth --oldest -n 0

# FTS5 search within a workspace, with stats
siftd query -s "migration" -w myproject --stats

# Conversations using git commands in the last month
siftd query --tool-tag shell:vcs --since 2025-01

# Everything tagged for review, excluding archived
siftd query -l review --no-tag archived

# JSON output for scripting
siftd query -w myproject --json | jq 'length'
```
