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
CREATE TABLE workspaces (
    id              TEXT PRIMARY KEY,           -- ULID
    path            TEXT NOT NULL UNIQUE,       -- /Users/kaygee/Code/tbd
    git_remote      TEXT,                       -- git@github.com:user/repo.git
    discovered_at   TEXT NOT NULL               -- ISO timestamp
);
CREATE TABLE tags (
    id              TEXT PRIMARY KEY,           -- ULID
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_workspaces_git_remote ON workspaces(git_remote);
CREATE TABLE content_blobs (
    hash TEXT PRIMARY KEY,              -- SHA256 of content (natural key)
    content TEXT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL            -- ISO timestamp
);
CREATE INDEX idx_content_blobs_ref_count ON content_blobs(ref_count);
CREATE TABLE sync_inbox (
    id                  TEXT PRIMARY KEY,
    received_at         TEXT NOT NULL,
    processed_at        TEXT,
    processing_started_at TEXT,
    status              TEXT NOT NULL DEFAULT 'staged',
    source_host         TEXT,
    size_bytes          INTEGER,
    conversations       INTEGER
);
CREATE VIRTUAL TABLE content_fts USING fts5(
            text_content,
            content_id UNINDEXED,
            side UNINDEXED,
            conversation_id UNINDEXED,
            tokenize='porter unicode61 remove_diacritics 1'
        )
/* content_fts(text_content,content_id,side,conversation_id) */;
CREATE TABLE IF NOT EXISTS 'content_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE IF NOT EXISTS 'content_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'content_fts_content'(id INTEGER PRIMARY KEY, c0, c1, c2, c3);
CREATE TABLE IF NOT EXISTS 'content_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'content_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
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
        ON pending_tags(harness_session_id)
    ;
CREATE TABLE prompt_tags (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            applied_at TEXT NOT NULL,
            UNIQUE (prompt_id, tag_id)
        );
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
        ON conversation_owners(user_id)
    ;
CREATE TABLE IF NOT EXISTS "tool_aliases" (
                id              TEXT PRIMARY KEY,
                raw_name        TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                tool_id         TEXT NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
                UNIQUE (raw_name, harness_id)
            );
CREATE INDEX idx_tool_aliases_tool ON tool_aliases(tool_id);
CREATE INDEX idx_tool_aliases_harness ON tool_aliases(harness_id);
CREATE TABLE IF NOT EXISTS "pricing" (
                id              TEXT PRIMARY KEY,
                model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
                provider_id     TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
                input_per_mtok  REAL,
                output_per_mtok REAL,
                UNIQUE (model_id, provider_id)
            );
CREATE TABLE IF NOT EXISTS "conversations" (
                id              TEXT PRIMARY KEY,
                external_id     TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                workspace_id    TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
                branch          TEXT,
                started_at      TEXT NOT NULL,
                ended_at        TEXT,
                UNIQUE (harness_id, external_id)
            );
CREATE INDEX idx_conversations_harness ON conversations(harness_id);
CREATE INDEX idx_conversations_workspace ON conversations(workspace_id);
CREATE INDEX idx_conversations_started ON conversations(started_at);
CREATE INDEX idx_conversations_ended ON conversations(ended_at);
CREATE TABLE IF NOT EXISTS "prompts" (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                external_id     TEXT,
                timestamp       TEXT NOT NULL,
                UNIQUE (conversation_id, external_id)
            );
CREATE INDEX idx_prompts_conversation ON prompts(conversation_id);
CREATE INDEX idx_prompts_timestamp ON prompts(timestamp);
CREATE TABLE IF NOT EXISTS "responses" (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                prompt_id       TEXT REFERENCES prompts(id) ON DELETE CASCADE,
                model_id        TEXT REFERENCES models(id) ON DELETE SET NULL,
                provider_id     TEXT REFERENCES providers(id) ON DELETE SET NULL,
                external_id     TEXT,
                timestamp       TEXT NOT NULL,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                UNIQUE (conversation_id, external_id)
            );
CREATE INDEX idx_responses_conversation ON responses(conversation_id);
CREATE INDEX idx_responses_prompt ON responses(prompt_id);
CREATE INDEX idx_responses_model ON responses(model_id);
CREATE INDEX idx_responses_timestamp ON responses(timestamp);
CREATE TABLE IF NOT EXISTS "tool_calls" (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                tool_id         TEXT REFERENCES tools(id) ON DELETE SET NULL,
                external_id     TEXT,
                input           TEXT,
                result          TEXT,
                result_hash     TEXT REFERENCES content_blobs(hash),
                status          TEXT,
                timestamp       TEXT
            );
CREATE INDEX idx_tool_calls_response ON tool_calls(response_id);
CREATE INDEX idx_tool_calls_conversation ON tool_calls(conversation_id);
CREATE INDEX idx_tool_calls_tool ON tool_calls(tool_id);
CREATE INDEX idx_tool_calls_status ON tool_calls(status);
CREATE TABLE IF NOT EXISTS "prompt_content" (
                id              TEXT PRIMARY KEY,
                prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                block_index     INTEGER NOT NULL,
                block_type      TEXT NOT NULL,
                content         TEXT NOT NULL,
                UNIQUE (prompt_id, block_index)
            );
CREATE INDEX idx_prompt_content_prompt ON prompt_content(prompt_id);
CREATE TABLE IF NOT EXISTS "response_content" (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                block_index     INTEGER NOT NULL,
                block_type      TEXT NOT NULL,
                content         TEXT NOT NULL,
                UNIQUE (response_id, block_index)
            );
CREATE INDEX idx_response_content_response ON response_content(response_id);
CREATE TABLE IF NOT EXISTS "conversation_attributes" (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (conversation_id, key, scope)
            );
CREATE TABLE IF NOT EXISTS "prompt_attributes" (
                id              TEXT PRIMARY KEY,
                prompt_id       TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (prompt_id, key, scope)
            );
CREATE TABLE IF NOT EXISTS "response_attributes" (
                id              TEXT PRIMARY KEY,
                response_id     TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (response_id, key, scope)
            );
CREATE INDEX idx_response_attributes_key ON response_attributes(key, response_id, value);
CREATE TABLE IF NOT EXISTS "tool_call_attributes" (
                id              TEXT PRIMARY KEY,
                tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
                key             TEXT NOT NULL,
                value           TEXT NOT NULL,
                scope           TEXT,
                UNIQUE (tool_call_id, key, scope)
            );
CREATE TABLE IF NOT EXISTS "workspace_tags" (
                id              TEXT PRIMARY KEY,
                workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (workspace_id, tag_id)
            );
CREATE INDEX idx_workspace_tags_tag ON workspace_tags(tag_id);
CREATE TABLE IF NOT EXISTS "conversation_tags" (
                id              TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (conversation_id, tag_id)
            );
CREATE INDEX idx_conversation_tags_tag ON conversation_tags(tag_id);
CREATE TABLE IF NOT EXISTS "tool_call_tags" (
                id              TEXT PRIMARY KEY,
                tool_call_id    TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
                tag_id          TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                applied_at      TEXT NOT NULL,
                UNIQUE (tool_call_id, tag_id)
            );
CREATE INDEX idx_tool_call_tags_tag ON tool_call_tags(tag_id);
CREATE TABLE IF NOT EXISTS "ingested_files" (
                id              TEXT PRIMARY KEY,
                path            TEXT NOT NULL UNIQUE,
                file_hash       TEXT NOT NULL,
                harness_id      TEXT NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                ingested_at     TEXT NOT NULL,
                error           TEXT,
                file_mtime      REAL,
                file_size       INTEGER
            );
PRAGMA user_version = 2;
