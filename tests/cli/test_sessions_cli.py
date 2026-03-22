"""Tests for siftd.cli.sessions handlers."""

from types import SimpleNamespace

from siftd.cli.sessions import cmd_register, cmd_session_id


class _Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_cmd_register_writes_state_and_registers(tmp_path, monkeypatch, capsys):
    conn, calls = _Conn(), {}
    sid_path = tmp_path / "state" / "session-id"
    workspace = tmp_path / "repo"
    workspace.mkdir()

    monkeypatch.setattr("siftd.cli.sessions.resolve_db", lambda args: tmp_path / "db.sqlite")
    monkeypatch.setattr("siftd.cli.sessions.ensure_dirs", lambda: None)
    monkeypatch.setattr("siftd.cli.sessions.create_database", lambda db: conn)
    monkeypatch.setattr("siftd.cli.sessions.register_session", lambda c, s, a, w, commit: calls.setdefault("r", (s, a, w, commit)))
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: sid_path)

    rc = cmd_register(SimpleNamespace(db=None, session="sess-123", adapter="claude_code", workspace=str(workspace)))
    assert rc == 0
    assert calls["r"] == ("sess-123", "claude_code", str(workspace.resolve()), True)
    assert sid_path.read_text() == "sess-123"
    assert conn.closed and "Registered session" in capsys.readouterr().out


def test_cmd_session_id_prefers_state_file(tmp_path, monkeypatch, capsys):
    sid_path = tmp_path / "session-id"
    sid_path.write_text("abc\n")
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: sid_path)

    assert cmd_session_id(SimpleNamespace(workspace=str(tmp_path), db=None)) == 0
    assert capsys.readouterr().out.strip() == "abc"


def test_cmd_session_id_falls_back_to_db(tmp_path, monkeypatch, capsys):
    conn = _Conn()
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: tmp_path / "missing")
    db = tmp_path / "db.sqlite"
    db.write_text("x")
    monkeypatch.setattr("siftd.cli.sessions.resolve_db", lambda args: db)
    monkeypatch.setattr("siftd.cli.sessions.open_database", lambda *_a, **_k: conn)
    monkeypatch.setattr("siftd.cli.sessions.find_active_session", lambda c, w: "from-db")

    assert cmd_session_id(SimpleNamespace(workspace=str(tmp_path), db=None)) == 0
    assert capsys.readouterr().out.strip() == "from-db"
    assert conn.closed


def test_cmd_session_id_returns_1_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("siftd.cli.sessions.session_id_file", lambda ws: tmp_path / "missing")
    monkeypatch.setattr("siftd.cli.sessions.resolve_db", lambda args: tmp_path / "none.db")
    assert cmd_session_id(SimpleNamespace(workspace=str(tmp_path), db=None)) == 1
