from types import SimpleNamespace

from siftd.output import painted_bridge as pb


class _Style:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def merge(self, other):
        merged = dict(self.kwargs)
        merged.update(getattr(other, "kwargs", {}))
        return _Style(**merged)


def test_styles_and_empty_lines_block(monkeypatch):
    class _Block:
        @staticmethod
        def empty(w, h):
            return (w, h)

    palette = SimpleNamespace(accent=_Style(accent=True), muted=_Style(muted=True), error=_Style(error=True))
    monkeypatch.setattr(pb, "_painted", lambda: (_Block, object, object, _Style, lambda: palette, lambda *a: a, lambda x: x))

    styles = pb._styles()
    assert isinstance(styles.heading, _Style)
    assert pb._lines_to_block([]) == (0, 0)


def test_append_multiline_empty_and_continuation(monkeypatch):
    lines = []
    monkeypatch.setattr(pb, "truncate_text", lambda text, limit: "")
    pb._append_multiline(lines, "P: ", _Style(), "content", _Style(), 10)
    assert lines == []

    monkeypatch.setattr(pb, "truncate_text", lambda text, limit: "line1\nline2")
    monkeypatch.setattr(pb, "_line", lambda *parts: parts)
    pb._append_multiline(lines, "P: ", _Style(), "content", _Style(), 10)
    assert len(lines) == 2


def test_tool_content_title_includes_count_suffix(monkeypatch):
    captured = {}

    dummy = SimpleNamespace(
        _role_styles=_Style(),
        _tool_chars=0,
        _parts=[],
        _ds=SimpleNamespace(tool_error=_Style(), tool_name=_Style(), tool_border="|", separator=_Style()),
    )
    dummy._flush_lines = lambda: None
    dummy._pad = lambda b, **kwargs: b
    dummy._border = lambda block, **kwargs: captured.setdefault("title", kwargs.get("title")) or block

    monkeypatch.setattr(pb, "_render_tool_content_lines", lambda *a, **k: [object()])
    monkeypatch.setattr(pb, "_lines_to_block", lambda lines: SimpleNamespace(width=99))

    pb.PaintedEmitter.tool_content(dummy, "search", 2, None, None, None)
    assert captured["title"] == "search ×2"
