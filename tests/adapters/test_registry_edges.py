from pathlib import Path
from types import SimpleNamespace

from siftd.adapters import registry
from siftd.domain import Source


def test_load_all_adapters_applies_config_path_override(monkeypatch):
    plugin = SimpleNamespace(name="aider", module=object())
    wrapped = object()
    monkeypatch.setattr("siftd.adapters.registry.load_all_extensions", lambda **kwargs: [plugin])
    monkeypatch.setattr(
        "siftd.config.get_adapter_settings", lambda: {"aider": {"locations": ["/tmp/custom"]}}
    )
    monkeypatch.setattr("siftd.adapters.registry.wrap_adapter_paths", lambda module, paths: wrapped)
    assert registry.load_all_adapters(dropin_path=Path("/tmp/dropins"))[0].module is wrapped


def test_load_all_adapters_filters_disabled(monkeypatch):
    plugins = [
        SimpleNamespace(name="aider", module=object()),
        SimpleNamespace(name="claude_code", module=object()),
    ]
    monkeypatch.setattr("siftd.adapters.registry.load_all_extensions", lambda **kwargs: plugins)
    monkeypatch.setattr(
        "siftd.config.get_adapter_settings", lambda: {"aider": {"enabled": False}}
    )

    disabled = []
    result = registry.load_all_adapters(dropin_path=Path("/tmp/dropins"), disabled_out=disabled)

    assert [p.name for p in result] == ["claude_code"]
    assert disabled == ["aider"]


def test_load_all_adapters_disabled_out_optional(monkeypatch):
    plugins = [SimpleNamespace(name="aider", module=object())]
    monkeypatch.setattr("siftd.adapters.registry.load_all_extensions", lambda **kwargs: plugins)
    monkeypatch.setattr(
        "siftd.config.get_adapter_settings", lambda: {"aider": {"enabled": False}}
    )

    assert registry.load_all_adapters(dropin_path=Path("/tmp/dropins")) == []


def test_load_all_adapters_reads_disable_knob_from_config_file(tmp_path, monkeypatch):
    """End-to-end: [adapters.<name>] enabled = false in config.toml drops the adapter."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    conf = tmp_path / "siftd"
    conf.mkdir()
    (conf / "config.toml").write_text(
        "[adapters.aider]\nenabled = false\n[adapters.no_such_adapter]\nenabled = false\n"
    )

    disabled = []
    names = [
        p.name
        for p in registry.load_all_adapters(dropin_path=tmp_path / "dropins", disabled_out=disabled)
    ]
    assert "aider" not in names
    assert "claude_code" in names
    assert disabled == ["aider"]  # unknown config names are simply never consulted


def test_wrap_adapter_paths_can_handle_only_within_override_root():
    adapter = SimpleNamespace(
        can_handle=lambda source: source.location.name == "session.jsonl"
    )
    wrapped = registry.wrap_adapter_paths(adapter, ["/tmp/custom"])

    assert wrapped.can_handle(
        Source(kind="file", location=Path("/tmp/custom/session.jsonl"))
    )
    assert not wrapped.can_handle(
        Source(kind="file", location=Path("/tmp/custom/other.jsonl"))
    )
    assert not wrapped.can_handle(
        Source(kind="file", location=Path("/tmp/elsewhere/session.jsonl"))
    )
