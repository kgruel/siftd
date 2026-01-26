# strata

Personal analytics for LLM coding sessions. Ingests conversation logs from Claude Code, Gemini CLI, and Codex CLI into SQLite. Query with full-text search, semantic search, or raw SQL.

## Quick Start

```bash
# Install from source
uv pip install .

# Ingest logs from default locations
strata ingest

# See what you have
strata status
```

```
Database: /Users/you/.local/share/strata/strata.db
Size: 42.3 MB

--- Counts ---
  Conversations: 847
  Prompts: 12,493
  Responses: 14,271
  Tool calls: 89,432
  Harnesses: 2
  Workspaces: 31
  Tools: 14
  Models: 8
  Ingested files: 1,203

--- Harnesses ---
  claude_code (anthropic, jsonl)
  gemini_cli (google, json_array)

--- Models ---
  claude-sonnet-4-20250514
  claude-3-5-sonnet-20241022
  gemini-2.0-flash
```

```bash
# List recent conversations
strata query
```

```
01JGK3M2P4Q5  2025-01-15 14:23  tbd-v2        claude-sonnet-4    8p/12r  45.2k tok
01JGK2N1R3S4  2025-01-15 11:07  myproject     claude-sonnet-4    3p/5r   12.1k tok
01JGK1P0T2U3  2025-01-14 22:41  dotfiles      gemini-2.0-flash   2p/3r   4.3k tok
```

## Commands

### Ingest

```bash
strata ingest                    # Scan default locations
strata ingest -p ~/custom/logs   # Add custom path
strata ingest -v                 # Verbose (show skipped files)
```

Default locations:
- Claude Code: `~/.claude/projects/`
- Gemini CLI: `~/.gemini/`
- Codex CLI: `~/.codex/`

### Query conversations

```bash
# List recent conversations
strata query

# Filter by workspace
strata query -w myproject

# Filter by model
strata query -m sonnet

# Filter by date
strata query --since 2025-01-01

# Full-text search (FTS5)
strata query -s "error handling"

# Filter by tool usage
strata query -t shell.execute

# Filter by tag
strata query -l important

# Combine filters
strata query -w myproject -m opus --since 2025-01-01

# Show more results
strata query -n 50

# Show all columns
strata query -v

# Output as JSON
strata query --json
```

### Conversation detail

```bash
# View a conversation timeline (prefix match on ID)
strata query 01JGK3
```

```
Conversation: 01JGK3M2P4Q5R6S7T8U9V0W1X2Y3
Workspace: tbd-v2
Started: 2025-01-15 14:23
Model: claude-sonnet-4
Tokens: 45.2k (input: 38.1k / output: 7.1k)

[prompt] 14:23
  Add semantic search to the CLI

[response] 14:23 (2.1k in / 0.8k out)
  I'll help you add semantic search. Let me first understand...
  → file.read (success)
  → file.read (success)
  → search.grep (success)

[prompt] 14:25
  Use fastembed for local embeddings

[response] 14:25 (8.4k in / 1.2k out)
  I'll integrate fastembed for local embedding generation...
  → file.edit ×3 (success)
  → shell.execute (success)
```

### Semantic search

Semantic search uses a two-stage hybrid approach:

1. **FTS5 recall**: Fast keyword search narrows candidates to ~80 conversations
2. **Embeddings rerank**: Dense vectors rerank chunks by semantic similarity

This gives you keyword precision with semantic understanding.

```bash
# Build the embeddings index (first time)
strata ask --index
```

```
Embedding 3,847 new chunks...
  64/3847
  128/3847
  ...
Done. Index has 3,847 chunks (fastembed, dim=384).
```

```bash
# Search
strata ask "how did I implement caching"
```

```
Results for: how did I implement caching

  01JGK3M2P4Q5  0.847  [RESPONSE]  2025-01-15  tbd-v2
    I'll add a simple LRU cache using functools. The cache key will be...

  01JGH2N1R3S4  0.812  [PROMPT  ]  2025-01-12  myproject
    Can you add caching to the API calls? They're too slow right now.
```

```bash
# Search with filters
strata ask -w myproject "authentication flow"
strata ask -m opus "error handling"
strata ask --since 2025-01-01 "database schema"

# Output modes
strata ask -v "caching"              # Full chunk text
strata ask --full "caching"          # Complete exchange from DB
strata ask --context 3 "caching"     # ±3 exchanges around match
strata ask --thread "caching"        # Narrative view: expanded top + shortlist
strata ask --conversations "caching" # Rank conversations, not chunks
strata ask --first "when did I add"  # Earliest match above threshold

# Filter by role
strata ask --role user "caching"     # Only user prompts
strata ask --role assistant "caching" # Only assistant responses

# Tune retrieval
strata ask --recall 200 "error"      # Widen FTS5 candidate pool
strata ask --embeddings-only "error" # Skip FTS5, pure embeddings
strata ask --threshold 0.7 "error"   # Only results with score >= 0.7

# File references
strata ask --refs "authelia"         # Show file annotations + content dump
```

#### Embedding backends

strata supports multiple embedding backends:

- **fastembed** (default): Local, no API key needed. Uses `BAAI/bge-small-en-v1.5`.
- **ollama**: Local, requires ollama running. Uses `nomic-embed-text`.

```bash
strata ask --index --backend fastembed
strata ask --index --backend ollama
```

#### Benchmarking retrieval

The `bench/` directory contains tools for evaluating retrieval quality:

```bash
# Build test corpora with different chunking strategies
python bench/build.py

# Run benchmark against gold-standard queries
python bench/run.py

# View results
python bench/view.py
```

Use this to compare chunking strategies, embedding models, and hybrid vs pure-embeddings approaches.

### Tags

```bash
# Apply a tag to a conversation
strata tag conversation 01JGK3M2P4Q5... important

# Apply a tag to a workspace
strata tag workspace 01JGH... work

# Apply a tag to a tool call
strata tag tool_call 01JGK... slow

# List all tags with counts
strata tags
```

```
  important - (3 conversations)
  work - (12 workspaces)
  slow - (7 tool_calls)
```

### SQL queries

User-defined queries live in `~/.config/strata/queries/*.sql`. Variables use `$name` syntax.

```bash
# List available queries
strata query sql
```

```
cost  (vars: limit)
```

```bash
# Run a query
strata query sql cost --var limit=20
```

```
workspace              model            provider   input_tokens  output_tokens  approx_cost_usd
---------------------  ---------------  ---------  ------------  -------------  ---------------
/Users/me/Code/tbd-v2  claude-sonnet-4  anthropic  2847293       412847         $0.4821
/Users/me/Code/other   claude-sonnet-4  anthropic  1293847       98472          $0.1923
```

#### Example queries

**Daily token usage** (`~/.config/strata/queries/daily.sql`):
```sql
SELECT
    date(c.started_at) AS day,
    COUNT(DISTINCT c.id) AS conversations,
    SUM(r.input_tokens) AS input_tok,
    SUM(r.output_tokens) AS output_tok
FROM conversations c
JOIN responses r ON r.conversation_id = c.id
WHERE c.started_at >= date('now', '-30 days')
GROUP BY day
ORDER BY day DESC
```

**Tool usage by workspace** (`~/.config/strata/queries/tools-by-workspace.sql`):
```sql
SELECT
    w.path AS workspace,
    t.name AS tool,
    COUNT(*) AS calls,
    SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) AS errors
FROM tool_calls tc
JOIN tools t ON t.id = tc.tool_id
JOIN conversations c ON c.id = tc.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
WHERE w.path LIKE '%$workspace%'
GROUP BY w.path, t.name
ORDER BY calls DESC
```

**Model comparison** (`~/.config/strata/queries/model-comparison.sql`):
```sql
SELECT
    m.name AS model,
    COUNT(DISTINCT c.id) AS conversations,
    AVG(r.input_tokens) AS avg_input,
    AVG(r.output_tokens) AS avg_output,
    AVG(r.output_tokens * 1.0 / NULLIF(r.input_tokens, 0)) AS output_ratio
FROM responses r
JOIN models m ON m.id = r.model_id
JOIN conversations c ON c.id = r.conversation_id
GROUP BY m.name
ORDER BY conversations DESC
```

**Long conversations** (`~/.config/strata/queries/long-conversations.sql`):
```sql
SELECT
    c.id,
    w.path AS workspace,
    COUNT(p.id) AS prompts,
    SUM(r.input_tokens + r.output_tokens) AS total_tokens
FROM conversations c
JOIN workspaces w ON w.id = c.workspace_id
JOIN prompts p ON p.conversation_id = c.id
JOIN responses r ON r.conversation_id = c.id
GROUP BY c.id
HAVING total_tokens > 100000
ORDER BY total_tokens DESC
LIMIT $limit
```

## Library API

For programmatic access:

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
)
```

### List and filter conversations

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
    print(f"  → {len(ex.tool_calls)} tool calls")
    print(f"  → {ex.response_text[:80]}")
```

### Semantic search

```python
# Hybrid search (FTS5 recall → embeddings rerank)
results = hybrid_search(
    "authentication flow",
    workspace="myproject",
    limit=10,
)

for r in results:
    print(f"{r.score:.3f}  {r.workspace_path}  {r.text[:100]}")

# Aggregate by conversation (rank conversations, not chunks)
conv_scores = aggregate_by_conversation(results)
for cs in conv_scores:
    print(f"{cs.conversation_id[:12]}  max={cs.max_score:.3f}  chunks={cs.chunk_count}")

# Find earliest mention above threshold
earliest = first_mention(results, threshold=0.7)
if earliest:
    print(f"First mentioned: {earliest.started_at}")
```

### Build embeddings index

```python
# Incremental update
stats = build_index()
print(f"Indexed {stats['chunks_added']} new chunks")

# Full rebuild
stats = build_index(rebuild=True)
```

### Database stats

```python
stats = get_stats()
print(f"Conversations: {stats.counts.conversations}")
print(f"Prompts: {stats.counts.prompts}")
print(f"Responses: {stats.counts.responses}")
print(f"Tool calls: {stats.counts.tool_calls}")
print(f"Models: {stats.models}")
```

### SQL query files

```python
# List available queries
for qf in list_query_files():
    print(f"{qf.name}  vars: {qf.variables}")

# Run a query with variables
result = run_query_file("cost", variables={"limit": "20"})
for row in result.rows:
    print(row)
```

### Tags

```python
# List all tags
for tag in list_tags():
    print(f"{tag['name']}: {tag['conversation_count']} conversations")

# Apply a tag
tag_id = get_or_create_tag(conn, "important")
apply_tag(conn, "conversation", "01JGK3...", tag_id)
```

## Data Model

```
conversations
  └── prompts (user messages)
        └── responses (assistant messages)
              └── tool_calls
```

### Core tables

| Table | Purpose |
|-------|---------|
| `conversations` | Session-level: workspace, timestamps |
| `prompts` | User input with content blocks |
| `responses` | Assistant output with token usage |
| `tool_calls` | Tool invocations with input/result/status |
| `prompt_content` | Ordered content blocks in prompts |
| `response_content` | Ordered content blocks in responses |

### Vocabulary tables

| Table | Purpose |
|-------|---------|
| `harnesses` | CLI tools (claude_code, gemini_cli, codex_cli) |
| `models` | Parsed model names with family/version/variant |
| `providers` | API providers (anthropic, google, openrouter) |
| `tools` | Canonical tool names (file.read, shell.execute) |
| `tool_aliases` | Raw → canonical tool name mapping per harness |
| `workspaces` | Project directories |
| `pricing` | Token pricing for cost approximation |

### Extension tables

| Table | Purpose |
|-------|---------|
| `tags` | User-defined labels |
| `conversation_tags` | Tag → conversation associations |
| `workspace_tags` | Tag → workspace associations |
| `tool_call_tags` | Tag → tool_call associations |
| `*_attributes` | Key-value metadata (per entity type) |

### Search tables

| Table | Purpose |
|-------|---------|
| `content_fts` | FTS5 full-text index over content |
| `chunks` (embeddings DB) | Dense vector index for semantic search |

## Paths

```bash
strata path
```

```
Data directory:   /Users/you/.local/share/strata
Config directory: /Users/you/.config/strata
Cache directory:  /Users/you/.cache/strata
Database:         /Users/you/.local/share/strata/strata.db
```

- **Data**: `~/.local/share/strata/` — database, embeddings
- **Config**: `~/.config/strata/` — queries, adapters
- **Queries**: `~/.config/strata/queries/*.sql`
- **Adapters**: `~/.config/strata/adapters/*.py`

## Adapters

Built-in adapters: Claude Code, Gemini CLI, Codex CLI.

### Drop-in adapters

Add custom adapters to `~/.config/strata/adapters/`:

```
~/.config/strata/adapters/
└── my_tool.py
```

Each adapter module must define:

```python
NAME = "my_tool"
DEFAULT_LOCATIONS = ["~/.my_tool/logs"]
DEDUP_STRATEGY = "file"           # "file" or "session"
HARNESS_SOURCE = "openai"         # provider name
HARNESS_LOG_FORMAT = "jsonl"      # or "json_array"

TOOL_ALIASES = {
    "RawToolName": "canonical.name",
}

def discover() -> Iterable[Source]:
    """Yield Source objects for all log files."""

def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the source."""

def parse(source: Source) -> Iterable[Conversation]:
    """Parse source into Conversation domain objects."""
```

A drop-in adapter with the same `NAME` as a built-in will override it.

Adapters are validated at load time — missing required attributes will raise an error.

See `src/strata/adapters/claude_code.py` for a complete example.
