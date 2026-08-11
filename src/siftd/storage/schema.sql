-- siftd Schema
-- Minimal Core + Vocabulary Entities + Schemaless Attributes
-- Based on "a simple datastore" principles
-- All primary keys are ULIDs (TEXT, 26 chars, sortable by creation time)

--------------------------------------------------------------------------------
-- VOCABULARY TABLES
-- Referenced by many, auto-discovered or predefined
--------------------------------------------------------------------------------

-- The CLI/tool that wraps model interactions
CREATE TABLE harnesses (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,       -- claude_code, gemini_cli, codex_cli, opencode
    version         TEXT,                       -- 1.0.3, 2.1.0
    display_name    TEXT,                       -- "Claude Code", "Gemini CLI"
    source          TEXT,                       -- anthropic, openai, google, community
    log_format      TEXT                        -- jsonl, json_array, event_stream
);

-- The actual model weights being invoked
CREATE TABLE models (
    id              TEXT PRIMARY KEY,           -- ULID
    raw_name        TEXT NOT NULL UNIQUE,       -- claude-3-opus-20240229, gpt-4o-2024-05-13
    name            TEXT NOT NULL,              -- canonical: claude-3-opus, gpt-4o
    creator         TEXT,                       -- anthropic, openai, google, meta
    family          TEXT,                       -- claude, gpt, gemini
    version         TEXT,                       -- 3, 3.5, 4, 2.0
    variant         TEXT,                       -- opus, sonnet, haiku, flash, pro
    released        TEXT                        -- date string or snapshot identifier
);

CREATE INDEX idx_models_name ON models(name);
CREATE INDEX idx_models_family ON models(family);

-- Who serves the model, takes your money
CREATE TABLE providers (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,       -- anthropic, openai, google, openrouter, local
    display_name    TEXT,                       -- "Anthropic API", "OpenRouter"
    billing_model   TEXT                        -- token, subscription, local, proxy
);

-- Tools available to models
CREATE TABLE tools (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,       -- canonical: file.read, shell.execute, search.grep
    category        TEXT,                       -- file, shell, search, web, edit
    description     TEXT
);

-- Raw tool names map to canonical tools (per harness)
CREATE TABLE tool_aliases (
    id              TEXT PRIMARY KEY,           -- ULID
    raw_name        TEXT NOT NULL,              -- Read, read_file, Bash, run_shell_command
    harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
    tool_id         TEXT NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
    UNIQUE (raw_name, harness_id)
);

CREATE INDEX idx_tool_aliases_tool ON tool_aliases(tool_id);
CREATE INDEX idx_tool_aliases_harness ON tool_aliases(harness_id);

-- Flat pricing lookup for approximate cost computation
CREATE TABLE pricing (
    id              TEXT PRIMARY KEY,           -- ULID
    model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    provider_id     TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    input_per_mtok  REAL,                       -- $ per million input tokens
    output_per_mtok REAL,                       -- $ per million output tokens
    -- Cache rates are OVERRIDE-ONLY: NULL means "derive from input_per_mtok via the
    -- standard Anthropic multiple" (cache read ×0.1, cache creation ×1.25) — exact for
    -- Anthropic; set explicitly for a provider whose cache pricing isn't that multiple.
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
CREATE TABLE workspaces (
    id              TEXT PRIMARY KEY,           -- ULID
    path            TEXT NOT NULL UNIQUE,       -- /Users/kaygee/Code/tbd
    git_remote      TEXT,                       -- git@github.com:user/repo.git
    discovered_at   TEXT NOT NULL               -- ISO timestamp
);

--------------------------------------------------------------------------------
-- CORE TABLES
-- What we ingest from logs
--------------------------------------------------------------------------------

-- A single interaction through one harness
CREATE TABLE conversations (
    id              TEXT PRIMARY KEY,           -- ULID
    external_id     TEXT NOT NULL,              -- harness's identifier
    harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
    workspace_id    TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    branch          TEXT,                       -- worktree branch (if applicable)
    started_at      TEXT NOT NULL,              -- ISO timestamp
    ended_at        TEXT,                       -- ISO timestamp, NULL if unknown/abandoned
    UNIQUE (harness_id, external_id)
);

--------------------------------------------------------------------------------
-- TAG TABLES
-- User-defined categorization
--------------------------------------------------------------------------------

CREATE TABLE tags (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    created_at      TEXT NOT NULL
);

-- Per-owner tag pins (serve-side UI preference state). tags is global (no owner
-- column), so which tags a user keeps in their "pinned" zone lives here, keyed
-- by owner. owner='' is the unscoped/local (no-auth) case. Existing DBs get this
-- table from ensure_tag_pins_table on the next write-open (no version bump);
-- reads guard on its presence (storage.tags.has_tag_pins_table).
CREATE TABLE tag_pins (
    owner      TEXT NOT NULL,
    tag_id     TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    pinned_at  TEXT NOT NULL,
    PRIMARY KEY (owner, tag_id)
);

-- Per-owner workspace pins — same shape and lifecycle as tag_pins (serve-side UI
-- preference state for the Workspaces head). owner='' is the unscoped/local case;
-- existing DBs get this table from ensure_workspace_pins_table on the next
-- write-open (no version bump); reads guard on its presence
-- (storage.queries.has_workspace_pins_table). ON DELETE CASCADE drops a pin when
-- its workspace is merged/removed, so a pin can never orphan a missing target.
CREATE TABLE workspace_pins (
    owner         TEXT NOT NULL,
    workspace_id  TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    pinned_at     TEXT NOT NULL,
    PRIMARY KEY (owner, workspace_id)
);

--------------------------------------------------------------------------------
-- OPERATIONAL TABLES
-- Ingestion tracking
--------------------------------------------------------------------------------

CREATE TABLE ingested_files (
    id              TEXT PRIMARY KEY,           -- ULID
    path            TEXT NOT NULL UNIQUE,
    file_hash       TEXT NOT NULL,
    harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    ingested_at     TEXT NOT NULL,
    error           TEXT,                       -- NULL = success, non-NULL = failure message
    file_mtime      REAL,                       -- st_mtime from os.stat()
    file_size       INTEGER                     -- st_size from os.stat()
);

--------------------------------------------------------------------------------
-- INDEXES
-- Single-table query optimization
--------------------------------------------------------------------------------

CREATE INDEX idx_ingested_files_conversation ON ingested_files(conversation_id);
CREATE INDEX idx_conversations_harness ON conversations(harness_id);
CREATE INDEX idx_conversations_workspace ON conversations(workspace_id);
CREATE INDEX idx_conversations_started ON conversations(started_at);
CREATE INDEX idx_conversations_ended ON conversations(ended_at);

CREATE INDEX idx_workspaces_git_remote ON workspaces(git_remote);

--------------------------------------------------------------------------------
-- CONTENT-ADDRESSABLE STORAGE
-- Deduplicated blob storage for large content (tool_calls.result)
--------------------------------------------------------------------------------

CREATE TABLE content_blobs (
    hash TEXT PRIMARY KEY,              -- SHA256 of content (natural key)
    content TEXT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 1 CHECK (ref_count >= 0),
    created_at TEXT NOT NULL            -- ISO timestamp
);

CREATE INDEX idx_content_blobs_ref_count ON content_blobs(ref_count);

--------------------------------------------------------------------------------
-- SYNC INBOX
-- Tracks staged payloads from push operations pending merge
--------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sync_inbox (
    id                  TEXT PRIMARY KEY,
    received_at         TEXT NOT NULL,
    processed_at        TEXT,
    processing_started_at TEXT,
    status              TEXT NOT NULL DEFAULT 'staged',
    error               TEXT,
    source_host         TEXT,
    size_bytes          INTEGER,
    conversations       INTEGER
);

--------------------------------------------------------------------------------
-- FTS5 FULL-TEXT SEARCH INDEX
-- Indexes text content from event_content
--------------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    text_content,
    event_content_id UNINDEXED,
    event_id         UNINDEXED,
    conversation_id  UNINDEXED,
    tokenize='porter unicode61 remove_diacritics 1'
);

--------------------------------------------------------------------------------
-- POLYMORPHIC EVENT TABLES (schema v4+)
-- Core event storage; replaces the old prompts/responses/tool_calls forks.
--------------------------------------------------------------------------------

CREATE TABLE events (
    id              TEXT PRIMARY KEY,           -- ULID (preserved from prompts/responses/tool_calls)
    kind            TEXT NOT NULL,              -- 'prompt' | 'response' | 'tool_call'
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    parent_id       TEXT REFERENCES events(id) ON DELETE CASCADE,
    external_id     TEXT,                       -- harness's identifier (NULL for synthetic)
    timestamp       TEXT NOT NULL,
    UNIQUE (conversation_id, kind, external_id)
);

CREATE INDEX idx_events_conversation_kind ON events(conversation_id, kind);
CREATE INDEX idx_events_parent ON events(parent_id);

-- Sparse extension: only present for kind='response'
CREATE TABLE event_response (
    event_id        TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    model_id        TEXT REFERENCES models(id) ON DELETE SET NULL,
    provider_id     TEXT REFERENCES providers(id) ON DELETE SET NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER
);

-- Sparse extension: only present for kind='tool_call'
CREATE TABLE event_tool_call (
    event_id        TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
    input           TEXT,                       -- JSON arguments
    result_hash     TEXT REFERENCES content_blobs(hash),
    status          TEXT                        -- success | error | pending
);

-- Indexed for fast ref_count maintenance (M6 heal + delete/update triggers).
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
CREATE TABLE event_content (
    id              TEXT PRIMARY KEY,           -- ULID (preserved from prompt_content/response_content)
    event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    block_index     INTEGER NOT NULL,
    block_type      TEXT NOT NULL,              -- text | thinking | tool_use | tool_result | image | ...
    content         TEXT NOT NULL,
    UNIQUE (event_id, block_index)
);

CREATE INDEX idx_event_content_event ON event_content(event_id);

-- Polymorphic schemaless key-value (replaces *_attributes tables)
CREATE TABLE attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    target_kind     TEXT NOT NULL,              -- 'conversation' | 'prompt' | 'response' | 'tool_call'
    target_id       TEXT NOT NULL,              -- references conversations.id OR events.id
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,                       -- NULL=user, 'provider', 'analyzer', etc.
    UNIQUE (target_kind, target_id, key, scope)
);

CREATE INDEX idx_attributes_target ON attributes(target_kind, target_id);
CREATE INDEX idx_attributes_key ON attributes(key, target_kind, target_id, value);

-- Polymorphic tag assignments (replaces workspace_tags/conversation_tags/tool_call_tags/prompt_tags)
CREATE TABLE tag_assignments (
    id              TEXT PRIMARY KEY,           -- ULID
    target_kind     TEXT NOT NULL,              -- 'conversation' | 'workspace' | 'prompt' | 'response' | 'tool_call' | 'exchange'
    target_id       TEXT NOT NULL,
    tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    applied_at      TEXT NOT NULL,
    UNIQUE (target_kind, target_id, tag_id)
);

CREATE INDEX idx_tag_assignments_target ON tag_assignments(target_kind, target_id);
CREATE INDEX idx_tag_assignments_tag ON tag_assignments(tag_id);

-- Polymorphic cascade cleanup triggers (schema v7+).
-- tag_assignments and attributes have no FK on target_id, so triggers provide the
-- structural guarantee that rows don't orphan when the target is deleted.

CREATE TRIGGER IF NOT EXISTS tr_polymorphic_events_cleanup
AFTER DELETE ON events
BEGIN
    DELETE FROM tag_assignments
    WHERE target_id = OLD.id
      AND target_kind IN ('prompt', 'response', 'tool_call', 'exchange');
    DELETE FROM attributes
    WHERE target_id = OLD.id
      AND target_kind IN ('prompt', 'response', 'tool_call', 'exchange');
END;

CREATE TRIGGER IF NOT EXISTS tr_polymorphic_workspaces_cleanup
AFTER DELETE ON workspaces
BEGIN
    DELETE FROM tag_assignments WHERE target_id = OLD.id AND target_kind = 'workspace';
    DELETE FROM attributes WHERE target_id = OLD.id AND target_kind = 'workspace';
END;

CREATE TRIGGER IF NOT EXISTS tr_polymorphic_conversations_cleanup
AFTER DELETE ON conversations
BEGIN
    DELETE FROM tag_assignments WHERE target_id = OLD.id AND target_kind = 'conversation';
    DELETE FROM attributes WHERE target_id = OLD.id AND target_kind = 'conversation';
END;

-- Content blocks (event_content.id) are the one target kind without an events
-- cleanup path: they cascade-delete via the events FK, but that FK fires no
-- trigger that touches tag_assignments, so a block tag would orphan. This
-- trigger mirrors the others for target_kind = 'block' (schema v12+).
CREATE TRIGGER IF NOT EXISTS tr_polymorphic_event_content_cleanup
AFTER DELETE ON event_content
BEGIN
    DELETE FROM tag_assignments WHERE target_id = OLD.id AND target_kind = 'block';
    DELETE FROM attributes WHERE target_id = OLD.id AND target_kind = 'block';
END;
