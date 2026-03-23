from siftd.api import sessions as api_sessions


def test_register_session_wrapper_forwards_workspace_and_commit(monkeypatch):
    seen = {}

    def fake_register(conn, harness_session_id, adapter_name, workspace_path, commit=False):
        seen.update(
            harness_session_id=harness_session_id,
            adapter_name=adapter_name,
            workspace_path=workspace_path,
            commit=commit,
        )
        return "sid-123"

    monkeypatch.setattr("siftd.api.sessions._register_session", fake_register)

    out = api_sessions.register_session(
        conn=object(),
        harness_session_id="hs-1",
        adapter_name="pi",
        workspace_path="/tmp/ws",
        commit=True,
    )

    assert out == "sid-123"
    assert seen == {
        "harness_session_id": "hs-1",
        "adapter_name": "pi",
        "workspace_path": "/tmp/ws",
        "commit": True,
    }
