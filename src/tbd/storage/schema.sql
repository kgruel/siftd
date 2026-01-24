-- tbd-v2 Schema
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
    harness_id      TEXT NOT NULL REFERENCES harnesses(id),
    tool_id         TEXT NOT NULL REFERENCES tools(id),
    UNIQUE (raw_name, harness_id)
);

CREATE INDEX idx_tool_aliases_tool ON tool_aliases(tool_id);
CREATE INDEX idx_tool_aliases_harness ON tool_aliases(harness_id);

-- Flat pricing lookup for approximate cost computation
CREATE TABLE pricing (
    id              TEXT PRIMARY KEY,           -- ULID
    model_id        TEXT NOT NULL REFERENCES models(id),
    provider_id     TEXT NOT NULL REFERENCES providers(id),
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
    harness_id      TEXT NOT NULL REFERENCES harnesses(id),
    workspace_id    TEXT REFERENCES workspaces(id),
    started_at      TEXT NOT NULL,              -- ISO timestamp
    ended_at        TEXT,                       -- ISO timestamp, NULL if unknown/abandoned
    UNIQUE (harness_id, external_id)
);

-- User's input
CREATE TABLE prompts (
    id              TEXT PRIMARY KEY,           -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    external_id     TEXT,                       -- harness's message ID
    timestamp       TEXT NOT NULL,
    UNIQUE (conversation_id, external_id)
);

-- Model's output
CREATE TABLE responses (
    id              TEXT PRIMARY KEY,           -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    prompt_id       TEXT REFERENCES prompts(id),  -- what it's responding to
    model_id        TEXT REFERENCES models(id),
    provider_id     TEXT REFERENCES providers(id),
    external_id     TEXT,                       -- harness's message ID
    timestamp       TEXT NOT NULL,
    input_tokens    INTEGER,                    -- universal
    output_tokens   INTEGER,                    -- universal
    UNIQUE (conversation_id, external_id)
);

-- Tool invocations during response generation
CREATE TABLE tool_calls (
    id              TEXT PRIMARY KEY,           -- ULID
    response_id     TEXT NOT NULL REFERENCES responses(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),  -- denormalized for convenience
    tool_id         TEXT REFERENCES tools(id),
    external_id     TEXT,                       -- model-assigned tool_call_id
    input           TEXT,                       -- JSON arguments
    result          TEXT,                       -- JSON result
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
    prompt_id       TEXT NOT NULL REFERENCES prompts(id),
    block_index     INTEGER NOT NULL,
    block_type      TEXT NOT NULL,              -- text, image, file
    content         TEXT NOT NULL,              -- the actual content or reference
    UNIQUE (prompt_id, block_index)
);

-- Content blocks in responses (text, thinking, tool references)
CREATE TABLE response_content (
    id              TEXT PRIMARY KEY,           -- ULID
    response_id     TEXT NOT NULL REFERENCES responses(id),
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
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,                       -- NULL=user, 'provider', 'analyzer', etc.
    UNIQUE (conversation_id, key, scope)
);

CREATE TABLE prompt_attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    prompt_id       TEXT NOT NULL REFERENCES prompts(id),
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,
    UNIQUE (prompt_id, key, scope)
);

CREATE TABLE response_attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    response_id     TEXT NOT NULL REFERENCES responses(id),
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,
    UNIQUE (response_id, key, scope)
);

CREATE TABLE tool_call_attributes (
    id              TEXT PRIMARY KEY,           -- ULID
    tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id),
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    scope           TEXT,
    UNIQUE (tool_call_id, key, scope)
);

--------------------------------------------------------------------------------
-- LABEL TABLES
-- User-defined categorization
--------------------------------------------------------------------------------

CREATE TABLE labels (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE workspace_labels (
    id              TEXT PRIMARY KEY,           -- ULID
    workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
    label_id        TEXT NOT NULL REFERENCES labels(id),
    applied_at      TEXT NOT NULL,
    UNIQUE (workspace_id, label_id)
);

CREATE TABLE conversation_labels (
    id              TEXT PRIMARY KEY,           -- ULID
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    label_id        TEXT NOT NULL REFERENCES labels(id),
    applied_at      TEXT NOT NULL,
    UNIQUE (conversation_id, label_id)
);

--------------------------------------------------------------------------------
-- OPERATIONAL TABLES
-- Ingestion tracking
--------------------------------------------------------------------------------

CREATE TABLE ingested_files (
    id              TEXT PRIMARY KEY,           -- ULID
    path            TEXT NOT NULL UNIQUE,
    file_hash       TEXT NOT NULL,
    harness_id      TEXT NOT NULL REFERENCES harnesses(id),
    conversation_id TEXT REFERENCES conversations(id),
    ingested_at     TEXT NOT NULL
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
-- FTS5 FULL-TEXT SEARCH INDEX
-- Indexes text content from prompt_content and response_content
--------------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    text_content,
    content_id UNINDEXED,
    side UNINDEXED,
    conversation_id UNINDEXED
);
