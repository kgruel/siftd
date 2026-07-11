"""Tests for Antigravity CLI adapter."""

import json
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR

from siftd.adapters import antigravity_cli
from siftd.domain.source import Source


@pytest.fixture(autouse=True)
def clear_history_cache():
    """_load_history_workspaces is process-cached; keep tests isolated."""
    antigravity_cli._load_history_workspaces.cache_clear()
    yield
    antigravity_cli._load_history_workspaces.cache_clear()


def _write_transcript(root: Path, conv_id: str, records: list[dict], *, full=True) -> Path:
    """Lay out a real brain/<id>/.system_generated/logs/transcript*.jsonl tree."""
    logs_dir = root / "brain" / conv_id / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    name = "transcript_full.jsonl" if full else "transcript.jsonl"
    path = logs_dir / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


class TestAntigravityCliAdapter:
    def test_can_handle(self):
        assert antigravity_cli.can_handle(
            Source(
                kind="file",
                location=Path("/mock/brain/x/.system_generated/logs/transcript_full.jsonl"),
            )
        )
        assert antigravity_cli.can_handle(
            Source(
                kind="file",
                location=Path("/mock/brain/x/.system_generated/logs/transcript.jsonl"),
            )
        )
        assert not antigravity_cli.can_handle(
            Source(kind="file", location=Path("/mock/brain/x/logs/transcript_full.jsonl"))
        )
        assert not antigravity_cli.can_handle(
            Source(kind="file", location=Path("/mock/brain/x/.system_generated/logs/other.jsonl"))
        )
        assert not antigravity_cli.can_handle(Source(kind="sqlite", location=Path("/mock/x.jsonl")))

    def test_parse_full(self):
        source = Source(
            kind="file",
            location=FIXTURES_DIR / "adapters" / "antigravity_cli" / "minimal" / "input.jsonl",
        )
        conv = list(antigravity_cli.parse(source))[0]
        assert conv.harness.name == "antigravity_cli" and conv.harness.source == "google"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert prompt.content[0].content["text"] == "What files are in the repo root?"
        assert len(prompt.responses) == 2
        first = prompt.responses[0]
        assert first.tool_calls[0].tool_name == "list_dir"
        assert first.tool_calls[0].input == {"DirectoryPath": "/repo", "Recursive": False}
        assert first.tool_calls[0].status == "success"
        assert first.tool_calls[0].result == {"output": "README.md\nsrc/\n"}
        thinking = [b for b in first.content if b.block_type == "thinking"]
        assert thinking and "directory listing" in thinking[0].content["text"]
        assert prompt.responses[1].content[0].content["text"] == "The repo root contains README.md and src/."


class TestAntigravityCliDiscoverAndWorkspace:
    def test_discover_prefers_full_falls_back_to_compact(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        _write_transcript(root, "conv-full", [{"type": "USER_INPUT"}], full=True)
        # A second conversation dir only has the compacted transcript.
        _write_transcript(root, "conv-compact-only", [{"type": "USER_INPUT"}], full=False)
        # Give conv-full a stray compact file too -- full must still win.
        (root / "brain" / "conv-full" / ".system_generated" / "logs" / "transcript.jsonl").write_text("{}\n")

        sources = list(antigravity_cli.discover(locations=[str(root)]))
        names = sorted(Path(s.location).parent.parent.parent.name for s in sources)
        assert names == ["conv-compact-only", "conv-full"]
        by_conv = {Path(s.location).parents[2].name: Path(s.location).name for s in sources}
        assert by_conv["conv-full"] == "transcript_full.jsonl"
        assert by_conv["conv-compact-only"] == "transcript.jsonl"

    def test_discover_missing_locations(self, tmp_path):
        assert list(antigravity_cli.discover(locations=[str(tmp_path / "nope")])) == []

    def test_workspace_resolved_via_sibling_history(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "<USER_REQUEST>\nhi\n</USER_REQUEST>"},
                {"type": "PLANNER_RESPONSE", "created_at": "T2", "content": "hello"},
            ],
        )
        (root / "history.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"display": "hi", "workspace": "/repo/a", "conversationId": "conv-1"},
                    {"display": "other", "workspace": "/repo/b", "conversationId": "conv-2"},
                ]
            )
            + "\n"
        )

        conv = list(antigravity_cli.parse(Source(kind="file", location=path)))[0]
        assert conv.workspace_path == "/repo/a"
        assert conv.external_id == "antigravity_cli::conv-1"

    def test_workspace_none_without_history_file(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root, "conv-1", [{"type": "USER_INPUT", "created_at": "T1", "content": "hi"}]
        )
        conv = list(antigravity_cli.parse(Source(kind="file", location=path)))[0]
        assert conv.workspace_path is None

    def test_workspace_cache_hit_avoids_second_read(self, tmp_path, monkeypatch):
        root = tmp_path / "antigravity-cli"
        path1 = _write_transcript(root, "conv-1", [{"type": "USER_INPUT", "created_at": "T1", "content": "a"}])
        path2 = _write_transcript(root, "conv-2", [{"type": "USER_INPUT", "created_at": "T1", "content": "b"}])
        (root / "history.jsonl").write_text(
            json.dumps({"workspace": "/repo", "conversationId": "conv-1"}) + "\n"
        )

        read_count = {"n": 0}
        orig_load_jsonl = antigravity_cli.load_jsonl

        def counting_load_jsonl(p):
            if p.name == "history.jsonl":
                read_count["n"] += 1
            return orig_load_jsonl(p)

        monkeypatch.setattr(antigravity_cli, "load_jsonl", counting_load_jsonl)

        list(antigravity_cli.parse(Source(kind="file", location=path1)))
        list(antigravity_cli.parse(Source(kind="file", location=path2)))
        assert read_count["n"] == 1

    def test_session_id_falls_back_to_stem_when_path_too_shallow(self):
        # A bare filename has too few parent components for the real
        # brain/<id>/.system_generated/logs layout; both helpers degrade
        # gracefully instead of raising.
        shallow = Path("transcript_full.jsonl")
        assert antigravity_cli._session_id(shallow) == "transcript_full"
        assert antigravity_cli._resolve_workspace(shallow) is None


class TestAntigravityCliParseEdgeCases:
    def test_empty_file_yields_nothing(self, tmp_path):
        empty = tmp_path / "transcript_full.jsonl"
        empty.write_text("")
        assert list(antigravity_cli.parse(Source(kind="file", location=empty))) == []

    def test_dangling_tool_call_never_resolved_flushes_as_pending(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "<USER_REQUEST>\ngo\n</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "T2",
                    "content": "running a background command",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"sleep 100"'}}],
                },
                # Transcript ends before the background command resolves.
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].tool_name == "run_command"
        assert resp.tool_calls[0].status == "pending"
        assert resp.tool_calls[0].result is None

    def test_running_result_step_maps_to_pending(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "go"},
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "T2",
                    "content": "running tests",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"pytest"'}}],
                },
                {
                    "type": "RUN_COMMAND",
                    "status": "RUNNING",
                    "created_at": "T3",
                    "content": "Tool is running as a background task",
                },
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].status == "pending"
        assert resp.tool_calls[0].result == {"output": "Tool is running as a background task"}

    def test_orphan_result_step_with_no_declared_call(self, tmp_path):
        """A result-typed step with nothing queued (e.g. a truncated log) isn't dropped."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "go"},
                {"type": "VIEW_FILE", "status": "DONE", "created_at": "T2", "content": "file contents"},
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].tool_name == "view_file"
        assert resp.tool_calls[0].result == {"output": "file contents"}
        assert resp.tool_calls[0].status == "success"

    def test_planner_response_before_any_user_input(self, tmp_path):
        """A PLANNER_RESPONSE with no preceding USER_INPUT gets a synthetic prompt."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [{"type": "PLANNER_RESPONSE", "created_at": "T1", "content": "hello"}],
        )
        conv = list(antigravity_cli.parse(Source(kind="file", location=path)))[0]
        assert len(conv.prompts) == 1
        assert conv.prompts[0].content == []

    def test_extract_user_text_falls_back_to_raw_when_unwrapped(self):
        assert antigravity_cli._extract_user_text("Continue") == "Continue"
        assert antigravity_cli._extract_user_text(None) == "None"

    def test_parse_tool_args_unwraps_and_falls_back(self):
        args = antigravity_cli._parse_tool_args({
            "Path": '"/a/b"',
            "Count": "3",
            "Flag": "true",
            "List": '["a", "b"]',
            "NotJson": "not valid json {{{",
            "AlreadyNative": 5,
        })
        assert args == {
            "Path": "/a/b",
            "Count": 3,
            "Flag": True,
            "List": ["a", "b"],
            "NotJson": "not valid json {{{",
            "AlreadyNative": 5,
        }


class TestAntigravityCliNormalizerAndPeek:
    def test_normalizer_user_and_assistant(self):
        n = antigravity_cli.normalize_record
        u = n({"type": "USER_INPUT", "created_at": "T", "content": "<USER_REQUEST>\nhi\n</USER_REQUEST>"})
        assert u.kind == "user" and u.content_blocks[0]["text"] == "hi"

        a = n({
            "type": "PLANNER_RESPONSE", "created_at": "T", "content": "ok", "thinking": "planning",
            "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": '"/x"'}}],
        })
        assert a.kind == "assistant"
        types = [b["type"] for b in a.content_blocks]
        assert types == ["thinking", "text", "tool_use"]
        assert a.content_blocks[2]["input"] == {"AbsolutePath": "/x"}

    def test_normalizer_skips_scaffolding_and_results(self):
        n = antigravity_cli.normalize_record
        for step_type in ("CHECKPOINT", "CONVERSATION_HISTORY", "SYSTEM_MESSAGE", "VIEW_FILE", "RUN_COMMAND"):
            assert n({"type": step_type, "content": "x"}) is None

    def test_normalizer_metadata(self):
        n = antigravity_cli.normalize_record
        m = n({"_kind": "metadata", "session_id": "conv-1", "workspace_path": "/repo"})
        assert m.kind == "metadata" and m.session_id == "conv-1" and m.workspace_path == "/repo"

    def test_peek_scan_and_exchanges_and_tail(self):
        path = FIXTURES_DIR / "adapters" / "antigravity_cli" / "minimal" / "input.jsonl"
        scan = antigravity_cli.peek_scan(path)
        assert scan.exchange_count == 1
        exchanges = antigravity_cli.peek_exchanges(path, last_n=5)
        assert exchanges and exchanges[0].prompt_text == "What files are in the repo root?"
        assert exchanges[0].tool_calls
        assert list(antigravity_cli.peek_tail(path, lines=3))

    def test_peek_scan_empty_file(self, tmp_path):
        empty = tmp_path / "transcript_full.jsonl"
        empty.write_text("")
        assert antigravity_cli.peek_scan(empty) is None
        assert not list(antigravity_cli.peek_tail(empty, lines=5))
