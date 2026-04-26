from pathlib import Path

from siftd.adapters import sdk


class TestDiscoverFilesContainment:
    """discover_files must not yield files whose resolved path escapes the scan base."""

    def test_symlink_escaping_base_is_skipped(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        outside = tmp_path / "outside" / "secret.jsonl"
        outside.parent.mkdir()
        outside.write_text("{}")
        link = base / "escape.jsonl"
        link.symlink_to(outside)

        results = list(sdk.discover_files([base], [], ["*.jsonl"]))
        assert not any("escape" in str(s.location) for s in results)

    def test_symlink_inside_base_is_yielded(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        real = base / "real.jsonl"
        real.write_text("{}")
        link = base / "linked.jsonl"
        link.symlink_to(real)

        results = list(sdk.discover_files([base], [], ["*.jsonl"]))
        locations = [s.location for s in results]
        assert any("linked" in str(loc) for loc in locations)

    def test_regular_file_is_yielded(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        f = base / "session.jsonl"
        f.write_text("{}")

        results = list(sdk.discover_files([base], [], ["*.jsonl"]))
        assert len(results) == 1

    def test_default_locations_also_filters_escapees(self, tmp_path):
        base = tmp_path / "adapter"
        base.mkdir()
        outside = tmp_path / "outside" / "x.jsonl"
        outside.parent.mkdir()
        outside.write_text("{}")
        (base / "escape.jsonl").symlink_to(outside)

        results = list(sdk.discover_files(None, [str(base)], ["*.jsonl"]))
        assert not any("escape" in str(s.location) for s in results)


def test_seek_last_lines_returns_empty_when_binary_open_fails(monkeypatch, tmp_path):
    p = tmp_path / "big.log"
    p.write_text("x\n" * 20000)

    orig_open = Path.open

    def fake_open(self, mode="r", *args, **kwargs):
        if self == p and mode == "rb":
            raise OSError("boom")
        return orig_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    assert sdk.seek_last_lines(p, 5) == []
