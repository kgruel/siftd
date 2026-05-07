CREATE VIRTUAL TABLE content_fts USING fts5(
    text_content,
    event_content_id UNINDEXED,
    event_id         UNINDEXED,
    conversation_id  UNINDEXED,
    tokenize='porter unicode61 remove_diacritics 1'
);
CREATE TABLE harnesses (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,       -- claude_code, gemini_cli, codex_cli, opencode
    version         TEXT,                       -- 1.0.3, 2.1.0
    display_name    TEXT,                       -- "Claude Code", "Gemini CLI"
    source          TEXT,                       -- anthropic, openai, google, community
    log_format      TEXT                        -- jsonl, json_array, event_stream
);
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
CREATE TABLE providers (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,       -- anthropic, openai, google, openrouter, local
    display_name    TEXT,                       -- "Anthropic API", "OpenRouter"
    billing_model   TEXT                        -- token, subscription, local, proxy
);
CREATE TABLE tools (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,       -- canonical: file.read, shell.execute, search.grep
    category        TEXT,                       -- file, shell, search, web, edit
    description     TEXT
);
CREATE TABLE tool_aliases (
    id              TEXT PRIMARY KEY,           -- ULID
    raw_name        TEXT NOT NULL,              -- Read, read_file, Bash, run_shell_command
    harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
    tool_id         TEXT NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
    UNIQUE (raw_name, harness_id)
);
CREATE INDEX idx_tool_aliases_tool ON tool_aliases(tool_id);
CREATE INDEX idx_tool_aliases_harness ON tool_aliases(harness_id);
CREATE TABLE pricing (
    id              TEXT PRIMARY KEY,           -- ULID
    model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    provider_id     TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    input_per_mtok  REAL,                       -- $ per million input tokens
    output_per_mtok REAL,                       -- $ per million output tokens
    UNIQUE (model_id, provider_id)
);
CREATE TABLE workspaces (
    id              TEXT PRIMARY KEY,           -- ULID
    path            TEXT NOT NULL UNIQUE,       -- /Users/kaygee/Code/tbd
    git_remote      TEXT,                       -- git@github.com:user/repo.git
    discovered_at   TEXT NOT NULL               -- ISO timestamp
);
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
CREATE TABLE tags (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    created_at      TEXT NOT NULL
);
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
CREATE INDEX idx_conversations_harness ON conversations(harness_id);
CREATE INDEX idx_conversations_workspace ON conversations(workspace_id);
CREATE INDEX idx_conversations_started ON conversations(started_at);
CREATE INDEX idx_conversations_ended ON conversations(ended_at);
CREATE INDEX idx_workspaces_git_remote ON workspaces(git_remote);
CREATE TABLE content_blobs (
    hash TEXT PRIMARY KEY,              -- SHA256 of content (natural key)
    content TEXT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 1 CHECK (ref_count >= 0),
    created_at TEXT NOT NULL            -- ISO timestamp
);
CREATE INDEX idx_content_blobs_ref_count ON content_blobs(ref_count);
CREATE TABLE sync_inbox (
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
CREATE TABLE event_response (
    event_id        TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    model_id        TEXT REFERENCES models(id) ON DELETE SET NULL,
    provider_id     TEXT REFERENCES providers(id) ON DELETE SET NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER
);
CREATE TABLE event_tool_call (
    event_id        TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
    input           TEXT,                       -- JSON arguments
    result_hash     TEXT REFERENCES content_blobs(hash),
    status          TEXT                        -- success | error | pending
);
CREATE TABLE event_content (
    id              TEXT PRIMARY KEY,           -- ULID (preserved from prompt_content/response_content)
    event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    block_index     INTEGER NOT NULL,
    block_type      TEXT NOT NULL,              -- text | thinking | tool_use | tool_result | image | ...
    content         TEXT NOT NULL,
    UNIQUE (event_id, block_index)
);
CREATE INDEX idx_event_content_event ON event_content(event_id);
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
CREATE TABLE active_sessions (
            harness_session_id TEXT PRIMARY KEY,
            adapter_name TEXT NOT NULL,
            workspace_path TEXT,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
CREATE TABLE pending_tags (
            id TEXT PRIMARY KEY,
            harness_session_id TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'conversation',
            exchange_index INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE (harness_session_id, tag_name, entity_type, exchange_index)
        );
CREATE INDEX idx_pending_tags_session
        ON pending_tags(harness_session_id);
CREATE TABLE conversation_stats (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    prompt_count    INTEGER NOT NULL DEFAULT 0,
    response_count  INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    model_name      TEXT,
    cost            REAL
);
CREATE TABLE conversation_owners (
            conversation_id TEXT NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            user_id         TEXT NOT NULL,
            push_id         TEXT,
            assigned_at     TEXT NOT NULL,
            PRIMARY KEY (conversation_id)
        );
CREATE INDEX idx_conversation_owners_user
        ON conversation_owners(user_id);
CREATE TRIGGER tr_event_tool_call_delete_release_blob
AFTER DELETE ON event_tool_call
FOR EACH ROW
WHEN OLD.result_hash IS NOT NULL
BEGIN
    UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0) WHERE hash = OLD.result_hash;
    DELETE FROM content_blobs WHERE hash = OLD.result_hash AND ref_count <= 0;
END;
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
CREATE TRIGGER tr_polymorphic_conversations_cleanup
AFTER DELETE ON conversations
BEGIN
    DELETE FROM tag_assignments WHERE target_id = OLD.id AND target_kind = 'conversation';
    DELETE FROM attributes WHERE target_id = OLD.id AND target_kind = 'conversation';
END;
CREATE TRIGGER tr_polymorphic_events_cleanup
AFTER DELETE ON events
BEGIN
    DELETE FROM tag_assignments
    WHERE target_id = OLD.id
      AND target_kind IN ('prompt', 'response', 'tool_call', 'exchange');
    DELETE FROM attributes
    WHERE target_id = OLD.id
      AND target_kind IN ('prompt', 'response', 'tool_call', 'exchange');
END;
CREATE TRIGGER tr_polymorphic_workspaces_cleanup
AFTER DELETE ON workspaces
BEGIN
    DELETE FROM tag_assignments WHERE target_id = OLD.id AND target_kind = 'workspace';
    DELETE FROM attributes WHERE target_id = OLD.id AND target_kind = 'workspace';
END;
PRAGMA user_version = 8;
