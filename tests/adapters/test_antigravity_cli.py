"""Tests for Antigravity CLI adapter."""

import json
import re
import sqlite3
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR

from siftd.adapters import antigravity_cli
from siftd.adapters.assembly import assemble
from siftd.adapters.records import Disposition
from siftd.api.database import create_database
from siftd.domain.source import Source
from siftd.storage.sql_helpers import uncached_input_sql
from siftd.storage.sqlite import get_or_create_model, get_or_create_provider, store_conversation
from siftd.storage.usage_rollup import rebuild_rollups

GOLDEN = FIXTURES_DIR / "adapters" / "antigravity_cli"


@pytest.fixture(autouse=True)
def clear_root_caches():
    """The history and summary loaders are process-cached; keep tests isolated."""
    antigravity_cli._load_history_workspaces.cache_clear()
    antigravity_cli._load_summary_workspaces.cache_clear()
    yield
    antigravity_cli._load_history_workspaces.cache_clear()
    antigravity_cli._load_summary_workspaces.cache_clear()


def _step(index: int, step_type: str, at: str = "T", status: str = "DONE", **fields) -> dict:
    """One transcript step, shaped like the real format: every step carries its step_index."""
    return {"step_index": index, "source": "MODEL", "type": step_type, "status": status, "created_at": at, **fields}


def _user(index: int, text: str = "go", at: str = "T1") -> dict:
    return _step(index, "USER_INPUT", at, source="USER_EXPLICIT", content=f"<USER_REQUEST>\n{text}\n</USER_REQUEST>")


def _planner(index: int, *calls: tuple[str, dict], at: str = "T2", **fields) -> dict:
    tool_calls = [{"name": name, "args": args} for name, args in calls]
    return _step(index, "PLANNER_RESPONSE", at, tool_calls=tool_calls, **fields)


def _result(index: int, step_type: str = "VIEW_FILE", content: str = "ok", at: str = "T3", **fields) -> dict:
    return _step(index, step_type, at, content=content, **fields)


def _running_step(task_id: str, *, index: int = 2, created_at="T2", description="cmd") -> dict:
    """A RUN_COMMAND step backgrounded mid-flight, matching the real shape."""
    return _step(
        index,
        "RUN_COMMAND",
        created_at,
        status="RUNNING",
        content=(
            f"Tool is running as a background task with task id: {task_id}\n"
            f"Task Description: {description}\n"
            f"Task logs are available at: file:///nonexistent/{task_id}.log"
        ),
    )


def _finished_message(task_id: str, inline_output: str, *, index: int = 9, created_at="T3", log_path: str | None = None) -> dict:
    """A SYSTEM_MESSAGE step reporting a background task's completion."""
    log_line = f"\n\nLog: file://{log_path}" if log_path else ""
    return _step(
        index,
        "SYSTEM_MESSAGE",
        created_at,
        content=(
            "<SYSTEM_MESSAGE>\n"
            f'[Message] content=Task id "{task_id}" finished with result:\n\n'
            f"{inline_output}{log_line}\n"
            "</SYSTEM_MESSAGE>"
        ),
    )


def _write_transcript(root: Path, conv_id: str, records: list[dict], *, full=True) -> Path:
    """Lay out a real brain/<id>/.system_generated/logs/transcript*.jsonl tree."""
    logs_dir = root / "brain" / conv_id / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    name = "transcript_full.jsonl" if full else "transcript.jsonl"
    path = logs_dir / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _source(path: Path) -> Source:
    return Source(kind="file", location=path)


def _assembly(path: Path):
    """The assembled source and its ledger, as parse() sees them."""
    source = _source(path)
    return assemble(
        source,
        read=antigravity_cli._read_steps,
        decoder=antigravity_cli.AntigravityDecoder,
        harness=antigravity_cli.HARNESS,
        default_session=antigravity_cli._default_session(source),
    )


def _only(path: Path):
    (conversation,) = antigravity_cli.parse(_source(path))
    return conversation


def _responses(conversation):
    return [r for p in conversation.prompts for r in p.responses]


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
        conv = list(antigravity_cli.parse(_source(GOLDEN / "minimal" / "input.jsonl")))[0]
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
        _write_transcript(root, "conv-full", [_user(0)], full=True)
        # A second conversation dir only has the compacted transcript.
        _write_transcript(root, "conv-compact-only", [_user(0)], full=False)
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
        _write_transcript(root, "conv-1", [_user(0)], full=True)
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
        path = _write_transcript(root, "conv-1", [_user(0, "hi"), _planner(1, at="T2")])
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

        conv = _only(path)
        assert conv.workspace_path == "/repo/a"
        assert conv.external_id == "antigravity_cli::conv-1"

    def test_workspace_falls_back_to_the_summaries_first_workspace(self, tmp_path):
        """history.jsonl records only conversations that were submitted from the CLI prompt; the
        summaries database holds every conversation's workspace_uris, whose first entry is the workspace."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [_user(0), _planner(1, at="T2")])
        _write_summaries(root, {"conv-1": ["file:///Users/user/Code/painted", "file:///tmp/scratch"]})
        assert _only(path).workspace_path == "/Users/user/Code/painted"

    def test_history_wins_over_the_summaries_when_both_name_a_workspace(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [_user(0), _planner(1, at="T2")])
        (root / "history.jsonl").write_text(json.dumps({"workspace": "/repo/a", "conversationId": "conv-1"}) + "\n")
        _write_summaries(root, {"conv-1": ["file:///repo/b"]})
        assert _only(path).workspace_path == "/repo/a"

    def test_summaries_entries_that_are_not_file_uris_are_skipped(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [_user(0), _planner(1, at="T2")])
        _write_summaries(root, {"conv-1": [], "conv-2": ["https://example.com/x"]})
        assert _only(path).workspace_path is None

    def test_workspace_none_without_history_file(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [_user(0, "hi", at="T1")])
        assert _only(path).workspace_path is None

    def test_workspace_cache_hit_avoids_second_read(self, tmp_path, monkeypatch):
        root = tmp_path / "antigravity-cli"
        path1 = _write_transcript(root, "conv-1", [_user(0, "a")])
        path2 = _write_transcript(root, "conv-2", [_user(0, "b")])
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

        list(antigravity_cli.parse(_source(path1)))
        list(antigravity_cli.parse(_source(path2)))
        assert read_count["n"] == 1

    def test_session_id_falls_back_to_stem_when_path_too_shallow(self):
        # A bare filename has too few parent components for the real
        # brain/<id>/.system_generated/logs layout; both helpers degrade
        # gracefully instead of raising.
        shallow = Path("transcript_full.jsonl")
        assert antigravity_cli._session_id(shallow) == "transcript_full"
        assert antigravity_cli._resolve_workspace(shallow) is None


def _write_summaries(root: Path, workspaces: dict[str, list[str]]) -> None:
    """A conversation_summaries.db shaped like the real table, with only the columns the adapter reads."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "conversation_summaries.db")
    conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, workspace_uris TEXT)")
    conn.executemany(
        "INSERT INTO conversation_summaries VALUES (?, ?)",
        [(cid, json.dumps(uris)) for cid, uris in workspaces.items()],
    )
    conn.commit()
    conn.close()


class TestDecoding:
    def test_every_step_type_is_named_and_the_table_matches_the_code(self):
        """The decoder's docstring table is the step-type ledger; it must not drift from what the code does."""
        rows = []
        for line in antigravity_cli.AntigravityDecoder.__doc__.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.strip().startswith("| `") and len(cells) == 3:
                rows.append((re.findall(r"`([A-Z_]+)`", cells[0]), cells[1]))
        named = {name for names, _ in rows for name in names}
        assert named == antigravity_cli._KNOWN_STEPS
        # SYSTEM_MESSAGE's row names both a completion and an Ignored outcome, so it is excluded from the
        # rows whose only outcome is a skip.
        ignored = {name for names, becomes in rows if becomes.startswith("Meta (time) + Ignored") for name in names}
        assert ignored - {"SYSTEM_MESSAGE"} == set(antigravity_cli._IGNORED_STEPS)
        decoder = antigravity_cli.AntigravityDecoder(_source(Path("/x/brain/c/.system_generated/logs/transcript_full.jsonl")))
        for name in named:
            unit = _step(1, name, content="x", error="x")
            records = decoder.decode(unit)
            assert records and not any(type(r).__name__ == "Unknown" for r in records), name

    def test_an_unrecognized_step_type_is_unknown_and_counted(self, tmp_path):
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _step(1, "A_NEW_STEP_TYPE", content="x"),
        ])
        with path.open("a") as f:
            f.write("{truncated\n")
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "type:A_NEW_STEP_TYPE")] == 1
        assert ledger.reasons[(Disposition.UNKNOWN, "malformed")] == 1
        assert ledger.disposed == ledger.records

    def test_a_step_without_a_step_index_is_unknown_not_guessed(self, tmp_path):
        """Pairing needs the step's place in the turn; a planner with no index cannot be named, so it is counted."""
        record = _planner(0, ("view_file", {}))
        del record["step_index"]
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [_user(1), record])
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.UNKNOWN, "PLANNER_RESPONSE:no-step_index")] == 1

    @pytest.mark.parametrize("position", ["first", "last"])
    def test_an_unknown_step_bounds_the_conversation_at_each_boundary(self, tmp_path, position):
        """An unfamiliar step states a time, and the span has always covered it: at either end it moves the bound."""
        if position == "first":
            records = [
                _step(0, "A_NEW_STEP_TYPE", at="T0", content="x"),
                _user(1, at="T1"),
                _planner(2, at="T2", content="hi"),
            ]
            expected = ("T0", "T2")
        else:
            records = [
                _user(0, at="T1"),
                _planner(1, at="T2", content="hi"),
                _step(9, "A_NEW_STEP_TYPE", at="T9", content="x"),
            ]
            expected = ("T1", "T9")
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", records)
        conv = _only(path)
        assert (conv.started_at, conv.ended_at) == expected
        assert _assembly(path).ledger.reasons[(Disposition.UNKNOWN, "type:A_NEW_STEP_TYPE")] == 1

    @pytest.mark.parametrize("shape", ["planner", "result"])
    def test_a_step_rejected_for_its_missing_index_still_bounds_the_conversation(self, tmp_path, shape):
        """A step with no index is Unknown, but its time is evidence exactly as an unknown step's is."""
        if shape == "planner":
            record = _planner(9, ("view_file", {}), at="T9")
        else:
            record = _result(9, "VIEW_FILE", at="T9")
        del record["step_index"]
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0, at="T1"),
            _planner(1, at="T2", content="hi"),
            record,
        ])
        conv = _only(path)
        assert (conv.started_at, conv.ended_at) == ("T1", "T9")

    @pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir() if p.is_dir()))
    def test_golden_cases_decode_with_nothing_unknown_or_orphaned(self, case):
        ledger = _assembly(GOLDEN / case / "input.jsonl").ledger
        assert ledger.count(Disposition.UNKNOWN) == 0
        assert ledger.count(Disposition.ORPHAN) == 0

    def test_steps_are_read_in_step_order_not_file_order(self, tmp_path):
        """A turn's parallel results can be written before the turn (29 corpus transcripts). They must pair
        with the turn that declared them, which is step order, not the order of the lines."""
        records = [
            _user(0),
            _result(2, "VIEW_FILE", content="first"),
            _result(3, "VIEW_FILE", content="second"),
            _planner(1, ("view_file", {"AbsolutePath": '"/a"'}), ("view_file", {"AbsolutePath": '"/b"'})),
        ]
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", records)
        conv = _only(path)
        calls = _responses(conv)[0].tool_calls
        assert [c.result for c in calls] == [{"output": "first"}, {"output": "second"}]
        assert [c.external_id for c in calls] == ["1:0", "1:1"]
        assert _assembly(path).ledger.count(Disposition.ORPHAN) == 0

    def test_error_and_reminder_steps_never_consume_a_declaration(self, tmp_path):
        """A declared call whose step never resulted must stay unresolved, not be paired with an error or a
        reminder that happens to come next. This is the defect the real 628f6b61 window shows."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _planner(1, ("write_to_file", {"TargetFile": '"/a.py"'})),
            _step(2, "ERROR_MESSAGE", error="invalid tool call"),
            _step(3, "EPHEMERAL_MESSAGE", content="reminder"),
            _planner(4, ("view_file", {"AbsolutePath": '"/b"'}), at="T4"),
            _result(5, "VIEW_FILE", content="contents of b", at="T5"),
        ])
        first, second = _responses(_only(path))
        assert first.tool_calls[0].tool_name == "write_to_file" and first.tool_calls[0].result is None
        assert first.attributes["error"] == "invalid tool call"
        assert second.tool_calls[0].tool_name == "view_file"
        assert second.tool_calls[0].result == {"output": "contents of b"}
        assert _assembly(path).ledger.count(Disposition.ORPHAN) == 0

    def test_a_result_with_no_declaration_is_an_orphan_not_a_call(self, tmp_path):
        """A result step nothing declared is counted, and is not stored as a tool call under a made-up name."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _result(1, "VIEW_FILE", content="contents"),
            _planner(2, at="T4", content="done"),
        ])
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.ORPHAN, "tool result with no call")] == 1
        assert _responses(_only(path))[0].tool_calls == []

    def test_planner_before_any_user_turn_is_the_preamble_not_a_prompt(self, tmp_path):
        """A response with no user turn before it is stored as the conversation's preamble. No prompt is invented."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _planner(0, at="T1", content="hello"),
            _user(1, "later", at="T2"),
            _planner(2, at="T3", content="reply"),
        ])
        conv = _only(path)
        assert [r.content[0].content["text"] for r in conv.preamble] == ["hello"]
        assert [p.content[0].content["text"] for p in conv.prompts] == ["later"]
        assert len(conv.prompts[0].responses) == 1

    def test_declarations_the_turn_never_resulted_stay_pending_at_the_next_turn(self, tmp_path):
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _planner(1, ("view_file", {}), ("view_file", {})),
            _result(2, "VIEW_FILE", content="only one"),
            _user(3, "next", at="T4"),
            _planner(4, ("grep_search", {}), at="T5"),
            _result(5, "GREP_SEARCH", content="match", at="T6"),
        ])
        first_turn, second_turn = _only(path).prompts
        first_calls = first_turn.responses[0].tool_calls
        assert first_calls[0].result == {"output": "only one"} and first_calls[0].status == "success"
        assert first_calls[1].result is None and first_calls[1].status == "pending"
        assert second_turn.responses[0].tool_calls[0].result == {"output": "match"}

    def test_a_user_turn_ends_the_declarations_before_it(self, tmp_path):
        """A result that follows a user turn cannot answer a call declared before that turn: the call never
        resulted, and the result is an orphan."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _planner(1, ("view_file", {})),
            _user(2, "next", at="T4"),
            _result(3, "VIEW_FILE", content="stray", at="T5"),
            _planner(4, at="T6", content="done"),
        ])
        assert _responses(_only(path))[0].tool_calls[0].result is None
        assert _assembly(path).ledger.reasons[(Disposition.ORPHAN, "tool result with no call")] == 1

    def test_error_message_describes_its_own_turn_not_an_earlier_one(self, tmp_path):
        """An error before the turn's first response has nothing to describe: it is named and skipped, never
        attached to the previous turn's response."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _planner(1, at="T2", content="first turn"),
            _user(2, "second", at="T3"),
            _step(3, "ERROR_MESSAGE", at="T4", error="stream interrupted"),
            _planner(4, at="T5", content="second turn"),
            _step(5, "ERROR_MESSAGE", at="T6", error="retried"),
        ])
        conv = _only(path)
        first, second = conv.prompts
        assert "error" not in first.responses[0].attributes
        assert second.responses[0].attributes["error"] == "retried"
        ledger = _assembly(path).ledger
        assert ledger.reasons[(Disposition.IGNORED, "error-message:before-response")] == 1

    def test_an_error_result_is_an_error_and_keeps_its_message(self, tmp_path):
        """A GENERIC step with status ERROR is a failed call, not one still in flight."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _planner(1, ("view_file", {"AbsolutePath": '"/x"'})),
            _result(2, "GENERIC", status="ERROR", error="permission denied", content=""),
        ])
        (call,) = _responses(_only(path))[0].tool_calls
        assert call.status == "error"
        assert call.attributes == {"error": "permission denied"}

    def test_skipped_steps_still_bound_the_conversation(self, tmp_path):
        """A checkpoint or reminder states a time; the span has always covered it, though nothing is stored."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _step(0, "CONVERSATION_HISTORY", at="T0"),
            _user(1, at="T1"),
            _planner(2, at="T2", content="hi"),
            _step(3, "CHECKPOINT", at="T9", content="summary"),
        ])
        conv = _only(path)
        assert (conv.started_at, conv.ended_at) == ("T0", "T9")

    def test_tool_call_ids_are_step_and_position_and_unique(self, tmp_path):
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _planner(7, ("view_file", {}), ("grep_search", {})),
            _result(8, "VIEW_FILE"),
            _result(9, "GREP_SEARCH"),
        ])
        ids = [c.external_id for c in _responses(_only(path))[0].tool_calls]
        assert ids == ["7:0", "7:1"]


class TestAntigravityCliBackgroundTasks:
    def test_resolves_from_log_file_when_present(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        log_path = root / "brain" / "conv-1" / ".system_generated" / "tasks" / "task-1.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("full untruncated pytest output\n2547 passed\n")

        path = _write_transcript(root, "conv-1", [
            _user(0, "run the tests"),
            _planner(1, ("run_command", {"CommandLine": '"pytest"'}), content="running tests"),
            _running_step("conv-1/task-1"),
            _finished_message(
                "conv-1/task-1",
                "The command completed successfully.\nOutput:\n<truncated 1 line>\n2547 passed",
                log_path=str(log_path),
            ),
        ])
        call = _responses(_only(path))[0].tool_calls[0]
        assert call.status == "success"
        assert call.result == {"output": "full untruncated pytest output\n2547 passed\n"}
        assert call.attributes["background_task_id"] == "conv-1/task-1"

    def test_falls_back_to_inline_text_when_log_file_missing(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [
            _user(0, "run the tests"),
            _planner(1, ("run_command", {"CommandLine": '"pytest"'}), content="running tests"),
            _running_step("conv-1/task-1"),
            _finished_message("conv-1/task-1", "The command completed successfully.\ndone"),
        ])
        call = _responses(_only(path))[0].tool_calls[0]
        assert call.status == "success"
        assert call.result == {"output": "The command completed successfully.\ndone"}

    def test_unrelated_system_message_leaves_the_call_in_flight(self, tmp_path):
        """A SYSTEM_MESSAGE that isn't a task-completion notice (e.g. a notice that subagents stopped) must not
        touch any open background task, and must not raise."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [
            _user(0, "run the tests"),
            _planner(1, ("run_command", {"CommandLine": '"pytest"'}), content="running tests"),
            _running_step("conv-1/task-1"),
            _step(9, "SYSTEM_MESSAGE", at="T3",
                  content="<SYSTEM_MESSAGE>\nAll subagents stopped due to server restart.\n</SYSTEM_MESSAGE>"),
        ])
        call = _responses(_only(path))[0].tool_calls[0]
        assert call.status == "pending"
        assert call.result == {"output": _running_step("conv-1/task-1")["content"]}
        # background_task_id is set at RUNNING-registration time, independent
        # of whether resolution ever arrives.
        assert call.attributes["background_task_id"] == "conv-1/task-1"
        assert _assembly(path).ledger.reasons[(Disposition.IGNORED, "system-message:other")] == 1

    def test_completion_for_an_undeclared_task_is_named_and_leaves_calls_alone(self, tmp_path):
        """A completion notice for a task id this file never declared (it started outside this slice) must not
        touch an unrelated pending call."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [
            _user(0, "run the tests"),
            _planner(1, ("run_command", {"CommandLine": '"pytest"'}), content="running tests"),
            _running_step("conv-1/task-1"),
            _finished_message("conv-1/some-other-task", "done"),
        ])
        assert _responses(_only(path))[0].tool_calls[0].status == "pending"
        assert _assembly(path).ledger.reasons[(Disposition.IGNORED, "system-message:unknown-task")] == 1

    def test_two_background_tasks_resolve_independently_by_id(self, tmp_path):
        """Resolution must key off the task id, not just resolve whichever pending background task happens to be
        most recent."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [
            _user(0, "run two things"),
            _planner(1, ("run_command", {"CommandLine": '"first"'}), ("run_command", {"CommandLine": '"second"'}),
                     content="starting both"),
            _running_step("conv-1/task-1", index=2, created_at="T3"),
            _running_step("conv-1/task-2", index=3, created_at="T4"),
            _finished_message("conv-1/task-2", "second done", index=4, created_at="T5"),
        ])
        first, second = _responses(_only(path))[0].tool_calls
        assert first.status == "pending"
        assert second.status == "success" and second.result == {"output": "second done"}

    def test_the_completion_is_the_calls_time_of_completion(self, tmp_path):
        """A completed call is stamped with its completion, as the assembler does for every result; the
        declaration's time is the response's."""
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [
            _user(0, at="T0"),
            _planner(1, ("run_command", {}), at="T1"),
            _running_step("conv-1/task-1", created_at="T2"),
            _finished_message("conv-1/task-1", "done", created_at="T9"),
        ])
        assert _responses(_only(path))[0].tool_calls[0].timestamp == "T9"

    def test_task_log_helper_reads_or_gives_none(self, tmp_path):
        log = tmp_path / "task.log"
        log.write_text("full output")
        assert antigravity_cli._task_log(str(log)) == "full output"
        assert antigravity_cli._task_log(str(tmp_path / "missing.log")) is None


class TestAntigravityCliPeek:
    def test_peek_scan_and_exchanges_and_tail(self):
        path = GOLDEN / "minimal" / "input.jsonl"
        scan = antigravity_cli.peek_scan(path)
        assert scan.exchange_count == 1
        exchanges = antigravity_cli.peek_exchanges(path, last_n=5)
        assert exchanges and exchanges[0].prompt_text == "What files are in the repo root?"
        assert exchanges[0].tool_calls
        assert list(antigravity_cli.peek_tail(path, 3))

    def test_a_text_and_tool_response_shows_each_tool_once(self, tmp_path):
        """The tool_use block stands for its call where the model placed it; the call is not shown again after the text."""
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", [
            _user(0),
            _planner(1, ("view_file", {}), ("run_command", {}), content="Checking the repo."),
            _result(2, "VIEW_FILE"),
            _result(3, "RUN_COMMAND"),
        ])
        (exchange,) = antigravity_cli.peek_exchanges(path, last_n=10)
        assert exchange.response_text == "Checking the repo.\n[tool: view_file]\n[tool: run_command]"

    def test_peek_scan_empty_file(self, tmp_path):
        empty = tmp_path / "transcript_full.jsonl"
        empty.write_text("")
        assert antigravity_cli.peek_scan(empty) is None
        assert not list(antigravity_cli.peek_tail(empty, 5))

    @pytest.mark.parametrize("case", ["file_order", "error_not_a_result", "background_task", "no_user_turn"])
    def test_peek_counts_what_parse_stores(self, case):
        """Peek and ingest classify each step once, so a preamble is an exchange and a call is a tool call."""
        path = GOLDEN / case / "input.jsonl"
        conv = _only(path)
        exchanges = antigravity_cli.peek_exchanges(path, last_n=10_000)
        assert len(exchanges) == len(conv.prompts) + (1 if conv.preamble else 0)
        # Peek tallies calls as (canonical name, count) pairs.
        peeked_calls = sum(count for e in exchanges for _, count in e.tool_calls)
        assert peeked_calls == sum(len(r.tool_calls) for r in _responses(conv) + list(conv.preamble))


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
    _user(0, "hi", at="T1"),
    _planner(1, at="T2", content="hello"),
]


class TestAntigravityCliModelIdentity:
    def _parse_one(self, path):
        (conv,) = antigravity_cli.parse(_source(path))
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

    def test_model_set_on_the_preamble_response_too(self, tmp_path):
        root = tmp_path / "antigravity-cli"
        path = _write_transcript(root, "conv-1", [_planner(0, at="T1", content="hello"), _user(1, at="T2")])
        _write_conversations_db(root, "conv-1", [_gen_metadata_blob()])
        conv = self._parse_one(path)
        assert conv.preamble[0].model == "Gemini 3.5 Flash (Medium)"


# --- Token usage (PLANNER_RESPONSE token fields) ----------------------------

_TOKEN_FIXTURE = GOLDEN / "tokens" / "input.jsonl"


def _native_planner_tokens(path: Path) -> tuple[int, int, int]:
    """Raw-JSON sums of the native PLANNER token fields: what the adapter must conserve."""
    native_input = cache_read = output = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "PLANNER_RESPONSE" and "input_tokens" in record:
            native_input += record["input_tokens"]
            cache_read += record.get("cache_read_tokens") or 0
            output += record.get("output_tokens") or 0
    return native_input, cache_read, output


def _parse_responses(path: Path):
    (conv,) = antigravity_cli.parse(_source(path))
    return conv, [r for p in conv.prompts for r in p.responses]


class TestAntigravityCliTokenUsage:
    def test_planner_tokens_attach_to_the_response_that_step_creates(self):
        _, responses = _parse_responses(_TOKEN_FIXTURE)
        # One Response per PLANNER_RESPONSE step; the result steps between them add none.
        assert [r.usage.output_tokens for r in responses] == [122, 122, 222, 107]
        # Stored input is the inclusive total: native 2110 + cache_read 16282.
        assert responses[1].usage.input_tokens == 2110 + 16282
        assert responses[1].attributes["cache_read_input_tokens"] == "16282"

    def test_a_turn_with_no_cache_read_still_states_zero(self):
        """The cache attribute is written for every token-bearing turn, zero included, as it always was."""
        _, responses = _parse_responses(_TOKEN_FIXTURE)
        assert responses[0].attributes["cache_read_input_tokens"] == "0"

    def test_parsed_usage_conserves_native_planner_tokens(self):
        native_input, cache_read, output = _native_planner_tokens(_TOKEN_FIXTURE)
        _, responses = _parse_responses(_TOKEN_FIXTURE)
        assert sum(r.usage.input_tokens for r in responses) == native_input + cache_read
        assert sum(int(r.attributes["cache_read_input_tokens"]) for r in responses) == cache_read
        assert sum(r.usage.output_tokens for r in responses) == output

    @pytest.mark.parametrize("native_input,cache_read", [(2110, 16282), (22630, 20381)])
    def test_stored_convention_recovers_native_input_in_cost_sql(self, native_input, cache_read):
        # google-source rows take uncached_input_sql's default branch (input - cache_read).
        # Storing the native input there would subtract cache_read a second time.
        conn = sqlite3.connect(":memory:")
        try:
            (uncached,) = conn.execute(
                f"SELECT {uncached_input_sql('r')} FROM "
                "(SELECT ? AS input_tokens, ? AS cache_read, 'google' AS source) r",
                (native_input + cache_read, cache_read),
            ).fetchone()
        finally:
            conn.close()
        assert uncached == native_input

    def test_cost_path_bills_native_input_and_cache_read_separately(self, tmp_path):
        conv, responses = _parse_responses(_TOKEN_FIXTURE)
        for response in responses:
            response.model = "gemini-3.1-pro"  # canonical name, so a pricing row can bill it
        conn = create_database(tmp_path / "usage.db")
        try:
            model_id = get_or_create_model(conn, "gemini-3.1-pro")
            provider_id = get_or_create_provider(conn, "google")
            conn.execute(
                "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
                "VALUES ('pr-test', ?, ?, 2.0, 12.0)",
                (model_id, provider_id),
            )
            store_conversation(conn, conv, commit=True)
            rebuild_rollups(conn)
            conn.commit()
            row = conn.execute(
                "SELECT input_tokens, output_tokens, cache_read_tokens, cost FROM usage_by_conv_model"
            ).fetchone()
        finally:
            conn.close()
        native_input, cache_read, output = _native_planner_tokens(_TOKEN_FIXTURE)
        assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (
            native_input + cache_read,
            output,
            cache_read,
        )
        # Uncached input at the input rate, cache read at the 0.1x default, output at the output rate.
        expected = (native_input * 2.0 + cache_read * 2.0 * 0.1 + output * 12.0) / 1_000_000
        assert row["cost"] == pytest.approx(expected)

    def test_planner_without_token_fields_has_no_usage(self, tmp_path):
        records = [
            _user(0, "hi", at="T1"),
            _planner(1, at="T2", content="hello"),
        ]
        path = _write_transcript(tmp_path / "antigravity-cli", "conv-1", records)
        _, responses = _parse_responses(path)
        assert responses[0].usage is None
        assert "cache_read_input_tokens" not in responses[0].attributes


def test_the_error_and_reminder_fixture_pairs_as_the_real_session_did(tmp_path):
    """The real window: the declaration at step 45 never resulted, and the ERROR_MESSAGE that followed it is its
    turn's error, not its result. The next declaration (step 47) pairs with the CODE_ACTION at step 48."""
    conv = _only(GOLDEN / "error_not_a_result" / "input.jsonl")
    planner_45, planner_47 = conv.preamble[1], conv.preamble[2]
    assert planner_45.tool_calls[0].result is None
    assert "invalid tool call" in planner_45.attributes["error"]
    assert planner_47.tool_calls[0].result["output"].startswith("Created At:")
