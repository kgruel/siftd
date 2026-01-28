s
 t
  r
   a
    t
     a >_

Every conversation with an LLM agent—Claude Code, Gemini CLI, Aider—is a unit of work. Decisions, rationale, dead ends, breakthroughs. Usually, this context disappears when the session ends.

**strata** ingests these logs, normalizes them into a local database, and makes your entire development history queryable. It turns transient sessions into persistent institutional memory.

## Leverage Your Data

Strata provides multiple interfaces to mine the knowledge buried in your logs:

### 1. Semantic Search (`ask`)
Find concepts by meaning, not just keywords.
```bash
strata ask "how did I handle token refresh"
# Returns relevant snippets from different projects/months
```

### 2. Agent Grounding
Give agents access to your history. Instead of guessing, they learn your preferences:
```bash
# Agent searches your history
strata ask -w rill "testing philosophy"
```
*Result:* The agent sees your preference for integration tests and writes code that fits your style immediately.

### 3. Evolutionary Analysis
Trace the history of a decision.
```bash
strata ask --first "event sourcing"
strata ask --chrono -w myproject "state management"
```
Reconstruct the *why* behind architectural choices by seeing where they started and how they changed.

### 4. Pattern Discovery
Analyze tool usage to find automation opportunities.
```bash
strata query --tool-tag shell:test
strata tools --by-workspace
```
*Insight:* Realize you run the same git commands 50 times a week? Automate it.

### 5. Institutional Memory
Tag conversations to thread meaning across projects.
```bash
strata tag 01JGK3 research:auth
strata ask -l research:auth "token expiry"
```
Build a curated layer of knowledge that persists across sessions and agents.

## Power Features

- **Custom SQL:** Drop `.sql` files in `~/.config/strata/queries/` and run them as `strata query sql <name>`.
- **Extensible Adapters:** Supports Claude Code, Gemini CLI, Codex CLI, and Aider out of the box. Add your own via Python plugins.
- **Local First:** All data lives in SQLite (`~/.local/share/strata`). Search runs locally via `fastembed` or `ollama`. No API calls, no cloud.

## For Developers

Strata is a Python library first. Build your own tools on top of your history.

```python
from strata import hybrid_search, list_conversations

# Build a custom dashboard or analysis script
results = hybrid_search("architectural decisions", workspace="my-project")
```

**Provenance in PRs:**
The discourse is shifting toward "show me your prompts." Strata makes this trivial. Generate a transcript of the session that built the feature and attach it to your PR:

```bash
# Export the session that built the feature
strata peek --json c520f8 > feature-context.json
```
Reviewers can see *why* decisions were made, not just the final code.

## The Philosophy

Strata was built with Strata. The goal is to **collapse the cognitive loop**. By reducing context pollution and making past insights instantly retrievable, you reduce the time to response and increase the quality of output. It transforms your history from a "log" into a "corpus".

## Getting Started

Strata works with existing log files. It doesn't record; it unearths.

**Install**
```bash
uv pip install .           # core functionality
uv pip install .[embed]    # with semantic search (strata ask)
```

Semantic search (`strata ask`) requires the `[embed]` extra. Core features — ingest, query, tags — work without it.

**Ingest & Status**
```bash
strata ingest
strata status
```
*Output:* 847 Conversations, 12k Prompts, 89k Tool calls. Structured and ready.

## Capabilities

| Feature | Command | Description |
| :--- | :--- | :--- |
| **Search** | `strata ask` | Semantic search with embeddings. |
| **Query** | `strata query` | Filter by tags, dates, complexity, or tools. |
| **Inspect** | `strata peek` | View live or past sessions in the terminal. |
| **Health** | `strata doctor` | Verify configuration and index health. |

## Documentation

| Topic | Doc |
|-------|-----|
| **CLI Reference** | [docs/cli.md](docs/cli.md) |
| **Configuration** | [docs/config.md](docs/config.md) |
| **Search Pipeline** | [docs/search.md](docs/search.md) |
| **Tagging System** | [docs/tags.md](docs/tags.md) |
| **Python API** | [docs/api.md](docs/api.md) |
| **Plugin Guide** | [docs/plugin.md](docs/plugin.md) |
