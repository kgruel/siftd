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
| `output_per_mtok` | REAL |  | $ per million output tokens -- Cache rates are OVERRIDE-ONLY: NULL means "derive from input_per_mtok via the -- standard Anthropic multiple" (cache read ×0.1, cache creation ×1.25 |

### workspaces

Anthropic; set explicitly for a provider whose cache pricing isn't that multiple.
    cache_read_per_mtok     REAL,               -- $ per million cache-read input tokens
    cache_creation_per_mtok REAL,               -- $ per million cache-creation input tokens
    -- Provenance (v11): the pricing table is a projection of the version-controlled
    -- reference (siftd/data/pricing.toml + user override). source/as_of carry the
    -- citation and verification date forward so a stored price is auditable.
    source          TEXT,                       -- citation URL or provenance note
    as_of           TEXT,                       -- date the value was verified (YYYY-MM-DD)
    UNIQUE (model_id, provider_id)
);

-- Physical paths where work happens

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
| `branch` | TEXT |  | worktree branch (if applicable) |
| `started_at` | TEXT | NOT NULL | ISO timestamp |
| `ended_at` | TEXT |  | ISO timestamp, NULL if unknown/abandoned |

## TAG TABLES

### tags

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `name` | TEXT | NOT NULL UNIQUE |  |
| `description` | TEXT |  |  |
| `created_at` | TEXT | NOT NULL |  |

### tag_pins

Per-owner tag pins (serve-side UI preference state). tags is global (no owner
-- column), so which tags a user keeps in their "pinned" zone lives here, keyed
-- by owner. owner='' is the unscoped/local (no-auth) case. Existing DBs get this
-- table from ensure_tag_pins_table on the next write-open (no version bump);
-- reads guard on its presence (storage.tags.has_tag_pins_table).

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `owner` | TEXT | NOT NULL |  |
| `tag_id` | TEXT | NOT NULL REFERENCES tags(id) ON DELETE CASCADE |  |
| `pinned_at` | TEXT | NOT NULL |  |

### workspace_pins

Per-owner workspace pins — same shape and lifecycle as tag_pins (serve-side UI
-- preference state for the Workspaces head). owner='' is the unscoped/local case;
-- existing DBs get this table from ensure_workspace_pins_table on the next
-- write-open (no version bump); reads guard on its presence
-- (storage.queries.has_workspace_pins_table). ON DELETE CASCADE drops a pin when
-- its workspace is merged/removed, so a pin can never orphan a missing target.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `owner` | TEXT | NOT NULL |  |
| `workspace_id` | TEXT | NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE |  |
| `pinned_at` | TEXT | NOT NULL |  |

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
| `file_mtime` | REAL |  | st_mtime from os.stat() |
| `file_size` | INTEGER |  | st_size from os.stat() |

## CONTENT-ADDRESSABLE STORAGE

### content_blobs

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `hash` | TEXT | PRIMARY KEY | SHA256 of content (natural key) |
| `content` | TEXT | NOT NULL |  |
| `ref_count` | INTEGER | NOT NULL DEFAULT 1 CHECK (ref_count >= 0) |  |
| `created_at` | TEXT | NOT NULL | ISO timestamp |

## SYNC INBOX

### sync_inbox

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY |  |
| `received_at` | TEXT | NOT NULL |  |
| `processed_at` | TEXT |  |  |
| `processing_started_at` | TEXT |  |  |
| `status` | TEXT | NOT NULL DEFAULT 'staged' |  |
| `error` | TEXT |  |  |
| `source_host` | TEXT |  |  |
| `size_bytes` | INTEGER |  |  |
| `conversations` | INTEGER |  |  |

## FTS5 FULL-TEXT SEARCH INDEX

### content_fts

_Virtual table using fts5._

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `text_content` | TEXT |  |  |
| `event_content_id` | TEXT | UNINDEXED |  |
| `event_id` | TEXT | UNINDEXED |  |
| `conversation_id` | TEXT | UNINDEXED |  |
| `tokenize` | TEXT |  |  |

## POLYMORPHIC EVENT TABLES (schema v4+)

### events

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID (preserved from prompts/responses/tool_calls) |
| `kind` | TEXT | NOT NULL | 'prompt' \| 'response' \| 'tool_call' |
| `conversation_id` | TEXT | NOT NULL REFERENCES conversations(id) ON DELETE CASCADE |  |
| `parent_id` | TEXT | REFERENCES events(id) ON DELETE CASCADE |  |
| `external_id` | TEXT |  | harness's identifier (NULL for synthetic) |
| `timestamp` | TEXT | NOT NULL |  |

### event_response

Sparse extension: only present for kind='response'

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `event_id` | TEXT | PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE |  |
| `model_id` | TEXT | REFERENCES models(id) ON DELETE SET NULL |  |
| `provider_id` | TEXT | REFERENCES providers(id) ON DELETE SET NULL |  |
| `input_tokens` | INTEGER |  |  |
| `output_tokens` | INTEGER |  |  |

### event_tool_call

Sparse extension: only present for kind='tool_call'

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `event_id` | TEXT | PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE |  |
| `tool_id` | TEXT | REFERENCES tools(id) ON DELETE SET NULL |  |
| `input` | TEXT |  | JSON arguments |
| `result_hash` | TEXT | REFERENCES content_blobs(hash) |  |
| `status` | TEXT |  | success \| error \| pending |

### event_content

Indexed for fast ref_count maintenance (M6 heal + delete/update triggers).
-- Without this, GROUP BY result_hash and WHERE result_hash = ? are full scans.
CREATE INDEX idx_event_tool_call_result_hash
    ON event_tool_call(result_hash) WHERE result_hash IS NOT NULL;

-- Trigger to decrement ref_count and garbage collect when event_tool_call rows are deleted
CREATE TRIGGER tr_event_tool_call_delete_release_blob
AFTER DELETE ON event_tool_call
FOR EACH ROW
WHEN OLD.result_hash IS NOT NULL
BEGIN
    UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0) WHERE hash = OLD.result_hash;
    DELETE FROM content_blobs WHERE hash = OLD.result_hash AND ref_count <= 0;
END;

-- Trigger to adjust ref_count when result_hash changes on event_tool_call
CREATE TRIGGER tr_event_tool_call_update_release_blob
AFTER UPDATE OF result_hash ON event_tool_call
FOR EACH ROW
WHEN OLD.result_hash IS NOT NEW.result_hash
BEGIN
    -- Decrement old blob (if any)
    UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0)
        WHERE OLD.result_hash IS NOT NULL AND hash = OLD.result_hash;
    DELETE FROM content_blobs
        WHERE OLD.result_hash IS NOT NULL AND hash = OLD.result_hash AND ref_count <= 0;
    -- Increment new blob (if any)
    UPDATE content_blobs SET ref_count = ref_count + 1
        WHERE NEW.result_hash IS NOT NULL AND hash = NEW.result_hash;
END;

-- Unified content blocks (replaces prompt_content + response_content)

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID (preserved from prompt_content/response_content) |
| `event_id` | TEXT | NOT NULL REFERENCES events(id) ON DELETE CASCADE |  |
| `block_index` | INTEGER | NOT NULL |  |
| `block_type` | TEXT | NOT NULL | text \| thinking \| tool_use \| tool_result \| image \| ... |
| `content` | TEXT | NOT NULL |  |

### attributes

Polymorphic schemaless key-value (replaces *_attributes tables)

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `target_kind` | TEXT | NOT NULL | 'conversation' \| 'prompt' \| 'response' \| 'tool_call' |
| `target_id` | TEXT | NOT NULL | references conversations.id OR events.id |
| `key` | TEXT | NOT NULL |  |
| `value` | TEXT | NOT NULL |  |
| `scope` | TEXT |  | NULL=user, 'provider', 'analyzer', etc. |

### tag_assignments

Polymorphic tag assignments (replaces workspace_tags/conversation_tags/tool_call_tags/prompt_tags)

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | TEXT | PRIMARY KEY | ULID |
| `target_kind` | TEXT | NOT NULL | 'conversation' \| 'workspace' \| 'prompt' \| 'response' \| 'tool_call' \| 'exchange' |
| `target_id` | TEXT | NOT NULL |  |
| `tag_id` | TEXT | NOT NULL REFERENCES tags(id) ON DELETE CASCADE |  |
| `applied_at` | TEXT | NOT NULL |  |
