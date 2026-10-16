"""Tests for Claude Code adapter."""

import json
import re
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR, default_location_source, write_jsonl

from siftd.adapters import claude_code
from siftd.adapters.assembly import AssemblyError
from siftd.adapters.contract import adapt
from siftd.adapters.records import Disposition
from siftd.domain.source import Source


def _assembly(path):
    """The assembled source and its ledger, as parse() sees them."""
    return adapt(claude_code).assemble(Source(kind="file", location=path))


def _only(path):
    (conversation,) = adapt(claude_code).parse(Source(kind="file", location=path))
    return conversation


def _user(uuid, at, content, **extra):
    return {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": at, "uuid": uuid,
            "message": {"role": "user", "content": content}, **extra}


def _assistant(uuid, at, content, message_id=None, **message):
    record = {"type": "assistant", "sessionId": "s", "timestamp": at, "uuid": uuid,
              "message": {"role": "assistant", "model": "m", "content": content, **message}}
    if message_id is not None:
        record["message"]["id"] = message_id
    return record


def _tool_use(call_id, name="Bash", **input):
    return {"type": "tool_use", "id": call_id, "name": name, "input": input}


def _tool_result(call_id, content="ok", **extra):
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, **extra}


class TestClaudeCodeAdapter:

    def test_can_handle(self):
        assert claude_code.can_handle(Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl"))
        assert not claude_code.can_handle(Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json"))

    def test_parse_full(self):
        conv = list(adapt(claude_code).parse(Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert "agent" in conv.external_id and conv.prompts[0].content[0].content["text"] == "plain string"
        assert conv.prompts[0].responses[0].attributes.get("cache_read_input_tokens") == "3"
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=jsonl)))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=jsonl)))[0]
        assert "subagent_type" not in conv.attributes and "agent_description" not in conv.attributes

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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert conv.branch == "feat/session-branch"

    def test_branch_falls_back_to_live_lookup_when_field_absent(self, tmp_path, monkeypatch):
        import siftd.git

        monkeypatch.setattr(siftd.git, "get_worktree_branch", lambda cwd: "live-branch")
        records = [
            {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
             "message": {"role": "user", "content": "hi"}},
        ]
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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

    def test_discover_and_parse_edges(self, tmp_path):
        assert list(claude_code.discover(locations=[str(FIXTURES_DIR)]))
        (tmp_path / "empty.jsonl").write_text("")
        assert list(adapt(claude_code).parse(Source(kind="file", location=tmp_path / "empty.jsonl"))) == []
        assert not claude_code.can_handle(Source(kind="directory", location=FIXTURES_DIR))
        assert claude_code.can_handle(default_location_source(claude_code))
        # Record with non-user/assistant type gets skipped (L167)
        records = [{"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1",
             "uuid": "u1", "message": {"role": "user", "content": "hi"}},
            {"type": "result", "sessionId": "s", "timestamp": "T2"},
            {"type": "assistant", "sessionId": "s", "timestamp": "T3", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": "ok"}}]
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
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
        conv = list(adapt(claude_code).parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        responses = conv.prompts[0].responses
        assert "turn_duration_ms" not in responses[0].attributes
        assert responses[1].attributes["turn_duration_ms"] == "114244"


GOLDEN = FIXTURES_DIR / "adapters" / "claude_code"


def _fixture(case):
    return GOLDEN / case / "input.jsonl"


def _calls(conversation):
    responses = list(conversation.preamble) + [r for p in conversation.prompts for r in p.responses]
    return [c for r in responses for c in r.tool_calls]


class TestAssembledShape:
    """What the shared assembler does with Claude Code's records, on captured shapes."""

    def test_leading_assistant_records_land_in_the_preamble(self):
        """parse() built the response for an assistant record that came before any prompt, then
        attached it to nothing: it and its tool call vanished, and a file with no prompt at all
        yielded no conversation (#93)."""
        conversation = _only(_fixture("assistant_before_prompt"))
        assert len(conversation.preamble) == 1
        (preamble,) = conversation.preamble
        assert [c.tool_name for c in preamble.tool_calls] == ["Bash"]
        assert preamble.tool_calls[0].status == "success"
        assert len(conversation.prompts) == 1 and len(conversation.prompts[0].responses) == 1
        assert _assembly(_fixture("assistant_before_prompt")).ledger.count(Disposition.ORPHAN) == 0

    def test_fork_transcript_is_kept_with_its_lineage(self):
        """A fork transcript starts with a fork-context-ref and has no prompt of its own. parse() dropped
        the whole file (37 responses, 22 tool calls in the real one); the ref is the only pointer to its parent."""
        assembly = _assembly(_fixture("fork_context_ref"))
        (conversation,) = assembly.conversations
        fork = conversation.parent("fork")
        assert fork is not None and fork.parent == "claude_code::06194393-48f0-4e17-aee4-c6b228547a1c"
        assert fork.anchor == "4f1e004c-5919-4d60-99aa-133a0db88d17"
        assert conversation.prompts == [] and len(conversation.preamble) == 3
        assert [c.tool_name for c in _calls(conversation)] == ["Agent", "Bash"]
        assert all(c.status == "success" for c in _calls(conversation))
        # It is also a sub-agent: containment is a second relation, kept beside the fork, not ignored.
        assert conversation.parent("subagent").parent == "claude_code::06194393-48f0-4e17-aee4-c6b228547a1c"
        assert (Disposition.IGNORED, "conversation already has a parent") not in assembly.ledger.reasons

    def test_a_forked_subagent_peeks_as_a_subagent_of_the_session_it_ran_in(self):
        """The fork ref arrives first. Peek derived a sub-agent's id from the only lineage the assembler kept, so
        a forked sub-agent showed as '<session>::agent::<id>' instead of '<session>:<id>' and named no parent."""
        scan = adapt(claude_code).peek_scan(_fixture("fork_context_ref"))
        assert scan.session_id == "06194393-48f0-4e17-aee4-c6b228547a1c:a313e0c2c15430b41"
        assert scan.parent_session_id == "06194393-48f0-4e17-aee4-c6b228547a1c"

    def test_text_beside_a_tool_result_is_named_not_dropped(self):
        """The fork's directive rides in the same record as the result of the call that spawned it. parse() read
        only the result and dropped the text unseen; the ledger now counts it by name."""
        ledger = _assembly(_fixture("fork_context_ref")).ledger
        assert ledger.reasons[(Disposition.IGNORED, "user:block-beside-tool_result")] == 1

    def test_tool_result_written_before_its_call_pairs(self):
        """Claude Code can write a result a line before the assistant record that issued the call. parse() found
        no pending call for it, dropped the result, and left the call pending forever (14 in the real corpus)."""
        path = _fixture("result_before_call")
        raw = [json.loads(line) for line in path.read_text().splitlines()]
        use_line = {
            b["id"]: i for i, r in enumerate(raw) if r["type"] == "assistant"
            for b in r["message"]["content"] if b["type"] == "tool_use"
        }
        result_line = {
            b["tool_use_id"]: i for i, r in enumerate(raw) if r["type"] == "user" and isinstance(r["message"]["content"], list)
            for b in r["message"]["content"] if b["type"] == "tool_result"
        }
        early = [call for call, i in result_line.items() if i < use_line[call]]
        assert early, "the fixture must hold a result that precedes its call"
        calls = {c.external_id: c for c in _calls(_only(path))}
        for call_id in early:
            assert calls[call_id].status == "success" and calls[call_id].result is not None
        assert all(c.status == "success" for c in calls.values())
        assert _assembly(path).ledger.count(Disposition.ORPHAN) == 0

    def test_chunks_of_one_message_merge_into_one_response(self, tmp_path):
        """Each content block of an API message is its own record sharing message.id. parse() made a response
        per record (83,618 for 40,235 messages in the real corpus); they are one response, in order, holding
        the calls of every chunk, under the first chunk's id."""
        records = [
            _user("u1", "T1", "go"),
            _assistant("a1", "T2", [{"type": "thinking", "thinking": "hm"}], "msg_1"),
            _assistant("a2", "T3", [_tool_use("t1", command="ls")], "msg_1"),
            _user("r1", "T4", [_tool_result("t1")]),
            _assistant("a3", "T5", [_tool_use("t2", command="pwd")], "msg_1"),
            _user("r2", "T6", [_tool_result("t2")]),
        ]
        conversation = _only(write_jsonl(tmp_path, records))
        (response,) = conversation.prompts[0].responses
        assert response.external_id == "claude_code::a1"
        assert [b.block_type for b in response.content] == ["thinking", "tool_use", "tool_use"]
        assert [(c.external_id, c.status) for c in response.tool_calls] == [("t1", "success"), ("t2", "success")]

    def test_a_message_without_an_id_is_its_own_response(self, tmp_path):
        records = [_user("u1", "T1", "go"), _assistant("a1", "T2", "x"), _assistant("a2", "T3", "y")]
        conversation = _only(write_jsonl(tmp_path, records))
        assert [r.external_id for r in conversation.prompts[0].responses] == ["claude_code::a1", "claude_code::a2"]

    def test_non_message_records_do_not_stretch_the_bounds(self, tmp_path):
        """Conversation bounds are the first and last user or assistant record, as they always were; a queue entry
        or hook summary stamped hours later is not part of the conversation's span."""
        records = [
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-01-01T00:00:00Z", "sessionId": "s"},
            _user("u1", "2026-01-01T10:00:00Z", "hi"),
            _assistant("a1", "2026-01-01T10:00:05Z", "ok"),
            {"type": "system", "subtype": "turn_duration", "durationMs": 5, "timestamp": "2026-01-01T23:00:00Z", "sessionId": "s"},
            {"type": "pr-link", "timestamp": "2026-01-02T00:00:00Z", "sessionId": "s"},
        ]
        conversation = _only(write_jsonl(tmp_path, records))
        assert (conversation.started_at, conversation.ended_at) == ("2026-01-01T10:00:00Z", "2026-01-01T10:00:05Z")


class TestDecoding:
    def test_background_task_id_comes_from_the_notice_not_from_quoting_it(self, tmp_path):
        """The task-id phrase is the harness's notice for a backgrounded Bash call and begins its result. Other
        output may quote it (a Read of this adapter's tests does), so only a result that starts with it names an id.
        The claim is about the result's text, so a call the harness backgrounded itself (no run_in_background
        input; a timeout) names its id too."""
        said = "Command running in background with ID: abc123. Output is being written to: /tmp/x"
        quoted = f"     1\t{said}\n     2\tmore"
        records = [
            _user("u1", "T1", "go"),
            _assistant("a1", "T2", [
                _tool_use("bg", command="make", run_in_background=True),
                _tool_use("auto", command="make slow"),
                _tool_use("rd", name="Read", file_path="/f"),
            ]),
            _user("r1", "T3", [_tool_result("bg", said), _tool_result("auto", said), _tool_result("rd", quoted)]),
        ]
        calls = {c.external_id: c for c in _calls(_only(write_jsonl(tmp_path, records)))}
        assert calls["bg"].attributes == {"background_task_id": "abc123"}
        assert calls["auto"].attributes == {"background_task_id": "abc123"}
        assert calls["rd"].attributes == {}

    def test_a_background_result_written_before_its_call_still_names_its_task(self, tmp_path):
        """Pairing handles a result that precedes its call (14 in the corpus), but the task id was only read when
        the call had already been seen, so reversing the two lines changed the stored attributes."""
        said = "Command running in background with ID: abc123. Output is being written to: /tmp/x"
        result = _user("r1", "T3", [_tool_result("bg", said)])
        call = _assistant("a1", "T2", [_tool_use("bg", command="make", run_in_background=True)])
        outcomes = []
        for order in ([call, result], [result, call]):
            records = [_user("u1", "T1", "go"), *order]
            outcomes.append(_calls(_only(write_jsonl(tmp_path, records, name=f"o{len(outcomes)}.jsonl")))[0])
        assert [c.attributes for c in outcomes] == [{"background_task_id": "abc123"}] * 2
        assert all(c.status == "success" for c in outcomes)

    def test_every_record_type_is_named_and_the_table_matches_the_code(self):
        """The decoder's docstring table is the record-type ledger; it must not drift from what the code does."""
        rows = []
        for line in claude_code.ClaudeDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows.append((re.match(r"`([^`]+)`", cells[0]).group(1), cells[1]))
        assert len(rows) > 30
        ignored = {name for name, becomes in rows if becomes.startswith("Ignored")}
        assert {n for n in ignored if "/" not in n} == claude_code._IGNORED_TYPES
        assert {n.split("/")[1] for n in ignored if n.startswith("system/")} == claude_code._IGNORED_SYSTEM_SUBTYPES
        decoder = claude_code.ClaudeDecoder(Source(kind="file", location=Path("/x/s.jsonl")))
        for name, _ in rows:
            kind, _, subtype = name.partition("/")
            unit = {"type": kind, **({"subtype": subtype, "durationMs": 1} if subtype else {}), "aiTitle": "t",
                    "parentSessionId": "p"}
            if kind in ("user", "assistant"):
                unit["message"] = {"role": kind, "content": "x"}
            records = decoder.decode(unit)
            assert records and not any(type(r).__name__ == "Unknown" for r in records), name

    def test_an_unrecognized_type_is_unknown_and_counted(self, tmp_path):
        path = write_jsonl(tmp_path, [
            _user("u1", "T1", "hi"),
            {"type": "a-new-record-type", "sessionId": "s"},
            {"type": "system", "subtype": "a-new-subtype"},
        ])
        with path.open("a") as f:
            f.write("{truncated\n")
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "type:a-new-record-type")] == 1
        assert ledger.reasons[(Disposition.UNKNOWN, "system:a-new-subtype")] == 1
        assert ledger.reasons[(Disposition.UNKNOWN, "malformed")] == 1
        assert ledger.disposed == ledger.records

    @pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir()))
    def test_fixtures_decode_with_nothing_unknown_or_orphaned(self, case):
        ledger = _assembly(_fixture(case)).ledger
        assert ledger.count(Disposition.UNKNOWN) == 0 and ledger.count(Disposition.ORPHAN) == 0

    def test_an_undated_record_takes_its_neighbours_time_and_a_file_with_none_is_an_error(self, tmp_path):
        """parse() stamped a record with no timestamp with the wall clock, so re-ingesting the file changed it.
        The assembler fills it from the nearest dated record and refuses a file that has no date anywhere."""
        records = [_user("u1", "2026-01-01T10:00:00Z", "hi"), _assistant("a1", None, "ok")]
        records[1].pop("timestamp")
        conversation = _only(write_jsonl(tmp_path, records))
        assert conversation.prompts[0].responses[0].timestamp == "2026-01-01T10:00:00Z"
        for record in records:
            record.pop("timestamp", None)
        with pytest.raises(AssemblyError, match="no timestamp"):
            _only(write_jsonl(tmp_path, records, name="undated.jsonl"))

    def test_a_file_without_a_session_id_is_named_for_its_stem(self, tmp_path):
        records = [{"type": "user", "cwd": "/w", "timestamp": "T1", "uuid": "u1",
                    "message": {"role": "user", "content": "hi"}}]
        conversation = _only(write_jsonl(tmp_path, records, name="loose-name.jsonl"))
        assert conversation.external_id == "claude_code::loose-name"
        assert conversation.workspace_path == "/w"
        records[0]["agentId"] = "ag1"
        conversation = _only(write_jsonl(tmp_path, records, name="agent-file.jsonl"))
        assert conversation.external_id == "claude_code::agent-file::agent::ag1"

    def test_identity_waits_for_every_id_the_file_states(self, tmp_path):
        """The legacy name used the first sessionId and the first agentId found anywhere in the file. A decoder that
        named the conversation at the first record with a sessionId gave a file whose agentId appeared later the
        parent's name, colliding with it and losing the sub-agent's sidecar and lineage."""
        records = [
            _user("u1", "T1", "hi"),
            _assistant("a1", "T2", "ok"),
        ]
        records[1]["agentId"] = "ag1"
        path = write_jsonl(tmp_path, records, name="agent-ag1.jsonl")
        path.with_name("agent-ag1.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "d"}))
        assembly = _assembly(path)
        (conversation,) = assembly.conversations
        assert conversation.external_id == "claude_code::s::agent::ag1"
        assert conversation.parent("subagent").parent == "claude_code::s"
        assert conversation.attributes["subagent_type"] == "Explore"
        assert conversation.workspace_path == "/w"
        assert assembly.ledger.count(Disposition.UNKNOWN) == 0

    def test_a_human_message_typed_mid_turn_is_named_in_the_ledger(self):
        """The harness records a message the user typed while a turn ran only as a `queued_command` attachment
        (29 in the corpus, none repeated as a user record). It is not stored, as before the port, but the ledger
        names it apart from the harness's own context so the omission is counted, not hidden."""
        assembly = _assembly(_fixture("queued_prompt"))
        reasons = assembly.ledger.reasons
        assert reasons[(Disposition.IGNORED, "attachment:queued_command:human-prompt")] == 1
        assert reasons[(Disposition.IGNORED, "attachment:hook_success")] == 1
        (conversation,) = assembly.conversations
        assert len(conversation.prompts) == 1

    def test_subagent_lineage_names_the_session_it_ran_in(self, tmp_path):
        records = [_user("u1", "T1", "hi", agentId="ag1")]
        conversation = _only(write_jsonl(tmp_path, records, name="agent-ag1.jsonl"))
        assert conversation.external_id == "claude_code::s::agent::ag1"
        assert conversation.parent("subagent").parent == "claude_code::s"
        assert not any(k.startswith("lineage.") for k in conversation.attributes)

    def test_a_later_cwd_or_branch_fills_what_the_first_record_lacked(self, tmp_path):
        first = _user("u1", "T1", "hi")
        first.pop("cwd")
        second = _assistant("a1", "T2", "ok")
        second.update(cwd="/later", gitBranch="topic")
        conversation = _only(write_jsonl(tmp_path, [first, second]))
        assert (conversation.workspace_path, conversation.branch) == ("/later", "topic")


class TestStreamedMessageUsage:
    """Issue #97: Claude Code repeats a message's full usage on every record of it."""

    @staticmethod
    def _chunk(uuid, output, cache_read, message_id="msg_1"):
        """One content-block record of a streamed message (repeats its full usage)."""
        return _assistant(uuid, "T2", "x", message_id, usage={
            "input_tokens": 5, "output_tokens": output, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 7, "cache_creation": {"ephemeral_5m_input_tokens": 7},
            "server_tool_use": {"web_search_requests": 1}})

    def test_streamed_message_usage_counted_once(self, tmp_path):
        """Three records share one message.id and become one response holding the message's final usage; a record
        without message.id is its own message and keeps its usage."""
        records = [
            _user("u1", "T1", "hi"),
            self._chunk("a1", 2, 100),
            self._chunk("a2", 2, 100),
            self._chunk("a3", 176, 100),
            self._chunk("a4", 9, 0, message_id=None),
        ]
        responses = _only(write_jsonl(tmp_path, records)).prompts[0].responses
        assert [r.external_id for r in responses] == ["claude_code::a1", "claude_code::a4"]
        assert sum(r.usage.output_tokens for r in responses) == 185
        assert responses[0].usage.input_tokens == 5
        assert responses[0].attributes["cache_read_input_tokens"] == "100"
        assert responses[0].attributes["cache_creation_ephemeral_5m_input_tokens"] == "7"
        assert responses[0].attributes["server_tool_use_web_search_requests"] == "1"
        assert responses[1].attributes["cache_creation_input_tokens"] == "7"

    def test_role_assistant_record_typed_user_carries_its_usage(self, tmp_path):
        """A Response is decided by message.role; the record type only gates which records are read. A record typed
        user with role assistant is a response, so its usage is its message's, not dropped."""
        records = [_user("u1", "T1", "hi"), {**_assistant("a1", "T2", "x", "msg_x", usage={"input_tokens": 5, "output_tokens": 9}), "type": "user"}]
        (response,) = _only(write_jsonl(tmp_path, records)).prompts[0].responses
        assert response.usage is not None and response.usage.output_tokens == 9

    def test_role_mismatch_peek_matches_parse(self, tmp_path):
        """parse() and peek classify a record by effective_role from one decoder. A record typed user with role
        assistant is a response, so peek must not turn it into a prompt, and a later such record replaces the
        message's usage with its own final counts (#97)."""
        def message(uuid, record_type, role, output_tokens):
            return {**_assistant(uuid, "T2", "x", "msg_x", usage={"input_tokens": 1, "output_tokens": output_tokens}),
                    "type": record_type, "message": {"role": role, "id": "msg_x", "model": "m", "content": "x",
                                                     "usage": {"input_tokens": 1, "output_tokens": output_tokens}}}

        prompt = _user("u1", "T1", "hi")
        scenarios = {
            # A chunk typed assistant, then a final record typed user with role assistant.
            "final_typed_user": ([prompt, message("a1", "assistant", "assistant", 2), message("a2", "user", "assistant", 20)], 1, 20),
            # The same chunk, then a record typed assistant with role user: a prompt, not a response.
            "later_role_user": ([prompt, message("a1", "assistant", "assistant", 2), message("a2", "assistant", "user", 0)], 2, 2),
        }
        for name, (records, prompt_count, output_total) in scenarios.items():
            path = write_jsonl(tmp_path, records, name=f"{name}.jsonl")
            conversation = _only(path)
            parsed_output = sum(r.usage.output_tokens for p in conversation.prompts for r in p.responses if r.usage)
            exchanges = adapt(claude_code).peek_exchanges(path, 5)
            assert len(conversation.prompts) == len(exchanges) == prompt_count, name
            assert parsed_output == sum(x.output_tokens for x in exchanges) == output_total, name

    def test_peek_counts_streamed_message_once(self, tmp_path):
        records = [_user("u1", "T1", "hi"), self._chunk("a1", 2, 100), self._chunk("a2", 2, 100), self._chunk("a3", 176, 100)]
        exchanges = adapt(claude_code).peek_exchanges(write_jsonl(tmp_path, records), 5)
        assert len(exchanges) == 1
        assert exchanges[0].input_tokens == 5 and exchanges[0].output_tokens == 176


class TestPeek:
    def test_injected_records_are_exchanges_as_ingest_stores_them(self, tmp_path):
        """Peek used to classify records a second time and showed an injected user record (a caveat, a skill
        injection) as metadata, so it counted 1,056 fewer exchanges than ingest stored prompts. Peek now reads
        the decoder ingest uses: the injected record is a prompt, with its text under meta_text."""
        records = [
            _user("u1", "T1", "<local-command-caveat>c</local-command-caveat>", isMeta=True),
            _user("u2", "T2", "real", promptSource="typed"),
            _assistant("a1", "T3", "ok"),
        ]
        path = write_jsonl(tmp_path, records)
        scan = adapt(claude_code).peek_scan(path)
        assert scan.exchange_count == len(_only(path).prompts) == 2

    def test_a_session_of_only_injected_prompts_is_visible(self, tmp_path):
        """peek_scan returned None for a file whose prompts were all injected (35 in the real corpus) while ingest
        stored them."""
        path = write_jsonl(tmp_path, [_user("u1", "T1", "inject", isMeta=True), _assistant("a1", "T2", "ok")])
        assert adapt(claude_code).peek_scan(path) is not None

    def test_leading_assistant_is_one_promptless_exchange_and_the_scan_agrees(self):
        path = _fixture("assistant_before_prompt")
        scan = adapt(claude_code).peek_scan(path)
        exchanges = adapt(claude_code).peek_exchanges(path, 10)
        assert scan.exchange_count == len(exchanges) == 2
        assert exchanges[0].prompt_text is None and exchanges[1].prompt_text is not None
