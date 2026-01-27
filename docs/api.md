# Library API

strata exposes a Python API for programmatic access. The CLI is one consumer of this API.

```python
from strata import (
    # Conversations
    list_conversations,         # List with filters
    get_conversation,           # Full detail by ID (prefix match)
    ConversationSummary,
    ConversationDetail,
    Exchange,

    # Search
    hybrid_search,              # FTS5 + embeddings semantic search
    aggregate_by_conversation,  # Group chunks by conversation
    first_mention,              # Earliest match above threshold
    build_index,                # Build/rebuild embeddings index
    SearchResult,
    ConversationScore,

    # Stats
    get_stats,                  # Database statistics
    DatabaseStats,

    # Query files
    list_query_files,           # List user-defined SQL queries
    run_query_file,             # Execute SQL query with variables
    QueryFile,
    QueryResult,

    # Tags
    list_tags,
    apply_tag,
    get_or_create_tag,

    # Adapters
    list_adapters,              # Discover available adapters
    copy_adapter,               # Copy built-in to config dir
    copy_query,                 # Copy built-in query to config dir

    # Health
    list_checks,                # Available health checks
    run_checks,                 # Run checks
    apply_fix,                  # Apply a fix

    # Peek
    list_active_sessions,       # Active session discovery
    read_session_detail,        # Session detail view
    tail_session,               # Raw JSONL tail
    find_session_file,          # Locate session file
)
```

## Conversations

### List and filter

```python
# Recent conversations
convs = list_conversations(limit=10)

# Filter by workspace, model, date
convs = list_conversations(
    workspace="myproject",
    model="sonnet",
    since="2025-01-01",
    limit=50,
)

# Full-text search
convs = list_conversations(search="error handling")

# Filter by tool usage
convs = list_conversations(tool="shell.execute")

# Boolean tag filters
convs = list_conversations(
    tags=["research:auth", "research:security"],   # OR
    all_tags=["review"],                            # AND
    exclude_tags=["archived"],                      # NOT
)

# Tool call tags
convs = list_conversations(tool_tag="shell:test")

for c in convs:
    print(f"{c.id[:12]}  {c.started_at[:10]}  {c.workspace_path}  {c.total_tokens} tok")
```

### Get conversation detail

```python
# Prefix match on ID
conv = get_conversation("01JGK3")

print(f"Workspace: {conv.workspace_path}")
print(f"Model: {conv.model}")
print(f"Tokens: {conv.total_input_tokens + conv.total_output_tokens}")

for ex in conv.exchanges:
    print(f"[{ex.timestamp}] {ex.prompt_text[:80]}")
    print(f"  -> {len(ex.tool_calls)} tool calls")
    print(f"  -> {ex.response_text[:80]}")
```

## Search

### Hybrid search

```python
results = hybrid_search(
    "authentication flow",
    workspace="myproject",
    limit=10,
)

for r in results:
    print(f"{r.score:.3f}  {r.workspace_path}  {r.text[:100]}")
```

### Aggregate by conversation

```python
conv_scores = aggregate_by_conversation(results)
for cs in conv_scores:
    print(f"{cs.conversation_id[:12]}  max={cs.max_score:.3f}  chunks={cs.chunk_count}")
```

### First mention

```python
earliest = first_mention(results, threshold=0.7)
if earliest:
    print(f"First mentioned: {earliest.started_at}")
```

### Build index

```python
# Incremental update
stats = build_index()
print(f"Indexed {stats['chunks_added']} new chunks")

# Full rebuild
stats = build_index(rebuild=True)
```

## Stats

```python
stats = get_stats()
print(f"Conversations: {stats.counts.conversations}")
print(f"Prompts: {stats.counts.prompts}")
print(f"Responses: {stats.counts.responses}")
print(f"Tool calls: {stats.counts.tool_calls}")
print(f"Models: {stats.models}")
```

## SQL query files

```python
# List available queries
for qf in list_query_files():
    print(f"{qf.name}  vars: {qf.variables}")

# Run a query with variables
result = run_query_file("cost", variables={"limit": "20"})
for row in result.rows:
    print(row)
```

## Tags

```python
# List all tags
for tag in list_tags():
    print(f"{tag['name']}: {tag['conversation_count']} conversations")

# Apply a tag
tag_id = get_or_create_tag(conn, "important")
apply_tag(conn, "conversation", "01JGK3...", tag_id)
```

## Tool tags

```python
# Summary by category
summary = get_tool_tag_summary()

# By workspace
by_ws = get_tool_tags_by_workspace()
```

## Health checks

```python
checks = list_checks()
results = run_checks()
for r in results:
    if r.findings:
        print(f"{r.check}: {len(r.findings)} issues")

# Apply a fix
apply_fix("ingest-pending")
```

## Peek (live sessions)

```python
sessions = list_active_sessions()
for s in sessions:
    print(f"{s.session_id}  {s.workspace}  {s.mtime}")

detail = read_session_detail(session_id)
for ex in detail.exchanges:
    print(f"[{ex.role}] {ex.text[:80]}")
```
