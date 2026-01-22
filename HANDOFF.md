# tbd-v2 — Handoff

Data model redesign from first principles. Ready for ingestion work.

## Where We Are

**Schema complete, tested with real data.**

### What We Built

1. **Data model from "a simple datastore" principles**
   - Vocabulary entities referenced by many, merged by natural key
   - Core entities with ULID primary keys for merge safety
   - Attributes for schemaless extension
   - Labels for user categorization

2. **Vocabulary entities**
   - `harnesses` — CLI/tool wrapping the interaction (Claude Code, Gemini CLI, opencode)
   - `models` — actual weights, decomposed (family/version/variant)
   - `providers` — who serves the model, billing model
   - `tools` — canonical tool names, with per-harness aliases
   - `workspaces` — physical paths where work happens

3. **Core entities (semantic naming)**
   - `conversations` — single interaction through one harness (was "session")
   - `prompts` — user input (was "message role=user")
   - `responses` — model output, has model_id/provider_id (was "message role=assistant")
   - `tool_calls` — invocations during response generation

4. **Content & attributes**
   - `prompt_content` / `response_content` — ordered blocks
   - `*_attributes` — schemaless key-value per entity

5. **ULIDs everywhere**
   - All primary keys are ULIDs (26 chars, sortable by creation time)
   - Merge-safe across machines and teammates
   - Inline generation, no dependencies

6. **Claude Code adapter**
   - Parses JSONL logs
   - Creates vocabulary on discovery (harness, tools, workspaces)
   - Maps Prompt → Response → ToolCall flow
   - Handles content normalization (string vs array)

### Test Results

```
5 conversations ingested
2 workspaces discovered
2 tools created (Read, Edit)
7 tool calls (6 success, 1 error)
19 responses, 5 prompts
```

## Key Concepts

| Term | Meaning |
|------|---------|
| **Conversation** | Single interaction through one harness (what logs capture) |
| **Session** | User work period spanning multiple conversations (future) |
| **Harness** | The CLI/tool (Claude Code, Gemini CLI, opencode, Cline) |
| **Model** | The weights being invoked (claude-3-opus, gpt-4o) |
| **Provider** | Who serves the model (Anthropic API, OpenRouter, local) |
| **Workspace** | Physical path where work happens |
| **Project** | Conceptual grouping (via labels on workspaces) |

## Files

```
tbd-v2/
├── src/
│   ├── storage/
│   │   ├── schema.sql      # Full schema, 19 tables, all ULID PKs
│   │   └── sqlite.py       # Storage adapter with ULID generation
│   └── adapters/
│       └── claude_code.py  # Claude Code JSONL parser
├── batch_ingest.py         # Test script for multiple files
├── ingest_test.py          # Single file test
└── test.db                 # Sample database with real data
```

## Schema Summary

```
Vocabulary (6 tables)
├── harnesses, models, providers
├── tools, tool_aliases (harness-scoped)
└── workspaces

Core (4 tables)
├── conversations
├── prompts, responses
└── tool_calls

Content (2 tables)
├── prompt_content
└── response_content

Attributes (4 tables)
├── conversation_attributes
├── prompt_attributes
├── response_attributes
└── tool_call_attributes

Labels (3 tables)
├── labels
├── workspace_labels
└── conversation_labels

Operational (1 table)
└── ingested_files
```

## What's Next

**Focus: Ingestion strategies and interfaces**

### Questions to Address

1. **Discovery patterns**
   - How do we find logs across harnesses?
   - Provider registry like tbd-v1?
   - Watch mode for continuous ingestion?

2. **Adapter interface**
   - What's the contract for a harness adapter?
   - Domain objects vs direct DB writes?
   - Streaming vs batch?

3. **Idempotency**
   - ingested_files table exists but not wired up
   - Hash-based deduplication?
   - Re-ingestion strategy (skip vs update)?

4. **Multi-provider**
   - Gemini CLI adapter
   - Codex CLI adapter
   - Common patterns to extract

5. **Model/Provider extraction**
   - Claude Code logs don't include model per message
   - Infer from harness config?
   - Default provider per harness?

6. **CLI interface**
   - `tbd ingest --all` like v1?
   - Progress reporting?
   - Dry-run mode?

### Decisions Made

- ULIDs everywhere (merge safety > query convenience)
- Tool aliases are harness-scoped
- Prompts and Responses are separate tables (model lives on Response)
- Vocabulary merges by natural key, keeps one ULID
- JSON columns for structured blobs (tool input/result), attributes for queryable metadata

### Deferred

- sqlite-ulid extension (not worth the dependency)
- Model extraction from logs (need to investigate what's available)
- Token type normalization (have the pattern, not implemented)
- OTLP ingestion (future layer)

## Commands

```bash
cd ~/Code/tbd-v2

# Test single file
python3 ingest_test.py

# Test batch
python3 batch_ingest.py

# Query the database
sqlite3 test.db "SELECT * FROM conversations"
sqlite3 test.db "SELECT name FROM tools"
```

## Origin

Redesigned from tbd-v1 after discussion about:
- "A simple datastore" principles (core + relationships + attributes)
- Vocabulary entities (Model, Tool, Harness, Provider)
- Semantic naming (Conversation vs Session, Prompt/Response vs Message)
- Developer context as the anchor (not production observability)

See `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md` for the pattern origin.

---

*Started: 2026-01-21*
*Schema complete: 2026-01-21*
*Ready for: Ingestion strategies and interfaces*
