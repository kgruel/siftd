"""Tests for Copilot CLI adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, default_location_source, fixture_source

from siftd.adapters import copilot_cli
from siftd.domain.source import Source


class TestCopilotCliAdapter:

    def test_can_handle(self):
        assert copilot_cli.can_handle(Source(kind="file", location=Path("/mock/.copilot/session-state/uuid/events.jsonl")))
        assert not copilot_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "copilot_cli_minimal.jsonl"))
        assert not copilot_cli.can_handle(Source(kind="directory", location=Path("/mock/.copilot/session-state")))
        assert copilot_cli.can_handle(default_location_source(copilot_cli, "uuid/events.jsonl"))

    def test_parse_full(self, tmp_path):
        copilot_source = fixture_source(tmp_path, "copilot_cli_minimal.jsonl", ".copilot/session-state/test-uuid", "events.jsonl")
        conv = list(copilot_cli.parse(copilot_source))[0]
        assert conv.external_id == "copilot_cli::copilot-session-001" and conv.workspace_path == "/test/workspace"
        assert conv.branch == "main" and conv.harness.name == "copilot_cli" and len(conv.prompts) == 1
        assert "List the files" in conv.prompts[0].content[0].content["text"] and len(conv.prompts[0].responses) == 2
        tc = conv.prompts[0].responses[0].tool_calls[0]
        assert tc.tool_name == "bash" and tc.status == "success" and "README.md" in str(tc.result)
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "thinking"]
        assert conv.prompts[0].responses[0].model == "claude-haiku-4.5"

    def test_parse_empty_file(self, tmp_path):
        d = tmp_path / ".copilot" / "session-state" / "uuid"
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("")
        assert list(copilot_cli.parse(Source(kind="file", location=d / "events.jsonl"))) == []

    def test_normalizer(self):
        n = copilot_cli.normalize_record
        assert n({"type": "session.start", "timestamp": "T", "data": {"sessionId": "s", "context": {"cwd": "/w"}}}).session_id == "s"
        assert n({"type": "session.model_change", "timestamp": "T", "data": {"newModel": "m"}}).model == "m"
        assert n({"type": "user.message", "timestamp": "T", "data": {"content": "hi"}}).content_blocks[0]["text"] == "hi"
        assert n({"type": "user.message", "timestamp": "T", "data": {"content": ""}}).content_blocks == []
        nr = n({"type": "assistant.message", "timestamp": "T", "data": {"content": "ok", "reasoningText": "think", "toolRequests": [{"name": "sh"}]}})
        assert nr.kind == "assistant" and len(nr.content_blocks) == 3 and nr.content_blocks[1]["type"] == "thinking"
        assert n({"type": "tool.execution_complete", "timestamp": "T"}).kind == "tool_result"
        assert n({"type": "unknown"}) is None

    def test_discover_wrapper_and_can_handle_fallback_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(copilot_cli, "discover_files", lambda *a, **k: [Source(kind="file", location=tmp_path / "x")])
        assert list(copilot_cli.discover(locations=[str(tmp_path)]))
        assert not copilot_cli.can_handle(Source(kind="file", location=tmp_path / "events.jsonl"))

    def test_parse_updates_started_at_when_timestamps_unordered(self, tmp_path):
        d = tmp_path / ".copilot" / "session-state" / "uuid"
        d.mkdir(parents=True)
        p = d / "events.jsonl"
        p.write_text("\n".join([
            '{"type":"assistant.message","timestamp":"2024-01-03T00:00:00Z","data":{"content":"later"}}',
            '{"type":"user.message","timestamp":"2024-01-02T00:00:00Z","data":{"content":"earlier"}}'
        ]))
        conv = list(copilot_cli.parse(Source(kind="file", location=p)))[0]
        assert conv.started_at == "2024-01-02T00:00:00Z"

    def test_windows_default_locations_branch_reload(self, monkeypatch):
        import importlib

        import siftd.adapters.copilot_cli as mod

        monkeypatch.setattr(mod.sys, "platform", "win32")
        mod = importlib.reload(mod)
        assert any("AppData/Local/.copilot/session-state" in p for p in mod.DEFAULT_LOCATIONS)
