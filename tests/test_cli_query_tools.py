import json
from pathlib import Path

from siftd.api import list_conversations
from siftd.cli import main
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
    insert_tool_call,
)


def _build_db(tmp_path: Path, *, response_text: str = "Working on it.") -> Path:
    db_path = tmp_path / "query_tools.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "gpt-test")
    shell_tool_id = get_or_create_tool(conn, "shell.execute")
    todo_tool_id = get_or_create_tool(conn, "ui.todo")

    conv_id = insert_conversation(
        conn,
        external_id="conv1",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )
    prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, prompt_id, 0, "text", json.dumps({"text": "Do the thing"}))
    response_id = insert_response(
        conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50,
    )
    insert_response_content(conn, response_id, 0, "text", json.dumps({"text": response_text}))
    insert_response_content(conn, response_id, 1, "thinking", json.dumps({"thinking": "First I'll inspect the repo state."}))
    insert_response_content(conn, response_id, 2, "tool_use", json.dumps({"id": "tc1", "name": "shell.execute"}))
    insert_response_content(conn, response_id, 3, "tool_use", json.dumps({"id": "tc2", "name": "ui.todo"}))
    insert_tool_call(
        conn, response_id, conv_id, shell_tool_id, "tc1",
        json.dumps({"cmd": "git status", "max_output_tokens": 4000}),
        json.dumps({"output": "Chunk ID: abc123\nWall time: 0.0000 seconds\nProcess exited with code 0\nOutput:\nM file.py\n"}),
        "success", "2024-01-15T10:00:02Z",
    )
    insert_tool_call(
        conn, response_id, conv_id, todo_tool_id, "tc2",
        json.dumps({"plan": [{"step": "Inspect", "status": "in_progress"}], "title": "Plan updated"}),
        json.dumps({"output": "Plan updated"}),
        "success", "2024-01-15T10:00:03Z",
    )

    conn.commit()
    conn.close()
    return db_path


def test_query_detail_plain_output(capsys, tmp_path):
    db = _build_db(tmp_path)
    conv_id = list_conversations(db_path=db, n=1)[0].id

    rc = main(["--db", str(db), "query", conv_id])
    assert rc == 0
    out = capsys.readouterr().out
    # Non-TTY output is markdown format
    assert f"# Session {conv_id[:12]}" in out
    assert "project" in out
    assert "User" in out
    assert "Do the thing" in out
    assert "Assistant" in out
    assert "*[shell.execute" in out


def test_query_tools_formats_input_and_result(capsys, tmp_path):
    db = _build_db(tmp_path)
    conv_id = list_conversations(db_path=db, n=1)[0].id

    rc = main(["--db", str(db), "query", conv_id, "--tools", "all"])
    assert rc == 0
    out = capsys.readouterr().out
    # Markdown tool detail: shows tool name and raw input/result
    assert "**shell.execute**" in out
    assert "git status" in out
    assert "Plan updated" in out


def test_query_thinking_shows_thinking_without_tool_payloads(capsys, tmp_path):
    db = _build_db(tmp_path)
    conv_id = list_conversations(db_path=db, n=1)[0].id

    rc = main(["--db", str(db), "query", conv_id, "--thinking"])
    assert rc == 0
    out = capsys.readouterr().out
    # Markdown thinking: blockquote format
    assert "First I'll inspect the repo state." in out
    assert "**Thinking**" in out
    # Tool summary present but not expanded
    assert "*[shell.execute" in out


def test_query_default_detail_does_not_truncate_text(capsys, tmp_path):
    long_text = "Working on it. " * 30
    db = _build_db(tmp_path, response_text=long_text)
    conv_id = list_conversations(db_path=db, n=1)[0].id

    rc = main(["--db", str(db), "query", conv_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("Working on it.") >= 20
    assert "..." not in out


def test_query_brief_alias_truncates_text(capsys, tmp_path):
    long_text = "Working on it. " * 30
    db = _build_db(tmp_path, response_text=long_text)
    conv_id = list_conversations(db_path=db, n=1)[0].id

    rc = main(["--db", str(db), "query", conv_id, "-b"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Working on it." in out
    assert "..." in out


def test_query_full_alias_implies_tool_content(capsys, tmp_path):
    db = _build_db(tmp_path)
    conv_id = list_conversations(db_path=db, n=1)[0].id

    rc = main(["--db", str(db), "query", conv_id, "-F"])
    assert rc == 0
    out = capsys.readouterr().out
    # --full expands tools in markdown format
    assert "git status" in out
    assert "Plan updated" in out
