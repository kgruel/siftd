---
name: siftd-api
description: "Encode siftd search patterns into reusable Python scripts or SQL queries. Use when a search pattern is worth automating, or when CLI exploration should graduate to programmatic access."
---

# Siftd API — From Exploration to Encoding

Siftd is API-first. The CLI exists for exploration; the API is where useful patterns become durable queries.

## The Workflow

```
CLI commands = exploration scaffolding
     ↓
Discover useful pattern
     ↓
Encode as Python/SQL = durable query
     ↓
Future retrieval uses the encoded form
```

## Phase 1: CLI Exploration

Bootstrap understanding with terminal commands:

```bash
# Broad semantic search in current workspace
siftd ask -w $(basename $PWD) "what we decided about authentication" --thread

# Find when a concept first appeared
siftd ask --first "event sourcing"

# List recent conversations with a tag
siftd query -l research:auth --since 2025-01

# Drill into a specific conversation
siftd query 01KG0EX2X2A1
```

**Goal**: Find useful results, understand the data shape.

## Phase 2: Recognize Encoding Signals

Encode a pattern when you notice:

| Signal | Example |
|--------|---------|
| Repeated query | "I keep searching for auth decisions in this workspace" |
| Multi-step process | "Search -> filter by score -> tag results" |
| Cross-reference need | "Find conversations that mention X and also touched file Y" |
| Scheduled retrieval | "Weekly summary of conversations by workspace" |

**Don't encode** one-off searches. The CLI is fine for exploration.

## Phase 3: Encode as Python

Use `siftd.api` for programmatic access:

```python
#!/usr/bin/env python3
"""Find and tag conversations about a principle."""

from siftd.api import hybrid_search, aggregate_by_conversation
from siftd.storage.tags import apply_tag, get_or_create_tag
from siftd.storage.sqlite import open_database
from siftd.paths import db_path

# The pattern: search -> threshold -> tag
query = "vocabulary as architecture, naming carries structural insight"
tag_name = "principle:vocabulary-as-architecture"
threshold = 0.65

results = hybrid_search(query, limit=100)
convs = aggregate_by_conversation(results, limit=50)

conn = open_database(db_path())
tag_id = get_or_create_tag(conn, tag_name)

for conv in convs:
    if conv.max_score >= threshold:
        apply_tag(conn, 'conversation', conv.conversation_id, tag_id)
        print(f"Tagged {conv.conversation_id[:12]} ({conv.max_score:.3f})")

conn.commit()
conn.close()
```

Save to `~/.config/siftd/scripts/` for reuse.

## Phase 4: Encode as SQL

For declarative queries, create `.sql` files in `~/.config/siftd/queries/`:

```sql
-- ~/.config/siftd/queries/auth-decisions.sql
-- Conversations about authentication in a workspace
SELECT
    c.id,
    c.started_at,
    w.path
FROM conversations c
JOIN workspaces w ON w.id = c.workspace_id
WHERE w.path LIKE '%' || $workspace || '%'
AND c.id IN (
    SELECT conversation_id
    FROM content_fts
    WHERE content_fts MATCH 'authentication OR auth OR login'
)
ORDER BY c.started_at DESC
LIMIT $limit
```

Run with: `siftd query sql auth-decisions workspace=myproject limit=10`

---

## API Reference

### Search

```python
from siftd.api import hybrid_search, aggregate_by_conversation, first_mention

# Semantic search with filters
results = hybrid_search(
    query,
    workspace="projectname",  # substring match
    since="2025-01",          # ISO date
    tags=["research:auth"],   # OR filter
    limit=20
)

# Aggregate to conversation level
convs = aggregate_by_conversation(results, limit=10)

# Find earliest mention
first = first_mention(results, threshold=0.65)
```

### Conversations

```python
from siftd.api import list_conversations, get_conversation

# List with filters
convs = list_conversations(
    workspace="project",
    tag="research:auth",
    search="authentication",  # FTS5
    limit=20
)

# Get full timeline
detail = get_conversation(conv_id)
for exchange in detail.exchanges:
    print(exchange.prompt_text[:100])
```

### Tags

```python
from siftd.storage.sqlite import open_database
from siftd.storage.tags import apply_tag, get_or_create_tag
from siftd.paths import db_path

conn = open_database(db_path())
tag_id = get_or_create_tag(conn, "my-tag")
apply_tag(conn, 'conversation', conv_id, tag_id)
conn.commit()
conn.close()
```

### SQL Queries

```python
from siftd.api import run_query_file, list_query_files

# List available queries
queries = list_query_files()

# Run with variables
result = run_query_file("cost", variables={"limit": "20"})
for row in result.rows:
    print(row)
```

---

## Conventions

1. **Scripts go in** `~/.config/siftd/scripts/` — not in project repos
2. **SQL queries go in** `~/.config/siftd/queries/*.sql`
3. **Use `$variable` syntax** in SQL for parameterization
4. **Tag encoded patterns** — when you encode a search, tag the conversations it finds so future retrieval can use the tag directly
5. **CLI for exploration, API for encoding** — don't write scripts for one-off searches

## Examples

Bundled examples in `scripts/examples/`:
- `bulk-tag.py` — search + threshold + bulk tag
- `find-decisions.py` — parameterized decision lookup
- `weekly-cost.sql` — SQL report with date math
