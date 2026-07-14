from pathlib import Path

from siftd.api import adapters as api_adapters


def test_list_adapters_includes_entrypoint_plugins(monkeypatch):
    plugin = object()
    monkeypatch.setattr("siftd.api.adapters.load_builtin_adapters", lambda: [])
    monkeypatch.setattr("siftd.api.adapters.load_dropin_adapters", lambda _p: [])
    monkeypatch.setattr("siftd.api.adapters.load_entrypoint_adapters", lambda: [plugin])
    monkeypatch.setattr("siftd.api.adapters.plugin_to_adapter_info", lambda p: {"ok": p is plugin})
    assert api_adapters.list_adapters(dropin_path=Path("/tmp")) == [{"ok": True}]


def test_plugin_to_adapter_info_tier_default_and_explicit():
    from types import SimpleNamespace

    from siftd.plugin_discovery import PluginInfo

    bare = PluginInfo(
        name="x", origin="dropin", module=SimpleNamespace(DEFAULT_LOCATIONS=[]),
        source_path=None, entrypoint=None,
    )
    assert api_adapters.plugin_to_adapter_info(bare).tier == "contrib"

    tiered = PluginInfo(
        name="y", origin="builtin",
        module=SimpleNamespace(DEFAULT_LOCATIONS=[], SUPPORT_TIER="frozen"),
        source_path=None, entrypoint=None,
    )
    assert api_adapters.plugin_to_adapter_info(tiered).tier == "frozen"
