from pathlib import Path
from types import ModuleType

from siftd.peek.reader import _find_adapter_for_file, _resolve_peek_scan
from siftd.plugin_discovery import PluginInfo


class TestFindAdapterContainment:
    """R16: is_relative_to must be used instead of startswith for path containment."""

    def _fake_plugin(self, base: Path) -> PluginInfo:
        m = ModuleType("fake_adapter")
        m.normalize_record = lambda r: None  # type: ignore[attr-defined]
        m.DEFAULT_LOCATIONS = [str(base)]  # type: ignore[attr-defined]
        return PluginInfo(name="fake", origin="builtin", module=m)

    def test_common_prefix_path_is_rejected(self, monkeypatch, tmp_path):
        """A path sharing a prefix (e.g. /tmp/foo vs /tmp/foobar) must NOT match."""
        base = tmp_path / "foo"
        base.mkdir()
        monkeypatch.setattr(
            "siftd.peek.reader.load_all_adapters",
            lambda: [self._fake_plugin(base)],
        )
        sibling = tmp_path / "foobar" / "x.py"
        assert _find_adapter_for_file(sibling) is None

    def test_true_descendant_matches(self, monkeypatch, tmp_path):
        """A path genuinely under the base dir must match."""
        base = tmp_path / "foo"
        base.mkdir()
        monkeypatch.setattr(
            "siftd.peek.reader.load_all_adapters",
            lambda: [self._fake_plugin(base)],
        )
        subfile = base / "sub" / "x.py"
        assert _find_adapter_for_file(subfile) is not None


def test_resolve_peek_scan_derived_function_invokes_sdk_scan(monkeypatch):
    mod = ModuleType("m")
    mod.normalize_record = lambda _r: None
    monkeypatch.setattr("siftd.adapters.sdk.iter_jsonl", lambda p: [{"x": 1}])
    monkeypatch.setattr(
        "siftd.adapters.sdk.peek_scan_from_records",
        lambda records, norm, **kwargs: (list(records), norm is mod.normalize_record, kwargs["default_session_id"]),
    )
    assert _resolve_peek_scan(mod)(Path("/tmp/s1.jsonl")) == ([{"x": 1}], True, "s1")
