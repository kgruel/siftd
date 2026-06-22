"""Tests for siftd.output.gutter — the grain-gutter taxonomy + the rail it draws.

Pins the siftd-authored half (kind/status → glyph+style) and that the emitter
prepends a continuous, kind-switching rail down a rendered narrative.
"""

from dataclasses import dataclass, field

from painted import Block, Fidelity, Style, use_theme

from siftd.output.gutter import apply_event_gutter, gutter_event_kind
from siftd.output.theme import siftd_theme


def _fg(style) -> str | None:
    return getattr(style, "fg", None)


def test_gutter_event_kind_glyphs_and_styles():
    with use_theme(siftd_theme):
        # user is the bright cream mark; assistant + thinking recede; a tool takes
        # its outcome (✓ pass / ✗ fail).
        g_user, s_user = gutter_event_kind("user", {})
        g_asst, s_asst = gutter_event_kind("assistant", {})
        g_think, s_think = gutter_event_kind("thinking", {})
        g_ok, s_ok = gutter_event_kind("tool", {"status": "success"})
        g_err, s_err = gutter_event_kind("tool", {"status": "error"})
        g_none, _ = gutter_event_kind("tool", {})  # no status → assumed pass

    assert g_user == "▪" and g_asst == "▪"
    assert g_think == "·"  # distinct, lighter — skippable reasoning
    assert g_ok == "✓" and g_err == "✗"
    assert g_none == "✓"  # a completed call with no error reads as a pass
    # user is brighter than the recessed assistant/thinking marks
    assert _fg(s_user) != _fg(s_asst)
    assert _fg(s_asst) == _fg(s_think)
    # ✓ teal vs ✗ red are distinct hues
    assert _fg(s_ok) != _fg(s_err)


def test_gutter_event_kind_degrades_to_ascii():
    with use_theme(siftd_theme):
        assert gutter_event_kind("user", {"ascii": True})[0] == "*"
        assert gutter_event_kind("thinking", {"ascii": True})[0] == "."
        assert gutter_event_kind("tool", {"status": "success", "ascii": True})[0] == "+"
        assert gutter_event_kind("tool", {"status": "error", "ascii": True})[0] == "x"


def test_failed_statuses_read_as_fail():
    with use_theme(siftd_theme):
        for status in ("error", "failed", "failure"):
            assert gutter_event_kind("tool", {"status": status})[0] == "✗"
        for status in ("success", "ok", "completed", ""):
            assert gutter_event_kind("tool", {"status": status})[0] == "✓"


def test_apply_event_gutter_draws_a_continuous_rail():
    # Every line of the block carries the mark — a continuous rail, not just the
    # first line — so a multi-line tool/thinking block is railed throughout.
    with use_theme(siftd_theme):
        block = Block.column([("line one", Style()), ("line two", Style())])
        railed = apply_event_gutter(block, "tool", status="error", ascii_mode=True)
    rows = ["".join(c.char for c in railed.row(y)) for y in range(railed.height)]
    assert all(r.startswith("x ") for r in rows)
    assert railed.width == block.width + 2  # glyph + space


# --- the emitter prepends the kind-switching rail ----------------------------


@dataclass
class _NB:
    block_type: str
    content: str = ""
    tool_calls: list = field(default_factory=list)
    event_id: str | None = None


@dataclass
class _TC:
    tool_name: str
    count: int = 1
    input: str | None = None
    result: str | None = None
    status: str | None = None
    tool_call_id: str | None = None


def test_narrative_rail_switches_mark_by_kind():
    from siftd.output.painted_bridge import render_narrative_block

    blocks = [
        _NB("text", "assistant prose here"),
        _NB("thinking", "some reasoning that the rail marks as skippable"),
        _NB("tool_calls", tool_calls=[_TC("Read", input="a.py", result="ok", status="success")]),
        _NB("tool_calls", tool_calls=[_TC("Bash", input="pytest", result="boom", status="error")]),
    ]
    with use_theme(siftd_theme):
        block = render_narrative_block(
            blocks, fidelity=Fidelity(visible=frozenset({"text", "thinking", "tools"}), depth=3)
        )
    text = "\n".join("".join(c.char for c in block.row(y)) for y in range(block.height))
    # Non-TTY test stream → ASCII rail glyphs. Each kind's mark leads its lines.
    lead = {line[0] for line in text.splitlines() if line.strip()}
    assert "*" in lead  # assistant prose
    assert "." in lead  # thinking
    assert "+" in lead  # tool success
    assert "x" in lead  # tool error
