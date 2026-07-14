"""Tests for Claude Code adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, default_location_source, write_jsonl

from siftd.adapters import claude_code
from siftd.domain.source import Source


class TestClaudeCodeAdapter:

    def test_can_handle(self):
        assert claude_code.can_handle(Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl"))
        assert not claude_code.can_handle(Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json"))

    def test_parse_full(self):
        conv = list(claude_code.parse(Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")))[0]
        assert conv.external_id == "claude_code::test-session-1" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "claude_code" and conv.harness.source == "anthropic"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert "Hello" in prompt.content[0].content["text"] and len(prompt.responses) == 2
        resp = prompt.responses[0]
        tc = resp.tool_calls[0]
        assert tc.tool_name == "Read" and tc.status == "success" and "Test Project" in str(tc.result)
        assert resp.usage.input_tokens == 100 and resp.usage.output_tokens == 50
        assert resp.attributes.get("cache_creation_input_tokens") == "10"

    def test_string_content_and_subagent(self, tmp_path):
        records = [
            {"type": "user", "sessionId": "s1", "agentId": "sub-1", "cwd": "/ws", "timestamp": "T1",
             "uuid": "u1", "message": {"role": "user", "content": "plain string"}},
            {"type": "assistant", "sessionId": "s1", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-3", "content": None,
                 "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert "agent" in conv.external_id and conv.prompts[0].content[0].content["text"] == "plain string"
        assert conv.prompts[0].responses[0].attributes.get("cache_read_input_tokens") == "3"
        n = claude_code.normalize_record
        assert n({"type": "user", "timestamp": "T1", "message": {"role": "user", "content": "hi"}}).kind == "user"
        assert n({"type": "assistant", "timestamp": "T2", "message": {"role": "assistant", "content": None}}).content_blocks == []
        assert n({"type": "system"}) is None
        assert not claude_code.can_handle(Source(kind="file", location=Path("/home/.codex/sessions/s.jsonl")))

    def test_subagent_reads_meta_sidecar(self, tmp_path):
        # The agent-<id>.meta.json sidecar beside a sub-agent transcript binds
        # agentId -> {agentType, description}; the adapter captures both as
        # conversation attributes (the child JSONL carries no type of its own).
        import json

        records = [
            {"type": "user", "sessionId": "root1", "agentId": "abc", "cwd": "/ws",
             "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "do the thing"}},
        ]
        jsonl = write_jsonl(tmp_path, records, name="agent-abc.jsonl")
        (tmp_path / "agent-abc.meta.json").write_text(
            json.dumps({"agentType": "Explore", "description": "scout the repo",
                        "toolUseId": "toolu_x"})
        )
        conv = list(claude_code.parse(Source(kind="file", location=jsonl)))[0]
        assert conv.attributes["subagent_type"] == "Explore"
        assert conv.attributes["agent_description"] == "scout the repo"

    def test_subagent_without_sidecar_has_no_type(self, tmp_path):
        # Historical sub-agents whose sidecar rotated off disk before ingest, or
        # variants that never wrote one — degrade silently to no attribute.
        records = [
            {"type": "user", "sessionId": "root1", "agentId": "def", "cwd": "/ws",
             "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
        ]
        jsonl = write_jsonl(tmp_path, records, name="agent-def.jsonl")
        conv = list(claude_code.parse(Source(kind="file", location=jsonl)))[0]
        assert conv.attributes == {}

    def test_normalizer_edges(self):
        n = claude_code.normalize_record
        # String content (L307), list content (L308), unknown content type (L310)
        assert n({"type": "assistant", "timestamp": "T", "message": {"role": "assistant", "content": "text"}}).content_blocks[0]["text"] == "text"
        assert n({"type": "assistant", "timestamp": "T", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}).content_blocks
        assert n({"type": "assistant", "timestamp": "T", "message": {"role": "assistant", "content": 42}}).content_blocks == []
        # tool_result detection (L319)
        tr = n({"type": "user", "timestamp": "T", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x"}]}})
        assert tr.kind == "tool_result"
        # agentId extraction (L324) — only on user records
        u = n({"type": "user", "timestamp": "T", "agentId": "sub-1", "message": {"role": "user", "content": "hi"}})
        assert u.extra.get("agent_id") == "sub-1"

    def test_backgrounded_bash_and_bash_output_correlate(self, tmp_path):
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "run the tests in the background"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": [
                 {"type": "tool_use", "id": "tu-1", "name": "Bash",
                  "input": {"command": "go test ./...", "run_in_background": True}},
             ]}},
            {"type": "user", "sessionId": "s", "timestamp": "T3", "uuid": "u2",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu-1",
                  "content": "Command running in background with ID: buxu4lquj. Output is being "
                             "written to: /tmp/x/tasks/buxu4lquj.output. You will be notified "
                             "when it completes. To check interim output, use Read on that file path."},
             ]}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T4", "uuid": "a2",
             "message": {"role": "assistant", "model": "m", "content": [
                 {"type": "tool_use", "id": "tu-2", "name": "BashOutput", "input": {"bash_id": "buxu4lquj"}},
             ]}},
            {"type": "user", "sessionId": "s", "timestamp": "T5", "uuid": "u3",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu-2",
                  "content": {"content": "<status>killed</status>\n\n<stdout>done</stdout>"}},
             ]}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        bash_tc = conv.prompts[0].responses[0].tool_calls[0]
        output_tc = conv.prompts[0].responses[1].tool_calls[0]
        assert bash_tc.tool_name == "Bash"
        assert bash_tc.attributes["background_task_id"] == "buxu4lquj"
        assert output_tc.tool_name == "BashOutput"
        assert output_tc.attributes["background_task_id"] == "buxu4lquj"
        assert claude_code.TOOL_ALIASES["BashOutput"] == "shell.output"

    def test_tool_aliases_cover_current_tool_set(self):
        """Live spawn tool is Agent (Task kept for historical logs); the
        current task-tracking, plan-mode, and messaging tools are mapped."""
        aliases = claude_code.TOOL_ALIASES
        assert aliases["Agent"] == "task.spawn"
        assert aliases["Task"] == "task.spawn"  # historical logs still resolve
        assert aliases["TaskCreate"] == "ui.todo"
        assert aliases["TaskUpdate"] == "ui.todo"
        assert aliases["ToolSearch"] == "tool.search"
        assert aliases["SendMessage"] == "task.message"
        assert aliases["Workflow"] == "workflow.run"
        assert aliases["EnterPlanMode"] == "ui.plan"
        assert aliases["ExitPlanMode"] == "ui.plan"

    def test_foreground_bash_gets_no_background_attribute(self, tmp_path):
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "list files"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": [
                 {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "ls"}},
             ]}},
            {"type": "user", "sessionId": "s", "timestamp": "T3", "uuid": "u2",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu-1", "content": "README.md"},
             ]}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        tc = conv.prompts[0].responses[0].tool_calls[0]
        assert tc.attributes == {}

    def test_default_locations_prefer_config_dir(self):
        # ~/.config/claude/projects is where modern installs write live logs;
        # ordering matters because discovery iterates in list order.
        assert claude_code.DEFAULT_LOCATIONS == [
            "~/.config/claude/projects",
            "~/.claude/projects",
        ]

    def test_branch_from_per_record_git_branch(self, tmp_path, monkeypatch):
        # Records carry gitBranch; the ingest-time live lookup must not run
        # (it reflects the branch checked out NOW, not during the session).
        import siftd.git

        def boom(cwd):
            raise AssertionError("live branch lookup must not run when gitBranch present")

        monkeypatch.setattr(siftd.git, "get_worktree_branch", boom)
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "gitBranch": "feat/session-branch",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "gitBranch": "feat/other-branch",
             "message": {"role": "assistant", "model": "m", "content": "ok"}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert conv.branch == "feat/session-branch"

    def test_branch_falls_back_to_live_lookup_when_field_absent(self, tmp_path, monkeypatch):
        import siftd.git

        monkeypatch.setattr(siftd.git, "get_worktree_branch", lambda cwd: "live-branch")
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert conv.branch == "live-branch"

    def test_injected_user_records_reclassified(self, tmp_path):
        # isMeta / non-user promptSource records keep their place in the
        # conversation but their text moves off the "text" key so user-prompt
        # FTS indexing (keyed on $.text) never sees injected content.
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "isMeta": True,
             "message": {"role": "user", "content": "<local-command-caveat>injected caveat</local-command-caveat>"}},
            {"type": "user", "sessionId": "s", "timestamp": "T2", "uuid": "u2",
             "promptSource": "system",
             "message": {"role": "user", "content": [{"type": "text", "text": "skill injection"}]}},
            {"type": "user", "sessionId": "s", "timestamp": "T3", "uuid": "u3",
             "promptSource": "typed",
             "message": {"role": "user", "content": "real human prompt"}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        meta1, meta2, real = conv.prompts

        b = meta1.content[0]
        assert b.block_type == "meta_text"
        assert "text" not in b.content
        assert "injected caveat" in b.content["meta_text"]
        assert b.content["meta_source"] == "isMeta"

        b = meta2.content[0]
        assert b.block_type == "meta_text"
        assert "text" not in b.content
        assert b.content["meta_text"] == "skill injection"
        assert b.content["meta_source"] == "system"

        b = real.content[0]
        assert b.block_type == "text"
        assert b.content["text"] == "real human prompt"

    def test_normalizer_injected_records_are_metadata(self):
        # Peek exchange counts / prompt previews only reflect typed prompts.
        n = claude_code.normalize_record
        meta = n({"type": "user", "timestamp": "T", "isMeta": True, "sessionId": "s", "cwd": "/w",
                  "message": {"role": "user", "content": "caveat"}})
        assert meta.kind == "metadata" and meta.session_id == "s"
        sysm = n({"type": "user", "timestamp": "T", "promptSource": "system",
                  "message": {"role": "user", "content": "inject"}})
        assert sysm.kind == "metadata"
        queued = n({"type": "user", "timestamp": "T", "promptSource": "queued",
                    "message": {"role": "user", "content": "queued prompt"}})
        assert queued.kind == "user"

    def test_discover_and_parse_edges(self, tmp_path):
        assert list(claude_code.discover(locations=[str(FIXTURES_DIR)]))
        (tmp_path / "empty.jsonl").write_text("")
        assert list(claude_code.parse(Source(kind="file", location=tmp_path / "empty.jsonl"))) == []
        assert not claude_code.can_handle(Source(kind="directory", location=FIXTURES_DIR))
        assert claude_code.can_handle(default_location_source(claude_code))
        # Record with non-user/assistant type gets skipped (L167)
        records = [{"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1",
             "uuid": "u1", "message": {"role": "user", "content": "hi"}},
            {"type": "result", "sessionId": "s", "timestamp": "T2"},
            {"type": "assistant", "sessionId": "s", "timestamp": "T3", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": "ok"}}]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert len(conv.prompts) == 1

    def test_tool_use_result_agent_spawn_attributes(self, tmp_path):
        # Structured toolUseResult carries the real parent->child spawn
        # linkage that the prose tool_result lacks. Both live shapes:
        # teammate spawn (snake_case) and async Task spawn (camelCase).
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "spawn helpers"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": [
                 {"type": "tool_use", "id": "tu-1", "name": "Agent", "input": {"prompt": "go"}},
             ]}},
            {"type": "user", "sessionId": "s", "timestamp": "T3", "uuid": "u2",
             "toolUseResult": {"agent_id": "agent-xyz", "agent_type": "general-purpose",
                               "model": "opus", "name": "genq-impl", "spawnDepth": 1,
                               "status": "teammate_spawned"},
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu-1", "content": "Spawned teammate"},
             ]}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T4", "uuid": "a2",
             "message": {"role": "assistant", "model": "m", "content": [
                 {"type": "tool_use", "id": "tu-2", "name": "Task", "input": {"prompt": "go"}},
             ]}},
            {"type": "user", "sessionId": "s", "timestamp": "T5", "uuid": "u3",
             "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": "abc123",
                               "resolvedModel": "claude-sonnet-5", "prompt": "go"},
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu-2", "content": "Async agent launched"},
             ]}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        teammate = conv.prompts[0].responses[0].tool_calls[0]
        assert teammate.attributes["agent_id"] == "agent-xyz"
        assert teammate.attributes["agent_type"] == "general-purpose"
        assert teammate.attributes["agent_model"] == "opus"
        assert teammate.attributes["agent_name"] == "genq-impl"
        assert teammate.attributes["spawn_depth"] == "1"
        assert teammate.attributes["spawn_status"] == "teammate_spawned"
        async_spawn = conv.prompts[0].responses[1].tool_calls[0]
        assert async_spawn.attributes["agent_id"] == "abc123"
        assert async_spawn.attributes["agent_model"] == "claude-sonnet-5"
        assert async_spawn.attributes["spawn_status"] == "async_launched"
        assert "spawn_depth" not in async_spawn.attributes

    def test_tool_use_result_bash_stdout_stderr(self, tmp_path):
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "run it"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": [
                 {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "ls"}},
             ]}},
            {"type": "user", "sessionId": "s", "timestamp": "T3", "uuid": "u2",
             "toolUseResult": {"stdout": "README.md\n", "stderr": "warn: x",
                               "interrupted": False, "isImage": False},
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu-1", "content": "README.md"},
             ]}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        tc = conv.prompts[0].responses[0].tool_calls[0]
        assert tc.attributes["stdout"] == "README.md\n"
        assert tc.attributes["stderr"] == "warn: x"
        assert "interrupted" not in tc.attributes

    def test_tool_use_result_string_form_ignored(self, tmp_path):
        # Error-prose toolUseResult (a bare string) carries nothing structured.
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "edit"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": [
                 {"type": "tool_use", "id": "tu-1", "name": "Edit", "input": {"file_path": "/f"}},
             ]}},
            {"type": "user", "sessionId": "s", "timestamp": "T3", "uuid": "u2",
             "toolUseResult": "Error: File has not been read yet.",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu-1", "is_error": True,
                  "content": "Error: File has not been read yet."},
             ]}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        tc = conv.prompts[0].responses[0].tool_calls[0]
        assert tc.status == "error" and tc.attributes == {}

    def test_usage_ttl_split_and_server_tool_use(self, tmp_path):
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": "ok",
                 "usage": {"input_tokens": 10, "output_tokens": 5,
                           "cache_creation_input_tokens": 44795,
                           "cache_read_input_tokens": 0,
                           "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                              "ephemeral_1h_input_tokens": 44695},
                           "server_tool_use": {"web_search_requests": 2,
                                               "web_fetch_requests": 0}}}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        attrs = conv.prompts[0].responses[0].attributes
        assert attrs["cache_creation_ephemeral_5m_input_tokens"] == "100"
        assert attrs["cache_creation_ephemeral_1h_input_tokens"] == "44695"
        assert attrs["server_tool_use_web_search_requests"] == "2"
        # zero-valued counters follow the existing falsy-skip convention
        assert "server_tool_use_web_fetch_requests" not in attrs

    def test_ai_title_last_occurrence_wins(self, tmp_path):
        records = [
            {"type": "ai-title", "aiTitle": "First draft title", "sessionId": "s"},
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
            {"type": "ai-title", "aiTitle": "Add Antigravity adapter configuration", "sessionId": "s"},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert conv.attributes["title"] == "Add Antigravity adapter configuration"

    def test_turn_duration_attaches_to_last_response(self, tmp_path):
        records = [
            # turn_duration before any response is skipped, not crashed on
            {"type": "system", "subtype": "turn_duration", "durationMs": 9,
             "sessionId": "s", "timestamp": "T0", "uuid": "s0"},
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": "thinking..."}},
            {"type": "assistant", "sessionId": "s", "timestamp": "T3", "uuid": "a2",
             "message": {"role": "assistant", "model": "m", "content": "done"}},
            {"type": "system", "subtype": "turn_duration", "durationMs": 114244,
             "messageCount": 63, "sessionId": "s", "timestamp": "T4", "uuid": "s1"},
            # other system subtypes stay dropped
            {"type": "system", "subtype": "local_command", "sessionId": "s",
             "timestamp": "T5", "uuid": "s2"},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        responses = conv.prompts[0].responses
        assert "turn_duration_ms" not in responses[0].attributes
        assert responses[1].attributes["turn_duration_ms"] == "114244"
