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
    seen = {}
    d = SimpleNamespace(_role_styles=_Style(), _tool_chars=0, _parts=[], _ds=SimpleNamespace(tool_error=_Style(), tool_name=_Style(), tool_border="|", separator=_Style()))
    d._flush_lines = lambda: None
    d._pad = lambda b, **_k: b
    d._border = lambda b, **k: seen.setdefault("title", k["title"]) or b
    monkeypatch.setattr(pb, "_render_tool_content_lines", lambda *a, **k: [1])
    monkeypatch.setattr(pb, "_lines_to_block", lambda _l: SimpleNamespace(width=99))
    pb.PaintedEmitter.tool_content(d, "search", 2, None, None, None)
    assert seen["title"] == "search ×2"
