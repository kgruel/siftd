from pathlib import Path
from types import SimpleNamespace

from siftd.adapters import registry
from siftd.domain import Source


def test_load_all_adapters_applies_config_path_override(monkeypatch):
    plugin = SimpleNamespace(name="aider", module=object())
    wrapped = object()
    monkeypatch.setattr("siftd.adapters.registry.load_all_extensions", lambda **kwargs: [plugin])
    monkeypatch.setattr("siftd.config.get_adapter_locations", lambda name: ["/tmp/custom"])
    monkeypatch.setattr("siftd.adapters.registry.wrap_adapter_paths", lambda module, paths: wrapped)
    assert registry.load_all_adapters(dropin_path=Path("/tmp/dropins"))[0].module is wrapped


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
