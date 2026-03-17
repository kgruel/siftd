from io import StringIO

from painted import Block, Style, join_vertical

from siftd.output.painted_bridge import print_block


class _TtyStringIO(StringIO):
    def isatty(self) -> bool:  # pragma: no cover - trivial shim
        return True


def test_print_block_ansi_trims_trailing_pad_spaces(monkeypatch):
    block = join_vertical(
        Block.text("abcdefghij", Style()),
        Block.text("hi", Style()),
    )

    stream = _TtyStringIO()
    monkeypatch.setattr("sys.stdout", stream)

    print_block(block)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0] == "\x1b[0mabcdefghij\x1b[0m"
    assert lines[1] == "\x1b[0mhi\x1b[0m"
