from pathlib import Path
from types import SimpleNamespace

from siftd.adapters import registry


def test_load_all_adapters_applies_config_path_override(monkeypatch):
    plugin = SimpleNamespace(name="aider", module=object())
    wrapped = object()
    monkeypatch.setattr("siftd.adapters.registry.load_all_extensions", lambda **kwargs: [plugin])
    monkeypatch.setattr("siftd.config.get_adapter_locations", lambda name: ["/tmp/custom"])
    monkeypatch.setattr("siftd.adapters.registry.wrap_adapter_paths", lambda module, paths: wrapped)
    assert registry.load_all_adapters(dropin_path=Path("/tmp/dropins"))[0].module is wrapped
