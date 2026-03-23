from siftd.output import terminal_fmt


def test_render_detail_handles_raw_turns_and_detail_turns(monkeypatch):
    seen = []

    def _fake_render(detail, *, turns, fidelity, tool_chars):
        seen.append((detail, turns, tool_chars))
        return "ok"

    monkeypatch.setattr("siftd.output.painted_bridge.render_query_detail_block", _fake_render)

    raw_turns = [{"role": "user", "content": "hi"}]
    detail_obj = object()
    assert terminal_fmt.render_detail(raw_turns, fidelity=object(), detail=detail_obj, tool_chars=42) == "ok"
    assert seen[-1] == (detail_obj, raw_turns, 42)

    class Detail:
        turns = [{"role": "assistant", "content": "yo"}]

    detail = Detail()
    assert terminal_fmt.render_detail(detail, fidelity=object()) == "ok"
    assert seen[-1] == (detail, detail.turns, 0)
