from pathlib import Path

from siftd.adapters import sdk


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
