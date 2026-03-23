from siftd.api import sessions as api_sessions


def test_register_session_wrapper_forwards_workspace_and_commit(monkeypatch):
    seen = []

    def fake_register(conn, harness_session_id, adapter_name, workspace_path, commit=False):
        seen.append((harness_session_id, adapter_name, workspace_path, commit))
        return "sid-123"

    monkeypatch.setattr("siftd.api.sessions._register_session", fake_register)
    out = api_sessions.register_session(object(), "hs-1", "pi", "/tmp/ws", commit=True)

    assert out == "sid-123"
    assert seen == [("hs-1", "pi", "/tmp/ws", True)]
