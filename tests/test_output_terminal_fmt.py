"""Focused tests for terminal formatter edge paths."""

from siftd.output import terminal_fmt


def test_render_detail_accepts_raw_turns_list(monkeypatch):
    calls = {}

    def _fake_render(detail, *, turns, fidelity, tool_chars):
        calls["detail"] = detail
        calls["turns"] = turns
        calls["fidelity"] = fidelity
        calls["tool_chars"] = tool_chars
        return "ok"

    monkeypatch.setattr("siftd.output.painted_bridge.render_query_detail_block", _fake_render)

    turns = [{"role": "user", "content": "hi"}]
    detail = object()
    out = terminal_fmt.render_detail(turns, fidelity=object(), detail=detail, tool_chars=42)

    assert out == "ok"
    assert calls["detail"] is detail
    assert calls["turns"] == turns
    assert calls["tool_chars"] == 42
