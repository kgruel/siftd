from pathlib import Path

from siftd.api import adapters as api_adapters


def test_list_builtin_adapters_contains_expected_keys():
    out = api_adapters.list_builtin_adapters()
    assert "aider" in out and "claude_code" in out and "codex_cli" in out and "gemini_cli" in out


def test_list_adapters_includes_entrypoint_plugins(monkeypatch):
    plugin = object()
    monkeypatch.setattr("siftd.api.adapters.load_builtin_adapters", lambda: [])
    monkeypatch.setattr("siftd.api.adapters.load_dropin_adapters", lambda _p: [])
    monkeypatch.setattr("siftd.api.adapters.load_entrypoint_adapters", lambda: [plugin])
    monkeypatch.setattr("siftd.api.adapters.plugin_to_adapter_info", lambda p: {"ok": p is plugin})

    out = api_adapters.list_adapters(dropin_path=Path("/tmp"))

    assert out == [{"ok": True}]
