"""Tests for Antigravity CLI adapter."""

import json
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR

from siftd.adapters import antigravity_cli
from siftd.domain import ToolCall
from siftd.domain.source import Source


@pytest.fixture(autouse=True)
def clear_history_cache():
    """_load_history_workspaces is process-cached; keep tests isolated."""
    antigravity_cli._load_history_workspaces.cache_clear()
    yield
    antigravity_cli._load_history_workspaces.cache_clear()


def _running_step(task_id: str, *, created_at="T2", description="cmd") -> dict:
    """A RUN_COMMAND step backgrounded mid-flight, matching the real shape."""
    return {
        "type": "RUN_COMMAND",
        "status": "RUNNING",
        "created_at": created_at,
        "content": (
            f"Tool is running as a background task with task id: {task_id}\n"
            f"Task Description: {description}\n"
            f"Task logs are available at: file:///nonexistent/{task_id}.log"
        ),
    }


def _finished_message(task_id: str, inline_output: str, *, created_at="T3", log_path: str | None = None) -> dict:
    """A SYSTEM_MESSAGE step reporting a background task's completion."""
    log_line = f"\n\nLog: file://{log_path}" if log_path else ""
    return {
        "type": "SYSTEM_MESSAGE",
        "status": "DONE",
        "created_at": created_at,
        "content": (
            "<SYSTEM_MESSAGE>\n"
            f'[Message] content=Task id "{task_id}" finished with result:\n\n'
            f"{inline_output}{log_line}\n"
            "</SYSTEM_MESSAGE>"
        ),
    }


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

    def test_peek_glob_patterns_exclude_history_and_avoid_duplicates(self, tmp_path):
        """PEEK_GLOB_PATTERNS must not match the top-level history.jsonl, and
        must not surface both transcript.jsonl and transcript_full.jsonl for
        the same conversation (peek has no dedup step, unlike discover())."""
        root = tmp_path / "antigravity-cli"
        _write_transcript(root, "conv-1", [{"type": "USER_INPUT"}], full=True)
        (root / "brain" / "conv-1" / ".system_generated" / "logs" / "transcript.jsonl").write_text("{}\n")
        (root / "history.jsonl").write_text("{}\n")

        matches = {
            match
            for pattern in antigravity_cli.PEEK_GLOB_PATTERNS
            for match in root.glob(pattern)
        }
        assert matches == {root / "brain" / "conv-1" / ".system_generated" / "logs" / "transcript_full.jsonl"}

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

    def test_orphan_multiword_result_step_maps_to_real_tool_name(self, tmp_path):
        """LIST_DIRECTORY.lower() would be 'list_directory', which TOOL_ALIASES
        doesn't recognize -- the orphan fallback must use the real raw name
        ('list_dir') so canonicalization at ingest still applies."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "go"},
                {"type": "LIST_DIRECTORY", "status": "DONE", "created_at": "T2", "content": "a\nb\n"},
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].tool_name == "list_dir"

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


class TestAntigravityCliBackgroundTasks:
    def test_resolves_from_log_file_when_present(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        log_path = root / "brain" / "conv-1" / ".system_generated" / "tasks" / "task-1.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("full untruncated pytest output\n2547 passed\n")

        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "run the tests"},
                {
                    "type": "PLANNER_RESPONSE", "created_at": "T2", "content": "running tests",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"pytest"'}}],
                },
                _running_step("conv-1/task-1"),
                _finished_message(
                    "conv-1/task-1",
                    "The command completed successfully.\nOutput:\n<truncated 1 line>\n2547 passed",
                    log_path=str(log_path),
                ),
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].status == "success"
        assert resp.tool_calls[0].result == {"output": "full untruncated pytest output\n2547 passed\n"}
        assert resp.tool_calls[0].attributes["background_task_id"] == "conv-1/task-1"

    def test_falls_back_to_inline_text_when_log_file_missing(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "run the tests"},
                {
                    "type": "PLANNER_RESPONSE", "created_at": "T2", "content": "running tests",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"pytest"'}}],
                },
                _running_step("conv-1/task-1"),
                _finished_message("conv-1/task-1", "The command completed successfully.\ndone"),
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].status == "success"
        assert resp.tool_calls[0].result == {"output": "The command completed successfully.\ndone"}

    def test_unrelated_system_message_is_a_noop(self, tmp_path):
        """A SYSTEM_MESSAGE that isn't a task-completion notice (e.g. the real
        'subagents stopped due to server restart' notice) must not touch any
        open background task, and must not raise."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "run the tests"},
                {
                    "type": "PLANNER_RESPONSE", "created_at": "T2", "content": "running tests",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"pytest"'}}],
                },
                _running_step("conv-1/task-1"),
                {
                    "type": "SYSTEM_MESSAGE", "status": "DONE", "created_at": "T3",
                    "content": "<SYSTEM_MESSAGE>\nAll subagents stopped due to server restart.\n</SYSTEM_MESSAGE>",
                },
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].status == "pending"
        assert resp.tool_calls[0].result == {"output": _running_step("conv-1/task-1")["content"]}
        # background_task_id is set at RUNNING-registration time, independent
        # of whether resolution ever arrives.
        assert resp.tool_calls[0].attributes["background_task_id"] == "conv-1/task-1"

    def test_completion_for_unknown_task_id_is_a_noop(self, tmp_path):
        """A completion notice for a task id we never saw declared (e.g. it
        started in a part of the log outside this slice) must not crash and
        must not touch an unrelated pending tool call."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "run the tests"},
                {
                    "type": "PLANNER_RESPONSE", "created_at": "T2", "content": "running tests",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"pytest"'}}],
                },
                _running_step("conv-1/task-1"),
                _finished_message("conv-1/some-other-task", "done"),
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        assert resp.tool_calls[0].status == "pending"

    def test_two_background_tasks_resolve_independently_by_id(self, tmp_path):
        """Resolution must key off the task id, not just resolve whichever
        pending background task happens to be most recent."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(
            root,
            "conv-1",
            [
                {"type": "USER_INPUT", "created_at": "T1", "content": "run two things"},
                {
                    "type": "PLANNER_RESPONSE", "created_at": "T2", "content": "starting both",
                    "tool_calls": [
                        {"name": "run_command", "args": {"CommandLine": '"first"'}},
                        {"name": "run_command", "args": {"CommandLine": '"second"'}},
                    ],
                },
                _running_step("conv-1/task-1", created_at="T3"),
                _running_step("conv-1/task-2", created_at="T4"),
                _finished_message("conv-1/task-2", "second done", created_at="T5"),
            ],
        )
        resp = list(antigravity_cli.parse(Source(kind="file", location=path)))[0].prompts[0].responses[0]
        first, second = resp.tool_calls
        assert first.status == "pending"
        assert second.status == "success" and second.result == {"output": "second done"}

    def test_resolve_background_task_helper_directly(self):
        open_tasks = {"t1": ToolCall(tool_name="run_command", input={})}
        antigravity_cli._resolve_background_task("not a completion message", open_tasks)
        assert "t1" in open_tasks  # untouched

        antigravity_cli._resolve_background_task(
            '<SYSTEM_MESSAGE>\ncontent=Task id "t1" finished with result:\n\nok\n</SYSTEM_MESSAGE>',
            open_tasks,
        )
        assert "t1" not in open_tasks


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


# --- Model identity via conversations/<id>.db -------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _len_field(field_no: int, payload: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(payload)) + payload


def _gen_metadata_blob(
    display_name: str | None = "Gemini 3.5 Flash (Medium)",
    map_entries: dict[str, str] | None = None,
) -> bytes:
    """Build a protobuf blob shaped like real gen_metadata.data."""
    sub = b""
    sub += _len_field(19, b"gemini-default")
    for key, value in (map_entries or {}).items():
        entry = _len_field(1, key.encode()) + _len_field(2, value.encode())
        sub += _len_field(20, entry)
    if display_name is not None:
        sub += _len_field(21, display_name.encode())
    return _len_field(1, sub)


def _write_conversations_db(root: Path, conv_id: str, blobs: list[bytes]) -> Path:
    import sqlite3

    conv_dir = root / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    db_path = conv_dir / f"{conv_id}.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
    conn.executemany(
        "INSERT INTO gen_metadata VALUES (?, ?)", list(enumerate(blobs))
    )
    conn.commit()
    conn.close()
    return db_path


_MODEL_RECORDS = [
    {"type": "USER_INPUT", "created_at": "T1", "content": "hi"},
    {"type": "PLANNER_RESPONSE", "created_at": "T2", "content": "hello"},
]


class TestAntigravityCliModelIdentity:
    def _parse_one(self, path):
        (conv,) = antigravity_cli.parse(Source(kind="file", location=path))
        return conv

    def test_model_set_from_sibling_db(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", _MODEL_RECORDS)
        _write_conversations_db(root, "conv-1", [_gen_metadata_blob()] * 3)
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model == "Gemini 3.5 Flash (Medium)"

    def test_model_none_when_db_missing(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", _MODEL_RECORDS)
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model is None

    def test_model_none_when_db_corrupt(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", _MODEL_RECORDS)
        conv_dir = root / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / "conv-1.db").write_bytes(b"not a sqlite database at all")
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model is None

    def test_model_none_when_table_missing(self, tmp_path):
        import sqlite3

        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", _MODEL_RECORDS)
        conv_dir = root / "conversations"
        conv_dir.mkdir(parents=True)
        conn = sqlite3.connect(conv_dir / "conv-1.db")
        conn.execute("CREATE TABLE other (x)")
        conn.commit()
        conn.close()
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model is None

    def test_model_none_when_rows_disagree(self, tmp_path):
        # Per-generation attribution to transcript steps is unverified, so a
        # mixed-model conversation degrades to None rather than guessing.
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", _MODEL_RECORDS)
        _write_conversations_db(
            root,
            "conv-1",
            [_gen_metadata_blob("Model A"), _gen_metadata_blob("Model B")],
        )
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model is None

    def test_model_falls_back_to_model_enum_map(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", _MODEL_RECORDS)
        blob = _gen_metadata_blob(
            display_name=None,
            map_entries={"used_claude": "false", "model_enum": "MODEL_PLACEHOLDER_M20"},
        )
        _write_conversations_db(root, "conv-1", [blob])
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model == "MODEL_PLACEHOLDER_M20"

    def test_malformed_blob_rows_are_skipped(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", _MODEL_RECORDS)
        _write_conversations_db(
            root, "conv-1", [b"\xff\xff\xff", _gen_metadata_blob("Model A")]
        )
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model == "Model A"

    def test_model_set_on_orphan_response_too(self, tmp_path):
        # A result step with no PLANNER_RESPONSE synthesizes a Response; it
        # should carry the model as well.
        root = tmp_path / "antigravity-cli"
        records = [
            {"type": "USER_INPUT", "created_at": "T1", "content": "hi"},
            {"type": "VIEW_FILE", "created_at": "T2", "content": "out", "status": "DONE"},
        ]
        path = _write_transcript(root, "conv-1", records)
        _write_conversations_db(root, "conv-1", [_gen_metadata_blob()])
        conv = self._parse_one(path)
        assert conv.prompts[0].responses[0].model == "Gemini 3.5 Flash (Medium)"
