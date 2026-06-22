from types import SimpleNamespace

from siftd.output import painted_bridge as pb


class _Style:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def merge(self, other):
        return _Style(**(self.kwargs | getattr(other, "kwargs", {})))


def test_styles_lines_and_multiline_edges(monkeypatch):
    class _Block:
        @staticmethod
        def empty(w, h):
            return (w, h)

    pal = SimpleNamespace(accent=_Style(a=1), muted=_Style(m=1), error=_Style(e=1))
    monkeypatch.setattr(pb, "_painted", lambda: (_Block, object, object, _Style, lambda: pal, lambda *a: a, lambda x: x))
    assert isinstance(pb._styles().heading, _Style)
    assert pb._lines_to_block([]) == (0, 0)

    lines = []
    monkeypatch.setattr(pb, "truncate_text", lambda _t, _l: "")
    pb._append_multiline(lines, "P: ", _Style(), "x", _Style(), 1)
    monkeypatch.setattr(pb, "truncate_text", lambda _t, _l: "a\nb")
    monkeypatch.setattr(pb, "_line", lambda *parts: parts)
    pb._append_multiline(lines, "P: ", _Style(), "x", _Style(), 1)
    assert lines and len(lines) == 2


def test_tool_content_title_count_suffix(monkeypatch):
    # No box now — the count rides the `→ name ×N` header line the collapsed
    # tool summary also uses; the input/result append beneath it.
    d = SimpleNamespace(
        _role_styles=_Style(),
        _tool_chars=0,
        _pending=[],
        _ds=SimpleNamespace(tool_error=_Style(), tool_name=_Style(), separator=_Style()),
    )
    monkeypatch.setattr(pb, "_render_tool_content_lines", lambda *a, **k: [])
    monkeypatch.setattr(pb, "_line", lambda *parts: parts)  # capture header parts
    pb.PaintedEmitter.tool_content(d, "search", 2, None, None, None)
    header_text = "".join(text for text, _style in d._pending[0])
    assert "→ search ×2" in header_text
