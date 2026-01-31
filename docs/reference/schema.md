# Schema Reference

_Auto-generated from `src/siftd/storage/schema.sql`._

All primary keys are ULIDs (26-char TEXT, sortable by creation time).

## VOCABULARY TABLES

### harnesses

The CLI/tool that wraps model interactions

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `name` | TEXT | NOT NULL UNIQUE | claude_code, gemini_cli, codex_cli, opencode |
| `version` | TEXT |  | 1.0.3, 2.1.0 |
| `display_name` | TEXT |  | "Claude Code", "Gemini CLI" |
| `source` | TEXT |  | anthropic, openai, google, community |
| `log_format` | TEXT |  | jsonl, json_array, event_stream |

### models

The actual model weights being invoked

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `raw_name` | TEXT | NOT NULL UNIQUE | claude-3-opus-20240229, gpt-4o-2024-05-13 |
| `name` | TEXT | NOT NULL | canonical: claude-3-opus, gpt-4o |
| `creator` | TEXT |  | anthropic, openai, google, meta |
| `family` | TEXT |  | claude, gpt, gemini |
| `version` | TEXT |  | 3, 3.5, 4, 2.0 |
| `variant` | TEXT |  | opus, sonnet, haiku, flash, pro |
| `released` | TEXT |  | date string or snapshot identifier |

### providers

Who serves the model, takes your money

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `name` | TEXT | NOT NULL UNIQUE | anthropic, openai, google, openrouter, local |
| `display_name` | TEXT |  | "Anthropic API", "OpenRouter" |
| `billing_model` | TEXT |  | token, subscription, local, proxy |

### tools

Tools available to models

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `name` | TEXT | NOT NULL UNIQUE | canonical: file.read, shell.execute, search.grep |
| `category` | TEXT |  | file, shell, search, web, edit |
| `description` | TEXT |  |  |

### tool_aliases

Raw tool names map to canonical tools (per harness)

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `raw_name` | TEXT | NOT NULL | Read, read_file, Bash, run_shell_command |
| `harness_id` | TEXT | NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE |  |
| `tool_id` | TEXT | NOT NULL REFERENCES tools(id) ON DELETE CASCADE |  |

### pricing

Flat pricing lookup for approximate cost computation

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `model_id` | TEXT | NOT NULL REFERENCES models(id) ON DELETE CASCADE |  |
| `provider_id` | TEXT | NOT NULL REFERENCES providers(id) ON DELETE CASCADE |  |
| `input_per_mtok` | REAL |  | $ per million input tokens |
| `output_per_mtok` | REAL |  | $ per million output tokens |

### workspaces

Physical paths where work happens

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `path` | TEXT | NOT NULL UNIQUE | /Users/kaygee/Code/tbd |
| `git_remote` | TEXT |  | git@github.com:user/repo.git |
| `discovered_at` | TEXT | NOT NULL | ISO timestamp |

## CORE TABLES

### conversations

A single interaction through one harness

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `external_id` | TEXT | NOT NULL | harness's identifier |
| `harness_id` | TEXT | NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE |  |
| `workspace_id` | TEXT | REFERENCES workspaces(id) ON DELETE SET NULL |  |
| `started_at` | TEXT | NOT NULL | ISO timestamp |
| `ended_at` | TEXT |  | ISO timestamp, NULL if unknown/abandoned |

### prompts

User's input

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `conversation_id` | TEXT | NOT NULL REFERENCES conversations(id) ON DELETE CASCADE |  |
| `external_id` | TEXT |  | harness's message ID |
| `timestamp` | TEXT | NOT NULL |  |

### responses

Model's output

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `conversation_id` | TEXT | NOT NULL REFERENCES conversations(id) ON DELETE CASCADE |  |
| `prompt_id` | TEXT | REFERENCES prompts(id) ON DELETE CASCADE |  |
| `model_id` | TEXT | REFERENCES models(id) ON DELETE SET NULL |  |
| `provider_id` | TEXT | REFERENCES providers(id) ON DELETE SET NULL |  |
| `external_id` | TEXT |  | harness's message ID |
| `timestamp` | TEXT | NOT NULL |  |
| `input_tokens` | INTEGER |  | universal |
| `output_tokens` | INTEGER |  | universal |

### tool_calls

Tool invocations during response generation

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `response_id` | TEXT | NOT NULL REFERENCES responses(id) ON DELETE CASCADE |  |
| `conversation_id` | TEXT | NOT NULL REFERENCES conversations(id) ON DELETE CASCADE |  |
| `tool_id` | TEXT | REFERENCES tools(id) ON DELETE SET NULL |  |
| `external_id` | TEXT |  | model-assigned tool_call_id |
| `input` | TEXT |  | JSON arguments |
| `result` | TEXT |  | JSON result (legacy, use result_hash) |
| `result_hash` | TEXT | REFERENCES content_blobs(hash) | deduplicated result |
| `status` | TEXT |  | success, error, pending |
| `timestamp` | TEXT |  |  |

## CONTENT TABLES

### prompt_content

Content blocks in prompts (usually just text, but could be attachments)

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `prompt_id` | TEXT | NOT NULL REFERENCES prompts(id) ON DELETE CASCADE |  |
| `block_index` | INTEGER | NOT NULL |  |
| `block_type` | TEXT | NOT NULL | text, image, file |
| `content` | TEXT | NOT NULL | the actual content or reference |

### response_content

Content blocks in responses (text, thinking, tool references)

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `response_id` | TEXT | NOT NULL REFERENCES responses(id) ON DELETE CASCADE |  |
| `block_index` | INTEGER | NOT NULL |  |
| `block_type` | TEXT | NOT NULL | text, thinking, tool_use, tool_result |
| `content` | TEXT | NOT NULL |  |

## ATTRIBUTE TABLES

### conversation_attributes

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `conversation_id` | TEXT | NOT NULL REFERENCES conversations(id) ON DELETE CASCADE |  |
| `key` | TEXT | NOT NULL |  |
| `value` | TEXT | NOT NULL |  |
| `scope` | TEXT |  | NULL=user, 'provider', 'analyzer', etc. |

### prompt_attributes

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `prompt_id` | TEXT | NOT NULL REFERENCES prompts(id) ON DELETE CASCADE |  |
| `key` | TEXT | NOT NULL |  |
| `value` | TEXT | NOT NULL |  |
| `scope` | TEXT |  |  |

### response_attributes

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `response_id` | TEXT | NOT NULL REFERENCES responses(id) ON DELETE CASCADE |  |
| `key` | TEXT | NOT NULL |  |
| `value` | TEXT | NOT NULL |  |
| `scope` | TEXT |  |  |

### tool_call_attributes

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `tool_call_id` | TEXT | NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE |  |
| `key` | TEXT | NOT NULL |  |
| `value` | TEXT | NOT NULL |  |
| `scope` | TEXT |  |  |

## TAG TABLES

### tags

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `name` | TEXT | NOT NULL UNIQUE |  |
| `description` | TEXT |  |  |
| `created_at` | TEXT | NOT NULL |  |

### workspace_tags

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `workspace_id` | TEXT | NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE |  |
| `tag_id` | TEXT | NOT NULL REFERENCES tags(id) ON DELETE CASCADE |  |
| `applied_at` | TEXT | NOT NULL |  |

### conversation_tags

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `conversation_id` | TEXT | NOT NULL REFERENCES conversations(id) ON DELETE CASCADE |  |
| `tag_id` | TEXT | NOT NULL REFERENCES tags(id) ON DELETE CASCADE |  |
| `applied_at` | TEXT | NOT NULL |  |

### tool_call_tags

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `tool_call_id` | TEXT | NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE |  |
| `tag_id` | TEXT | NOT NULL REFERENCES tags(id) ON DELETE CASCADE |  |
| `applied_at` | TEXT | NOT NULL |  |

## OPERATIONAL TABLES

### ingested_files

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `path` | TEXT | NOT NULL UNIQUE |  |
| `file_hash` | TEXT | NOT NULL |  |
| `harness_id` | TEXT | NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE |  |
| `conversation_id` | TEXT | REFERENCES conversations(id) ON DELETE CASCADE |  |
| `ingested_at` | TEXT | NOT NULL |  |
| `error` | TEXT |  | NULL = success, non-NULL = failure message |

## CONTENT-ADDRESSABLE STORAGE

### content_blobs

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `hash` | TEXT | PRIMARY KEY | SHA256 of content (natural key) |
| `content` | TEXT | NOT NULL |  |
| `ref_count` | INTEGER | DEFAULT 1 |  |
| `created_at` | TEXT | NOT NULL | ISO timestamp |

## FTS5 FULL-TEXT SEARCH INDEX

### content_fts

_Virtual table using fts5._

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `text_content` | TEXT |  |  |
| `content_id` | TEXT | UNINDEXED |  |
| `side` | TEXT | UNINDEXED |  |
| `conversation_id` | TEXT | UNINDEXED |  |
