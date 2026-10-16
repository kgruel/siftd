"""The ledger at ingest: counts reach IngestStats, and `siftd ingest` says so only when nonzero.

An adapter that decodes gives every record a disposition. Ingest obtains that
ledger from the parse it already does and surfaces what the decoder did not
recognize (UNKNOWN), could not place (ORPHAN) and skipped on purpose (IGNORED).
"""

import json
from pathlib import Path

import pytest
from conftest import FIXTURES_DIR

from siftd.adapters import claude_code
from siftd.adapters.contract import adapt
from siftd.adapters.registry import wrap_adapter_paths
from siftd.cli import main
from siftd.ingestion import IngestEvent, ingest_all
from siftd.storage.sqlite import create_database

CLEAN = (FIXTURES_DIR / "claude_code_minimal.jsonl").read_text()
# A record type no decoder table names, a result whose call never appears, and a
# record type the decoder deliberately skips.
DRIFTED = CLEAN + "".join(
    json.dumps(record) + "\n"
    for record in [
        {"type": "a-record-type-from-a-newer-release", "sessionId": "test-session-1"},
        {"type": "file-history-snapshot", "sessionId": "test-session-1"},
        {
            "type": "user", "sessionId": "test-session-1", "timestamp": "2024-01-15T10:00:09Z", "uuid": "msg-009",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "never-called", "content": "x"}]},
        },
    ]
)


def _projects(tmp_path: Path, text: str, name: str = "conversation.jsonl") -> Path:
    projects = tmp_path / "projects"
    (projects / "session").mkdir(parents=True, exist_ok=True)
    (projects / "session" / name).write_text(text)
    return projects


def _ingest_stats(tmp_path: Path, projects: Path, on_event=None):
    adapter = wrap_adapter_paths(adapt(claude_code), [str(projects)])
    return ingest_all(create_database(tmp_path / "ledger.db"), [adapter], on_event=on_event)


class TestStats:
    def test_unknown_and_orphan_records_are_counted_per_harness(self, tmp_path):
        stats = _ingest_stats(tmp_path, _projects(tmp_path, DRIFTED))
        counts = stats.by_harness["claude_code"]
        assert counts["unknown"] == 1
        assert counts["orphan"] == 1
        assert counts["ignored"] == 1

    def test_a_clean_source_counts_no_unknown_or_orphan(self, tmp_path):
        counts = _ingest_stats(tmp_path, _projects(tmp_path, CLEAN)).by_harness["claude_code"]
        assert (counts["unknown"], counts["orphan"], counts["ignored"]) == (0, 0, 0)

    def test_counts_sum_over_the_sources_parsed(self, tmp_path):
        projects = _projects(tmp_path, DRIFTED, "a.jsonl")
        (projects / "session" / "b.jsonl").write_text(DRIFTED.replace("test-session-1", "test-session-2"))
        counts = _ingest_stats(tmp_path, projects).by_harness["claude_code"]
        assert counts["unknown"] == 2 and counts["orphan"] == 2

    def test_a_path_override_around_a_bare_module_ingests_and_counts(self, tmp_path):
        """wrap_adapter_paths(module, paths) is the supported composition; the override must not hide what the framework derives."""
        projects = _projects(tmp_path, DRIFTED)
        adapter = wrap_adapter_paths(claude_code, [str(projects)])
        stats = ingest_all(create_database(tmp_path / "bare.db"), [adapter])
        assert stats.conversations == 1 and stats.files_ingested == 1 and not stats.files_errored
        assert stats.by_harness["claude_code"]["unknown"] == 1

    def test_one_parse_counts_a_source_once(self, tmp_path):
        """A re-ingest of a changed file replaces the stored conversation; the new parse is the one counted."""
        projects = _projects(tmp_path, CLEAN)
        db = create_database(tmp_path / "twice.db")
        adapter = wrap_adapter_paths(adapt(claude_code), [str(projects)])
        ingest_all(db, [adapter])
        (projects / "session" / "conversation.jsonl").write_text(DRIFTED)
        counts = ingest_all(db, [adapter]).by_harness["claude_code"]
        assert counts["unknown"] == 1 and counts["orphan"] == 1

    def test_a_reparse_counts_each_source_once(self, tmp_path):
        """--reparse parses before deciding to replace and hands that parse on; it is the one counted."""
        projects = _projects(tmp_path, DRIFTED)
        db = create_database(tmp_path / "reparse.db")
        adapter = wrap_adapter_paths(adapt(claude_code), [str(projects)])
        ingest_all(db, [adapter])
        stats = ingest_all(db, [adapter], reparse=True)
        assert stats.reparsed == 1
        counts = stats.by_harness["claude_code"]
        assert (counts["unknown"], counts["orphan"]) == (1, 1)

    def test_ingest_assembles_each_source_once_and_never_parses_it_separately(self, tmp_path, monkeypatch):
        """The ledger comes from the parse that is stored: one assemble per source, no second read."""
        from siftd.adapters.contract import DecodingAdapter

        calls = []
        real = DecodingAdapter.assemble
        monkeypatch.setattr(DecodingAdapter, "assemble", lambda self, source, **kw: calls.append(source) or real(self, source, **kw))
        monkeypatch.setattr(DecodingAdapter, "parse", lambda self, source: pytest.fail("ingest must not parse separately"))
        projects = _projects(tmp_path, DRIFTED, "a.jsonl")
        (projects / "session" / "b.jsonl").write_text(CLEAN.replace("test-session-1", "test-session-2"))
        _ingest_stats(tmp_path, projects)
        assert sorted(Path(s.location).name for s in calls) == ["a.jsonl", "b.jsonl"]

    def test_a_file_that_is_not_parsed_this_run_adds_nothing(self, tmp_path):
        projects = _projects(tmp_path, DRIFTED)
        db = create_database(tmp_path / "again.db")
        adapter = wrap_adapter_paths(adapt(claude_code), [str(projects)])
        ingest_all(db, [adapter])
        counts = ingest_all(db, [adapter]).by_harness["claude_code"]
        assert counts["unknown"] == 0 and counts["orphan"] == 0

    def test_the_event_for_a_source_carries_that_sources_counts(self, tmp_path):
        projects = _projects(tmp_path, DRIFTED, "drifted.jsonl")
        (projects / "session" / "clean.jsonl").write_text(CLEAN.replace("test-session-1", "test-session-2"))
        events: list[IngestEvent] = []
        _ingest_stats(tmp_path, projects, on_event=events.append)
        by_name = {Path(e.path).name: e for e in events}
        assert (by_name["drifted.jsonl"].unknown, by_name["drifted.jsonl"].orphan) == (1, 1)
        assert (by_name["clean.jsonl"].unknown, by_name["clean.jsonl"].orphan) == (0, 0)


def _run_ingest(tmp_path, projects, capsys, *extra):
    rc = main(["--db", str(tmp_path / "cli.db"), "ingest", *extra, "--adapter", "claude_code", "--path", str(projects)])
    assert rc == 0
    return capsys.readouterr()


class TestCli:
    def test_the_line_names_the_adapter_and_the_counts(self, tmp_path, capsys):
        captured = _run_ingest(tmp_path, _projects(tmp_path, DRIFTED), capsys)
        text = captured.out + captured.err
        assert "claude_code" in text and "1 record(s) unrecognized" in text and "1 orphaned" in text

    def test_no_line_when_nothing_is_unrecognized_or_orphaned(self, tmp_path, capsys):
        captured = _run_ingest(tmp_path, _projects(tmp_path, CLEAN), capsys)
        for word in ("unrecognized", "orphaned", "sources read this run"):
            assert word not in captured.out + captured.err

    def test_only_an_orphan_is_named_without_an_unrecognized_clause(self, tmp_path, capsys):
        only_orphan = CLEAN + json.dumps({
            "type": "user", "sessionId": "test-session-1", "timestamp": "2024-01-15T10:00:09Z", "uuid": "m9",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "never", "content": "x"}]},
        }) + "\n"
        captured = _run_ingest(tmp_path, _projects(tmp_path, only_orphan), capsys)
        text = captured.out + captured.err
        assert "1 orphaned" in text and "unrecognized" not in text

    def test_quiet_suppresses_the_line(self, tmp_path, capsys):
        captured = _run_ingest(tmp_path, _projects(tmp_path, DRIFTED), capsys, "--quiet")
        assert "unrecognized" not in captured.out + captured.err

    def test_json_carries_the_counts_in_the_summary_and_per_file(self, tmp_path, capsys):
        captured = _run_ingest(tmp_path, _projects(tmp_path, DRIFTED), capsys, "--json")
        events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
        summary = next(e for e in events if e["type"] == "summary")
        assert summary["by_harness"]["claude_code"]["unknown"] == 1
        assert summary["by_harness"]["claude_code"]["orphan"] == 1
        assert summary["by_harness"]["claude_code"]["ignored"] == 1
        (file_event,) = [e for e in events if e["type"] == "file"]
        assert (file_event["unknown"], file_event["orphan"]) == (1, 1)
        warning = next(e for e in events if e.get("kind") == "unrecognized_records")
        assert warning["adapter"] == "claude_code" and (warning["unknown"], warning["orphan"]) == (1, 1)

    def test_json_emits_no_warning_for_a_clean_run(self, tmp_path, capsys):
        captured = _run_ingest(tmp_path, _projects(tmp_path, CLEAN), capsys, "--json")
        events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
        assert not [e for e in events if e.get("kind") == "unrecognized_records"]
        (file_event,) = [e for e in events if e["type"] == "file"]
        assert (file_event["unknown"], file_event["orphan"]) == (0, 0)
