"""Shared test fixtures for the siftd test suite.

xdist safety note
-----------------
Avoid ``monkeypatch.setattr(sys, "stdout", ...)`` or ``sys.stderr`` in tests.
Under pytest-xdist, workers share the process-level stdio descriptors, so
monkeypatching them races with capture and other workers.

Preferred alternatives:
- Use callback parameters (``on_turn``, ``render``) to collect output in-process.
- Use ``capsys`` / ``capfd`` (pytest-native, xdist-aware).
- For functions that unconditionally write to stdout, pass an explicit ``file=``
  parameter or refactor to accept a writable object.

See: test_peek_follow.py test_follow_session_json_output for the canonical
example of converting from monkeypatch-stdout to callback collection.
"""

import dataclasses
import json
import random
import string
import sys
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Route CLI help snapshots to a per-Python-version subdirectory so that
    # --snapshot-update on one version never deletes another version's snapshots.
    # syrupy reads config.option.snapshot_dirname in pytest_sessionstart and sets
    # SnapshotCollectionStorage.snapshot_dirname from it — this hook runs first.
    ver = f"py{sys.version_info.major}{sys.version_info.minor}"
    config.option.snapshot_dirname = f"__snapshots__/{ver}"


@pytest.fixture(autouse=True)
def _sandbox_db_home(tmp_path, monkeypatch):
    """Hard-isolate every test from the real database.

    `siftd.paths` resolves XDG_DATA_HOME at call time, so a test that opens the
    default db_path() reaches the user's real ~/.local/share/siftd/siftd.db.
    While SCHEMA_VERSION matched that was a harmless no-op open; a version bump
    turns it into a *migration of production data* (the v9 rollup incident).
    Redirecting XDG_DATA_HOME (where the DB lives) to a throwaway dir makes the
    real DB unreachable from any test, regardless of how the path is resolved.
    XDG_CONFIG_HOME and XDG_STATE_HOME are sandboxed for the same reason:
    tests that resolved the user's real ~/.config/siftd/config.toml or
    ~/.local/state/siftd/serve.json asserted against whatever the developer's
    serve/auth settings or last-run serve happened to be (the port-8485
    delegation flakes came from the runtime state-file fallback; two auth
    tests silently depended on the developer's real [auth] table). A test
    that needs specific config/state writes it into the sandbox dirs or
    monkeypatches the loader; none may depend on the real ones.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "_xdg_data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "_xdg_config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "_xdg_state"))
    yield


@pytest.fixture(autouse=True)
def _reset_caveat_producers():
    """Snapshot and restore the caveat producer registry around each test.

    `_producers` is module-scope and mutated at import time by
    `@caveat_producer`. Without this, tests that register their own
    producers leak into siblings and import order becomes load-bearing.
    """
    from siftd.api import caveats as _caveats_mod

    snapshot = list(_caveats_mod._producers)
    yield
    _caveats_mod._producers[:] = snapshot

from siftd.domain.models import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    Usage,
)
from siftd.domain.source import Source
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
    insert_tool_call,
    record_ingested_file,
)
from siftd.storage.tags import apply_tag, get_or_create_tag

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# CWD-relative path so workspace_path and path-hash fields in adapter output
# are stable across machines (some adapters derive IDs from str(path)).
GOLDEN_DIR = Path("tests/fixtures/adapters")


def _golden_cases() -> list[tuple[str, str]]:
    """Discover all (adapter, case) pairs that have an expected.json."""
    if not GOLDEN_DIR.exists():
        return []
    return sorted(
        (case_dir.parent.name, case_dir.name)
        for case_dir in GOLDEN_DIR.glob("*/*")
        if case_dir.is_dir() and (case_dir / "expected.json").exists()
    )


def load_golden_input(adapter: str, case: str, tmp_path=None):
    """Return a Source pointing at the input fixture for (adapter, case).

    If the case directory contains setup.sql, materializes a temporary SQLite
    DB from it and returns Source(kind="sqlite", ...). tmp_path is required for
    the SQL path; raises ValueError if absent.

    Otherwise discovers any file in the case directory that is not expected.json.
    Hidden files (e.g. .aider.chat.history.md) are included.
    Uses a CWD-relative path so adapters that derive IDs from the path
    produce the same output on every machine.
    """
    import sqlite3

    from siftd.domain.source import Source

    case_dir = GOLDEN_DIR / adapter / case
    setup_sql = case_dir / "setup.sql"
    if setup_sql.exists():
        if tmp_path is None:
            raise ValueError(
                f"tmp_path is required to materialize SQL fixture for {adapter}/{case}"
            )
        tmp_db = tmp_path / f"{adapter}_{case}.db"
        conn = sqlite3.connect(str(tmp_db))
        conn.executescript(setup_sql.read_text())
        conn.close()
        return Source(kind="sqlite", location=tmp_db)

    candidates = [
        f for f in case_dir.iterdir()
        if f.is_file() and f.name != "expected.json"
    ]
    if not candidates:
        raise FileNotFoundError(f"No input fixture for {adapter}/{case}")
    return Source(kind="file", location=candidates[0])


def load_golden_expected(adapter: str, case: str) -> list[dict]:
    """Return the expected Conversation list for (adapter, case)."""
    path = GOLDEN_DIR / adapter / case / "expected.json"
    return json.loads(path.read_text())


def assert_golden(adapter_module, adapter: str, case: str, tmp_path=None) -> None:
    """Run adapter.parse() on the golden input and compare to expected.json.

    Comparison is sort_keys=True so dict insertion order never causes diffs.
    tmp_path is required for SQL-backed fixtures (cases with setup.sql).
    """
    source = load_golden_input(adapter, case, tmp_path)
    actual = [dataclasses.asdict(c) for c in adapter_module.parse(source)]
    expected = load_golden_expected(adapter, case)
    assert (
        json.loads(json.dumps(actual, sort_keys=True))
        == json.loads(json.dumps(expected, sort_keys=True))
    )


def fixture_source(tmp_path, fixture, subdir, dest_name=None):
    """Copy a fixture into a subdirectory and return a Source."""
    from siftd.domain.source import Source

    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    dest = d / (dest_name or Path(fixture).name)
    dest.write_text((FIXTURES_DIR / fixture).read_text())
    return Source(kind="file", location=dest)


def default_location_source(adapter_module, filename="test.jsonl"):
    """Create a Source with a path under the adapter's DEFAULT_LOCATIONS.

    Useful for testing can_handle() against the expanded home-directory path
    without needing real files on disk.
    """
    from siftd.domain.source import Source

    loc = str(Path(adapter_module.DEFAULT_LOCATIONS[0]).expanduser())
    return Source(kind="file", location=Path(f"{loc}/{filename}"))


def make_db(
    path,
    *,
    harness_name="test_harness",
    workspace_path="/test/project",
    workspace_git_remote=None,
    model_name="test-model",
    conversations=None,
):
    """Create a database with optional conversations.

    Consolidated factory used by test_receive, test_sync, test_merge, and
    storage tests. Supports the superset of parameters needed by all callers.

    conversations: list of dicts with keys:
        external_id, prompt_text, response_text, started_at (optional),
        tags (optional list of tag names), tool_name (optional)

    Returns the path for chaining.
    """
    conn = create_database(path)

    harness_id = get_or_create_harness(conn, harness_name, source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, workspace_path, "2024-01-01T10:00:00Z")

    if workspace_git_remote:
        conn.execute(
            "UPDATE workspaces SET git_remote = ? WHERE id = ?",
            (workspace_git_remote, workspace_id),
        )

    model_id = get_or_create_model(conn, model_name)
    provider_id = get_or_create_provider(conn, "test_provider")

    for conv in conversations or []:
        started = conv.get("started_at", "2024-01-15T10:00:00Z")
        conv_id = insert_conversation(
            conn,
            external_id=conv["external_id"],
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at=started,
        )
        prompt_id = insert_prompt(conn, conv_id, f"p-{conv['external_id']}", started)
        insert_prompt_content(
            conn, prompt_id, 0, "text",
            f'{{"text": "{conv.get("prompt_text", "Hello")}"}}',
        )
        response_id = insert_response(
            conn, conv_id, prompt_id, model_id, provider_id,
            f"r-{conv['external_id']}", started,
            input_tokens=100, output_tokens=50,
        )
        insert_response_content(
            conn, response_id, 0, "text",
            f'{{"text": "{conv.get("response_text", "Hi there")}"}}',
        )

        if conv.get("tool_name"):
            tool_id = get_or_create_tool(conn, conv["tool_name"])
            insert_tool_call(
                conn, response_id, conv_id, tool_id, f"tc-{conv['external_id']}",
                '{"cmd": "test"}', '{"output": "ok"}', "success", started,
            )

        for tag_name in conv.get("tags", []):
            tag_id = get_or_create_tag(conn, tag_name)
            apply_tag(conn, "conversation", conv_id, tag_id)

    conn.commit()
    conn.close()
    return path


def text_block(text: str) -> str:
    """Create JSON content for a text block."""
    return json.dumps({"text": text})


def make_conversation(
    external_id="test-conv-1",
    workspace_path="/test/project",
    started_at="2024-01-01T10:00:00Z",
    ended_at=None,
    harness_name="test_harness",
    harness_source="test",
    harness_log_format="jsonl",
    model="test-model",
    prompt_text="Hello",
    response_text="Hi there",
    input_tokens=100,
    output_tokens=50,
    tool_calls=None,
    response_attributes=None,
):
    """Build a Conversation domain object for testing.

    Provides sensible defaults for all fields so tests can override only
    what they care about.
    """
    tc_list = tool_calls or []
    return Conversation(
        external_id=external_id,
        workspace_path=workspace_path,
        started_at=started_at,
        ended_at=ended_at,
        harness=Harness(name=harness_name, source=harness_source, log_format=harness_log_format),
        prompts=[
            Prompt(
                external_id="p1",
                timestamp=started_at,
                content=[ContentBlock(block_type="text", content={"text": prompt_text})],
                responses=[
                    Response(
                        external_id="r1",
                        timestamp=started_at,
                        model=model,
                        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
                        content=[ContentBlock(block_type="text", content={"text": response_text})],
                        tool_calls=tc_list,
                        attributes=response_attributes or {},
                    ),
                ],
            ),
        ],
    )


def make_test_adapter(
    dest,
    *,
    name="test_harness",
    dedup="file",
    harness_source="test",
    can_handle_fn=None,
    parse_fn=None,
):
    """Factory for test adapters with configurable dedup strategy and parse function.

    Args:
        dest: Path to the file the adapter will discover
        name: Adapter NAME attribute
        dedup: DEDUP_STRATEGY attribute ('file' or 'session')
        harness_source: HARNESS_SOURCE attribute (e.g., 'test', 'anthropic', 'openai')
        can_handle_fn: Optional custom can_handle(source) function
        parse_fn: Optional custom parse(source) function
    """

    class _Adapter:
        NAME = name
        DEDUP_STRATEGY = dedup
        HARNESS_SOURCE = harness_source

        @staticmethod
        def can_handle(source):
            if can_handle_fn:
                return can_handle_fn(source)
            return True

        @staticmethod
        def parse(source):
            if parse_fn:
                return parse_fn(source)
            return []

        @staticmethod
        def discover():
            yield Source(kind="file", location=dest)

    return _Adapter


def make_session_adapter(dest, *, name="test_harness", dedup="session", parse_fn=None):
    """Factory for test adapters with session-based dedup (convenience wrapper)."""
    return make_test_adapter(dest, name=name, dedup=dedup, parse_fn=parse_fn)


@pytest.fixture
def test_db(tmp_path):
    """Create a test database with standard sample data.

    Contains: 1 harness, 1 workspace, 1 model, 2 conversations with
    prompts, responses, and content.
    """
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "claude-3-opus-20240229")

    conv1_id = insert_conversation(
        conn,
        external_id="conv1",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )
    conv2_id = insert_conversation(
        conn,
        external_id="conv2",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-16T10:00:00Z",
    )

    prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, prompt1_id, 0, "text", '{"text": "Hello, how are you?"}')
    response1_id = insert_response(
        conn, conv1_id, prompt1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50,
    )
    insert_response_content(conn, response1_id, 0, "text", '{"text": "I am doing well, thank you!"}')

    prompt2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
    insert_prompt_content(conn, prompt2_id, 0, "text", '{"text": "What is Python?"}')
    response2_id = insert_response(
        conn, conv2_id, prompt2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
        input_tokens=200, output_tokens=150,
    )
    insert_response_content(conn, response2_id, 0, "text", '{"text": "Python is a programming language."}')

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture
def test_db_with_tool_tags(tmp_path):
    """Create a test database with tool calls and tags.

    Contains: 2 workspaces, 3 conversations, tool calls tagged with
    shell:test and shell:vcs.
    """
    db_path = tmp_path / "test_tools.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    workspace2_id = get_or_create_workspace(conn, "/other/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "claude-3-opus-20240229")
    tool_id = get_or_create_tool(conn, "shell.execute")

    test_tag_id = get_or_create_tag(conn, "shell:test")
    vcs_tag_id = get_or_create_tag(conn, "shell:vcs")

    # Conversation 1 (/test/project) — test command
    conv1_id = insert_conversation(
        conn, external_id="conv1", harness_id=harness_id,
        workspace_id=workspace_id, started_at="2024-01-15T10:00:00Z",
    )
    prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, prompt1_id, 0, "text", '{"text": "Run tests"}')
    response1_id = insert_response(
        conn, conv1_id, prompt1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50,
    )
    tc1_id = insert_tool_call(
        conn, response1_id, conv1_id, tool_id, "tc1",
        '{"command": "pytest"}', '{"output": "OK"}', "success", "2024-01-15T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc1_id, test_tag_id)

    # Conversation 2 (/test/project) — vcs command
    conv2_id = insert_conversation(
        conn, external_id="conv2", harness_id=harness_id,
        workspace_id=workspace_id, started_at="2024-01-16T10:00:00Z",
    )
    prompt2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
    insert_prompt_content(conn, prompt2_id, 0, "text", '{"text": "Commit changes"}')
    response2_id = insert_response(
        conn, conv2_id, prompt2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
        input_tokens=200, output_tokens=150,
    )
    tc2_id = insert_tool_call(
        conn, response2_id, conv2_id, tool_id, "tc2",
        '{"command": "git commit"}', '{"output": "OK"}', "success", "2024-01-16T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc2_id, vcs_tag_id)

    # Conversation 3 (/other/project) — test command
    conv3_id = insert_conversation(
        conn, external_id="conv3", harness_id=harness_id,
        workspace_id=workspace2_id, started_at="2024-01-17T10:00:00Z",
    )
    prompt3_id = insert_prompt(conn, conv3_id, "p3", "2024-01-17T10:00:00Z")
    insert_prompt_content(conn, prompt3_id, 0, "text", '{"text": "Run more tests"}')
    response3_id = insert_response(
        conn, conv3_id, prompt3_id, model_id, None, "r3", "2024-01-17T10:00:01Z",
        input_tokens=150, output_tokens=100,
    )
    tc3_id = insert_tool_call(
        conn, response3_id, conv3_id, tool_id, "tc3",
        '{"command": "pytest -v"}', '{"output": "OK"}', "success", "2024-01-17T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc3_id, test_tag_id)

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture
def semantic_search_db(tmp_path):
    """Create a database with conversations suited for semantic search tests.

    Contains:
    - 1 harness, 1 model
    - 2 workspaces: /projects/python-app, /projects/rust-cli
    - 2 conversations (one per workspace) with semantically distinct content
    - Each conversation has a prompt/response about error handling

    Returns dict with db_path, workspace paths, and conversation IDs.
    """
    db_path = tmp_path / "main.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    model_id = get_or_create_model(conn, "test-model")

    # Workspace 1: Python project
    ws1_id = get_or_create_workspace(conn, "/projects/python-app", "2024-01-01T10:00:00Z")

    conv1_id = insert_conversation(
        conn, external_id="conv-python", harness_id=harness_id,
        workspace_id=ws1_id, started_at="2024-01-15T10:00:00Z",
    )
    p1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, p1_id, 0, "text", '{"text": "How do I handle exceptions in Python?"}')
    r1_id = insert_response(
        conn, conv1_id, p1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=10, output_tokens=100,
    )
    insert_response_content(
        conn, r1_id, 0, "text",
        '{"text": "Use try/except blocks to catch and handle exceptions. You can catch specific exception types."}'
    )

    # Workspace 2: Rust project
    ws2_id = get_or_create_workspace(conn, "/projects/rust-cli", "2024-01-01T10:00:00Z")

    conv2_id = insert_conversation(
        conn, external_id="conv-rust", harness_id=harness_id,
        workspace_id=ws2_id, started_at="2024-01-16T10:00:00Z",
    )
    p2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
    insert_prompt_content(conn, p2_id, 0, "text", '{"text": "How do I handle errors in Rust?"}')
    r2_id = insert_response(
        conn, conv2_id, p2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
        input_tokens=10, output_tokens=100,
    )
    insert_response_content(
        conn, r2_id, 0, "text",
        '{"text": "Use Result<T, E> for recoverable errors. Use the ? operator to propagate errors."}'
    )

    conn.commit()
    conn.close()

    return {
        "db_path": db_path,
        "ws1_path": "/projects/python-app",
        "ws2_path": "/projects/rust-cli",
        "conv1_id": conv1_id,
        "conv2_id": conv2_id,
    }


@pytest.fixture
def test_db_with_ingested_files(tmp_path):
    """Create a test database with conversations linked to ingested files.

    Used by active session exclusion tests.
    """
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "claude_code", source="local", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")

    active_conv_id = insert_conversation(
        conn,
        external_id="active-conv",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )
    insert_prompt(conn, active_conv_id, "p1", "2024-01-15T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/abc/session-active.jsonl",
        "hash_active",
        active_conv_id,
    )

    inactive_conv_id = insert_conversation(
        conn,
        external_id="inactive-conv",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-14T10:00:00Z",
    )
    insert_prompt(conn, inactive_conv_id, "p2", "2024-01-14T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/abc/session-old.jsonl",
        "hash_old",
        inactive_conv_id,
    )

    active2_conv_id = insert_conversation(
        conn,
        external_id="active-conv-2",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-16T10:00:00Z",
    )
    insert_prompt(conn, active2_conv_id, "p3", "2024-01-16T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/xyz/session-active2.jsonl",
        "hash_active2",
        active2_conv_id,
    )

    conn.commit()
    conn.close()

    return {
        "db_path": db_path,
        "active_conv_id": active_conv_id,
        "inactive_conv_id": inactive_conv_id,
        "active2_conv_id": active2_conv_id,
    }


# =============================================================================
# Session file builders — generate valid adapter-format files with random data
#
# Each builder produces a complete session file. Data is randomized by default;
# override only what your test cares about.
#
#   f = ClaudeSession(tmp_path).build()                     # 1-exchange session
#   f = ClaudeSession(tmp_path, exchanges=3).build()        # 3 exchanges
#   f = ClaudeSession(tmp_path).with_tools(["Read"]).build()
#   f = CodexSession(tmp_path).with_tools(["shell"]).with_usage().build()
#   f = PeekSession(tmp_path, exchanges=2).build()          # generic SDK format
# =============================================================================


def _rand_word(n=8):
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _rand_sentence():
    return " ".join(_rand_word(random.randint(3, 10)) for _ in range(random.randint(4, 12)))


def _rand_ts(i):
    return f"2024-01-{1 + i // 24:02d}T{i % 24:02d}:{random.randint(0, 59):02d}:00Z"


class _BaseJSONLSession:
    """Base for JSONL session builders."""

    def __init__(self, tmp_path, *, exchanges=1, name="session.jsonl"):
        self._tmp = tmp_path
        self._exchanges = exchanges
        self._name = name
        self._records = []

    def _write(self):
        f = self._tmp / self._name
        f.write_text("\n".join(json.dumps(r) for r in self._records) + "\n")
        return f


class ClaudeSession(_BaseJSONLSession):
    """Build a Claude Code session JSONL file.

    Usage:
        f = ClaudeSession(tmp_path).build()
        f = ClaudeSession(tmp_path, exchanges=3, model="claude-3.5-sonnet").build()
        f = ClaudeSession(tmp_path).with_tools(["Read", "Bash"]).build()
        f = ClaudeSession(tmp_path).with_subagent("sub-1").build()
    """

    def __init__(self, tmp_path, *, exchanges=1, name="session.jsonl",
                 session_id=None, cwd=None, model=None):
        super().__init__(tmp_path, exchanges=exchanges, name=name)
        self._sid = session_id or f"sess-{_rand_word(6)}"
        self._cwd = cwd or f"/project/{_rand_word(6)}"
        self._model = model or "claude-3-opus-20240229"
        self._tools = []
        self._agent_id = None

    def with_tools(self, tool_names):
        self._tools = tool_names
        return self

    def with_subagent(self, agent_id="sub-1"):
        self._agent_id = agent_id
        return self

    def build(self):
        seq = 0
        for i in range(self._exchanges):
            ts_u, ts_a = _rand_ts(seq), _rand_ts(seq + 1)
            seq += 2
            user = {"type": "user", "sessionId": self._sid, "cwd": self._cwd,
                    "timestamp": ts_u, "uuid": f"u-{i}",
                    "message": {"role": "user", "content": [{"type": "text", "text": _rand_sentence()}]}}
            if self._agent_id:
                user["agentId"] = self._agent_id
            self._records.append(user)

            content = [{"type": "text", "text": _rand_sentence()}]
            for tn in self._tools:
                tid = f"tool-{i}-{tn}"
                content.append({"type": "tool_use", "id": tid, "name": tn,
                                "input": {"path": f"/{_rand_word()}.py"}})
            asst = {"type": "assistant", "sessionId": self._sid, "timestamp": ts_a,
                    "uuid": f"a-{i}",
                    "message": {"role": "assistant", "model": self._model, "content": content,
                                "usage": {"input_tokens": random.randint(50, 500),
                                          "output_tokens": random.randint(10, 200)}}}
            if self._agent_id:
                asst["agentId"] = self._agent_id
            self._records.append(asst)

            for tn in self._tools:
                tid = f"tool-{i}-{tn}"
                self._records.append(
                    {"type": "user", "sessionId": self._sid, "timestamp": _rand_ts(seq),
                     "uuid": f"tr-{i}-{tn}",
                     "message": {"role": "user", "content": [
                         {"type": "tool_result", "tool_use_id": tid, "content": _rand_sentence()}]}})
                seq += 1
        return self._write()


class CodexSession(_BaseJSONLSession):
    """Build a Codex CLI session JSONL file.

    Usage:
        f = CodexSession(tmp_path).build()
        f = CodexSession(tmp_path, exchanges=2).with_tools(["shell"]).with_usage().build()
    """

    def __init__(self, tmp_path, *, exchanges=1, name="session.jsonl",
                 session_id=None, cwd=None, model=None):
        super().__init__(tmp_path, exchanges=exchanges, name=name)
        self._sid = session_id or f"sess-{_rand_word(6)}"
        self._cwd = cwd or f"/project/{_rand_word(6)}"
        self._model = model or "codex-1"
        self._tools = []
        self._custom_tools = []
        self._usage = False

    def with_tools(self, tool_names):
        self._tools = tool_names
        return self

    def with_custom_tools(self, tool_names):
        """Add custom_tool_call / custom_tool_call_output records."""
        self._custom_tools = tool_names
        return self

    def with_usage(self):
        self._usage = True
        return self

    def build(self):
        seq = 0
        self._records.append({"type": "session_meta", "timestamp": _rand_ts(seq),
                               "payload": {"id": self._sid, "cwd": self._cwd}})
        self._records.append({"type": "turn_context", "timestamp": _rand_ts(seq + 1),
                               "payload": {"model": self._model}})
        seq += 2
        for i in range(self._exchanges):
            self._records.append({"type": "response_item", "timestamp": _rand_ts(seq),
                "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": _rand_sentence()}]}})
            self._records.append({"type": "response_item", "timestamp": _rand_ts(seq + 1),
                "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": _rand_sentence()}]}})
            if self._usage:
                self._records.append({"type": "event_msg", "timestamp": _rand_ts(seq + 2),
                    "payload": {"type": "token_count", "info": {"last_token_usage": {
                        "input_tokens": random.randint(50, 500),
                        "output_tokens": random.randint(10, 200)}}}})
            for tn in self._tools:
                cid = f"call-{i}-{tn}"
                self._records.append({"type": "response_item", "timestamp": _rand_ts(seq + 3),
                    "payload": {"type": "function_call", "name": tn, "call_id": cid, "arguments": "{}"}})
                self._records.append({"type": "response_item", "timestamp": _rand_ts(seq + 4),
                    "payload": {"type": "function_call_output", "call_id": cid, "output": _rand_sentence()}})
            for tn in self._custom_tools:
                cid = f"custom-{i}-{tn}"
                self._records.append({"type": "response_item", "timestamp": _rand_ts(seq + 5),
                    "payload": {"type": "custom_tool_call", "name": tn, "call_id": cid,
                                 "input": {"arg": _rand_word()}}})
                self._records.append({"type": "response_item", "timestamp": _rand_ts(seq + 6),
                    "payload": {"type": "custom_tool_call_output", "call_id": cid,
                                 "output": _rand_sentence()}})
            seq += 7
        return self._write()


class PiAgentSession(_BaseJSONLSession):
    """Build a Pi Agent session JSONL file.

    Usage:
        f = PiAgentSession(tmp_path).build()
        f = PiAgentSession(tmp_path, exchanges=2).with_tools(["shell"]).build()
    """

    def __init__(self, tmp_path, *, exchanges=1, name="session.jsonl",
                 session_id=None, cwd=None, model=None):
        super().__init__(tmp_path, exchanges=exchanges, name=name)
        self._sid = session_id or f"sess-{_rand_word(6)}"
        self._cwd = cwd or f"/project/{_rand_word(6)}"
        self._model = model or "claude-3-opus"
        self._tools = []

    def with_tools(self, tool_names):
        self._tools = tool_names
        return self

    def build(self):
        seq = 0
        # Session header
        self._records.append({"type": "session", "id": self._sid, "cwd": self._cwd,
                               "timestamp": _rand_ts(seq)})
        self._records.append({"type": "model_change", "modelId": self._model,
                               "timestamp": _rand_ts(seq + 1)})
        seq += 2
        for i in range(self._exchanges):
            ts_u, ts_a = _rand_ts(seq), _rand_ts(seq + 1)
            seq += 2
            # User message
            self._records.append({"type": "message", "timestamp": ts_u,
                "message": {"role": "user",
                             "content": [{"type": "text", "text": _rand_sentence()}]}})
            # Assistant message with optional tool calls
            content = [{"type": "text", "text": _rand_sentence()}]
            for tn in self._tools:
                cid = f"call-{i}-{tn}"
                content.append({"type": "toolCall", "id": cid, "name": tn,
                                "arguments": {"cmd": _rand_word()}})
            self._records.append({"type": "message", "timestamp": ts_a,
                "message": {"role": "assistant", "model": self._model,
                             "content": content,
                             "usage": {"input": random.randint(50, 500),
                                       "output": random.randint(10, 200)}}})
            # Tool results
            for tn in self._tools:
                cid = f"call-{i}-{tn}"
                self._records.append({"type": "message", "timestamp": _rand_ts(seq),
                    "message": {"role": "toolResult", "toolCallId": cid,
                                 "toolName": tn, "content": [{"type": "text", "text": _rand_sentence()}]}})
                seq += 1
        return self._write()


class CopilotSession(_BaseJSONLSession):
    """Build a Copilot CLI session JSONL file.

    Usage:
        f = CopilotSession(tmp_path).build()
        f = CopilotSession(tmp_path, exchanges=2).with_tools(["run_command"]).build()
    """

    def __init__(self, tmp_path, *, exchanges=1, name="events.jsonl",
                 session_id=None, cwd=None, model=None, branch=None):
        super().__init__(tmp_path, exchanges=exchanges, name=name)
        self._sid = session_id or f"sess-{_rand_word(6)}"
        self._cwd = cwd or f"/project/{_rand_word(6)}"
        self._model = model or "gpt-4o"
        self._branch = branch or "main"
        self._tools = []

    def with_tools(self, tool_names):
        self._tools = tool_names
        return self

    def build(self):
        seq = 0
        # Session start
        self._records.append({"type": "session.start", "timestamp": _rand_ts(seq),
            "data": {"sessionId": self._sid,
                      "context": {"cwd": self._cwd, "branch": self._branch}}})
        self._records.append({"type": "session.model_change", "timestamp": _rand_ts(seq + 1),
            "data": {"newModel": self._model}})
        seq += 2
        for i in range(self._exchanges):
            ts_u, ts_a = _rand_ts(seq), _rand_ts(seq + 1)
            seq += 2
            # User message
            self._records.append({"type": "user.message", "timestamp": ts_u,
                "data": {"content": _rand_sentence()}})
            # Assistant message with optional tool requests
            tool_requests = []
            for tn in self._tools:
                cid = f"call-{i}-{tn}"
                tool_requests.append({"toolCallId": cid, "name": tn,
                                       "arguments": json.dumps({"cmd": _rand_word()})})
            self._records.append({"type": "assistant.message", "timestamp": ts_a,
                "data": {"content": _rand_sentence(),
                          "toolRequests": tool_requests}})
            # Tool results
            for tn in self._tools:
                cid = f"call-{i}-{tn}"
                self._records.append({"type": "tool.execution_complete",
                    "timestamp": _rand_ts(seq),
                    "data": {"toolCallId": cid, "success": True,
                              "result": {"output": _rand_sentence()}}})
                seq += 1
        return self._write()


class PeekSession(_BaseJSONLSession):
    """Build a generic SDK-format peek session.

    Usage:
        f = PeekSession(tmp_path, exchanges=2).build()
        f = PeekSession(tmp_path, model="gpt-4o").build()
    """

    def __init__(self, tmp_path, *, exchanges=1, name="session.jsonl", model=None):
        super().__init__(tmp_path, exchanges=exchanges, name=name)
        self._model = model or "test-model"

    def build(self):
        for i in range(self._exchanges):
            self._records.append({"type": "user", "timestamp": _rand_ts(i * 2), "cwd": "/test",
                "message": {"content": [{"type": "text", "text": _rand_sentence()}]}})
            self._records.append({"type": "assistant", "timestamp": _rand_ts(i * 2 + 1),
                "message": {"model": self._model,
                             "content": [{"type": "text", "text": _rand_sentence()}]}})
        return self._write()


def write_jsonl(tmp_path, records, name="session.jsonl"):
    """Low-level: write raw dicts as JSONL file, return path."""
    f = tmp_path / name
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return f


def get_message_content_blocks(record):
    """Extract content blocks from a standard message record."""
    return record.get("message", {}).get("content", [])


@pytest.fixture
def cli_db(tmp_path, test_db):
    """Test database with extracted IDs for CLI testing."""
    from types import SimpleNamespace

    from siftd.storage.sqlite import open_database

    conn = open_database(test_db, read_only=True)
    row = conn.execute(
        "SELECT id, external_id FROM conversations ORDER BY started_at LIMIT 1"
    ).fetchone()
    conn.close()
    return SimpleNamespace(
        path=test_db,
        conv_id=row["id"],
        external_id=row["external_id"],
        args=["--db", str(test_db)],
    )
