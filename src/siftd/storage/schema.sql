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
    started_at      TEXT NOT NULL,              -- ISO timestamp
    ended_at        TEXT,                       -- ISO timestamp, NULL if unknown/abandoned
    UNIQUE (harness_id, external_id)
);

-- User's input
CREATE TABLE prompts (
    id              TEXT PRIMARY KEY,           -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    external_id     TEXT,                       -- harness's message ID
    timestamp       TEXT NOT NULL,
    UNIQUE (conversation_id, external_id)
);

-- Model's output
CREATE TABLE responses (
    id              TEXT PRIMARY KEY,           -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    prompt_id       TEXT REFERENCES prompts(id) ON DELETE CASCADE,
    model_id        TEXT REFERENCES models(id) ON DELETE SET NULL,
    provider_id     TEXT REFERENCES providers(id) ON DELETE SET NULL,
    external_id     TEXT,                       -- harness's message ID
    timestamp       TEXT NOT NULL,
    input_tokens    INTEGER,                    -- universal
    output_tokens   INTEGER,                    -- universal
    UNIQUE (conversation_id, external_id)
);

-- Tool invocations during response generation
CREATE TABLE tool_calls (
    id              TEXT PRIMARY KEY,           -- ULID
    response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
    external_id     TEXT,                       -- model-assigned tool_call_id
    input           TEXT,                       -- JSON arguments
    result          TEXT,                       -- JSON result (legacy, use result_hash)
    result_hash     TEXT REFERENCES content_blobs(hash),  -- deduplicated result
    status          TEXT,                       -- success, error, pending
    timestamp       TEXT
);

--------------------------------------------------------------------------------
-- CONTENT TABLES
-- Ordered blocks belonging to prompts/responses
--------------------------------------------------------------------------------

-- Content blocks in prompts (usually just text, but could be attachments)
CREATE TABLE prompt_content (
    id              TEXT PRIMARY KEY,           -- ULID
    prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    block_index     INTEGER NOT NULL,
    block_type      TEXT NOT NULL,              -- text, image, file
    content         TEXT NOT NULL,              -- the actual content or reference
    UNIQUE (prompt_id, block_index)
);

-- Content blocks in responses (text, thinking, tool references)
CREATE TABLE response_content (
    id              TEXT PRIMARY KEY,           -- ULID
    response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
    block_index     INTEGER NOT NULL,
    block_type      TEXT NOT NULL,              -- text, thinking, tool_use, tool_result
    content         TEXT NOT NULL,
    UNIQUE (response_id, block_index)
);

--------------------------------------------------------------------------------
-- ATTRIBUTE TABLES
-- Schemaless key-value for everything else
--------------------------------------------------------------------------------

CREATE TABLE conversation_attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,                       -- NULL=user, 'provider', 'analyzer', etc.
    UNIQUE (conversation_id, key, scope)
);

CREATE TABLE prompt_attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,
    UNIQUE (prompt_id, key, scope)
);

CREATE TABLE response_attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,
    UNIQUE (response_id, key, scope)
);

CREATE TABLE tool_call_attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,
    UNIQUE (tool_call_id, key, scope)
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

CREATE TABLE workspace_tags (
    id              TEXT PRIMARY KEY,           -- ULID
    workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    applied_at      TEXT NOT NULL,
    UNIQUE (workspace_id, tag_id)
);

CREATE TABLE conversation_tags (
    id              TEXT PRIMARY KEY,           -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    applied_at      TEXT NOT NULL,
    UNIQUE (conversation_id, tag_id)
);

CREATE TABLE tool_call_tags (
    id              TEXT PRIMARY KEY,           -- ULID
    tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
    tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    applied_at      TEXT NOT NULL,
    UNIQUE (tool_call_id, tag_id)
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
    error           TEXT                        -- NULL = success, non-NULL = failure message
);

--------------------------------------------------------------------------------
-- INDEXES
-- Single-table query optimization
--------------------------------------------------------------------------------

CREATE INDEX idx_conversations_harness ON conversations(harness_id);
CREATE INDEX idx_conversations_workspace ON conversations(workspace_id);
CREATE INDEX idx_conversations_started ON conversations(started_at);
CREATE INDEX idx_conversations_ended ON conversations(ended_at);

CREATE INDEX idx_prompts_conversation ON prompts(conversation_id);
CREATE INDEX idx_prompts_timestamp ON prompts(timestamp);

CREATE INDEX idx_responses_conversation ON responses(conversation_id);
CREATE INDEX idx_responses_prompt ON responses(prompt_id);
CREATE INDEX idx_responses_model ON responses(model_id);
CREATE INDEX idx_responses_timestamp ON responses(timestamp);

CREATE INDEX idx_tool_calls_response ON tool_calls(response_id);
CREATE INDEX idx_tool_calls_conversation ON tool_calls(conversation_id);
CREATE INDEX idx_tool_calls_tool ON tool_calls(tool_id);
CREATE INDEX idx_tool_calls_status ON tool_calls(status);

CREATE INDEX idx_prompt_content_prompt ON prompt_content(prompt_id);
CREATE INDEX idx_response_content_response ON response_content(response_id);

--------------------------------------------------------------------------------
-- CONTENT-ADDRESSABLE STORAGE
-- Deduplicated blob storage for large content (tool_calls.result)
--------------------------------------------------------------------------------

CREATE TABLE content_blobs (
    hash TEXT PRIMARY KEY,              -- SHA256 of content (natural key)
    content TEXT NOT NULL,
    ref_count INTEGER DEFAULT 1,
    created_at TEXT NOT NULL            -- ISO timestamp
);

CREATE INDEX idx_content_blobs_ref_count ON content_blobs(ref_count);

-- Trigger to decrement ref_count and garbage collect when tool_calls are deleted
CREATE TRIGGER tr_tool_calls_delete_release_blob
AFTER DELETE ON tool_calls
FOR EACH ROW
WHEN OLD.result_hash IS NOT NULL
BEGIN
    UPDATE content_blobs SET ref_count = ref_count - 1 WHERE hash = OLD.result_hash;
    DELETE FROM content_blobs WHERE hash = OLD.result_hash AND ref_count = 0;
END;

--------------------------------------------------------------------------------
-- FTS5 FULL-TEXT SEARCH INDEX
-- Indexes text content from prompt_content and response_content
--------------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    text_content,
    content_id UNINDEXED,
    side UNINDEXED,
    conversation_id UNINDEXED
);
