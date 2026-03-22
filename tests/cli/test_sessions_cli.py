"""Tests for siftd.cli.sessions handlers."""

from types import SimpleNamespace

from siftd.cli import main
from siftd.cli.sessions import cmd_register, cmd_session_id


def _args(**kwargs):
    base = {
        "db": None,
        "session": "sess-123",
        "adapter": "claude_code",
        "workspace": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_cmd_register_writes_session_file(tmp_path, monkeypatch, capsys):
    fake_db = tmp_path / "db.sqlite"
    monkeypatch.setattr("siftd.cli.sessions.resolve_db", lambda args: fake_db)
    monkeypatch.setattr("siftd.cli.sessions.ensure_dirs", lambda: None)

    calls = {}

    class Conn:
        def close(self):
            calls["closed"] = True

    monkeypatch.setattr("siftd.cli.sessions.create_database", lambda db: Conn())

    def _register(conn, session_id, adapter, workspace, commit):
        calls["register"] = (session_id, adapter, workspace, commit)

    monkeypatch.setattr("siftd.cli.sessions.register_session", _register)

    sid_path = tmp_path / "state" / "session-id"
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: sid_path)

    workspace = tmp_path / "repo"
    workspace.mkdir()
    rc = cmd_register(_args(workspace=str(workspace)))
    assert rc == 0

    assert calls["register"][0] == "sess-123"
    assert calls["register"][1] == "claude_code"
    assert calls["register"][2] == str(workspace.resolve())
    assert calls["register"][3] is True
    assert sid_path.read_text() == "sess-123"
    assert calls["closed"] is True
    assert "Registered session" in capsys.readouterr().out


def test_cmd_session_id_reads_state_file(tmp_path, monkeypatch, capsys):
    sid_path = tmp_path / "state" / "session-id"
    sid_path.parent.mkdir(parents=True, exist_ok=True)
    sid_path.write_text("abc-123\n")

    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: sid_path)

    rc = cmd_session_id(SimpleNamespace(workspace=str(tmp_path), db=None))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "abc-123"


def test_cmd_session_id_fallback_to_db(tmp_path, monkeypatch, capsys):
    sid_path = tmp_path / "state" / "session-id"
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: sid_path)

    db = tmp_path / "db.sqlite"
    db.write_text("x")
    monkeypatch.setattr("siftd.cli.sessions.resolve_db", lambda args: db)

    calls = {}

    class Conn:
        def close(self):
            calls["closed"] = True

    monkeypatch.setattr("siftd.cli.sessions.open_database", lambda *_a, **_k: Conn())
    monkeypatch.setattr("siftd.cli.sessions.find_active_session", lambda conn, ws: "from-db")

    rc = cmd_session_id(SimpleNamespace(workspace=str(tmp_path), db=None))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "from-db"
    assert calls["closed"] is True


def test_cmd_session_id_not_found_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: tmp_path / "missing")
    monkeypatch.setattr("siftd.cli.sessions.resolve_db", lambda args: tmp_path / "none.db")
    rc = cmd_session_id(SimpleNamespace(workspace=str(tmp_path), db=None))
    assert rc == 1


def test_main_register_and_session_id_cli(tmp_path, monkeypatch):
    sid_path = tmp_path / "state" / "session-id"
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: sid_path)
    monkeypatch.setattr("siftd.cli.sessions.ensure_dirs", lambda: None)

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr("siftd.cli.sessions.create_database", lambda db: Conn())
    monkeypatch.setattr("siftd.cli.sessions.register_session", lambda *a, **k: None)

    assert main(["register", "--session", "abc", "--adapter", "claude_code", "--workspace", str(tmp_path)]) == 0
    assert main(["session-id", "--workspace", str(tmp_path)]) == 0
    assert sid_path.read_text() == "abc"
