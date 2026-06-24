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


def test_railed_lines_never_overflow_the_width(monkeypatch):
    # The rail prefix is reserved out of the content width, so no line exceeds the
    # requested width — content wraps to width − GUTTER_COLS and the rail adds it
    # back. A long unbroken prompt + reasoning would overflow if the reservation
    # were missing.
    import siftd.output.painted_bridge as pb
    from siftd.output.painted_bridge import render_narrative_block

    monkeypatch.setattr(pb, "term_width", lambda *a, **k: 50)
    monkeypatch.setattr(pb, "prefers_ascii", lambda *a, **k: True)
    long_prose = "the quick brown fox jumps over the lazy dog " * 4
    # The wrapped paths (prose + reasoning) reserve the rail out of the width;
    # tool I/O renders at natural width (unwrapped, pre-existing) so it isn't part
    # of this contract — keep its lines short here.
    blocks = [
        _NB("text", long_prose),
        _NB("thinking", long_prose),
        _NB("tool_calls", tool_calls=[_TC("Bash", input="pytest", result="ok", status="error")]),
    ]
    with use_theme(siftd_theme):
        block = render_narrative_block(
            blocks, fidelity=Fidelity(visible=frozenset({"text", "thinking", "tools"}), depth=3)
        )
    assert block.width <= 50  # the rail prefix never pushes a wrapped line past the width


def test_long_tool_command_wraps_to_the_indent(monkeypatch):
    # A long tool command/output reflows to the tool indent under the rail rather
    # than spilling to column 0 — aligned with the rest of the feed.
    import siftd.output.painted_bridge as pb
    from siftd.output.painted_bridge import render_narrative_block

    monkeypatch.setattr(pb, "term_width", lambda *a, **k: 50)
    monkeypatch.setattr(pb, "prefers_ascii", lambda *a, **k: True)
    long_cmd = "git status --short && echo done && git log --oneline -20 && echo finished here"
    blocks = [_NB("tool_calls", tool_calls=[_TC("shell.execute", input=long_cmd, result="ok", status="success")])]
    with use_theme(siftd_theme):
        block = render_narrative_block(
            blocks, fidelity=Fidelity(visible=frozenset({"tools"}), depth=3)
        )
    lines = ["".join(c.char for c in block.row(y)).rstrip() for y in range(block.height)]
    assert block.width <= 50  # the wrapped command no longer overflows the width
    # every rendered tool line is railed (leads with +), none spill to column 0
    assert all(line[0] == "+" for line in lines if line)


def test_detail_view_rails_user_and_assistant_distinctly(monkeypatch):
    # The whole turn carries the rail: the prompt takes the bright user mark, the
    # response takes the recessed assistant mark — distinct gutter colours even
    # though both glyphs are ▪ (a non-TTY render keeps the style, drops the ANSI).
    import siftd.output.painted_bridge as pb
    from siftd.output.painted_bridge import render_query_detail_block

    monkeypatch.setattr(pb, "term_width", lambda *a, **k: 80)
    monkeypatch.setattr(pb, "prefers_ascii", lambda *a, **k: True)

    @dataclass
    class _Turn:
        timestamp: str
        prompt_text: str
        narrative: list
        total_input_tokens: int = 10
        total_output_tokens: int = 20
        tool_call_summaries: list = field(default_factory=list)

    @dataclass
    class _Detail:
        id: str = "01ABC"
        workspace_path: str = "/w"
        started_at: str = "2026-06-22T14:32:00"
        total_input_tokens: int = 10
        total_output_tokens: int = 20
        model: str = "m"
        tags: list = field(default_factory=list)

    turn = _Turn("2026-06-22T14:32:10", "the user prompt", [_NB("text", "the assistant reply")])
    with use_theme(siftd_theme):
        block = render_query_detail_block(
            _Detail(), turns=[turn], fidelity=Fidelity(visible=frozenset({"text"}), depth=1)
        )

    def gutter_fg(needle: str):
        for y in range(block.height):
            row = block.row(y)
            line = "".join(c.char for c in row)
            if needle in line and row and row[0].char == "*":
                return getattr(row[0].style, "fg", None)
        return None

    user_fg = gutter_fg("the user prompt")
    asst_fg = gutter_fg("the assistant reply")
    assert user_fg is not None and asst_fg is not None
    assert user_fg != asst_fg  # bright user vs recessed assistant


def test_block_break_never_leads_or_doubles_blanks():
    from siftd.output.painted_bridge import PaintedEmitter
    from siftd.output.theme import domain_styles

    with use_theme(siftd_theme):
        e = PaintedEmitter(domain_styles(), 0, width=80, ascii_mode=True)
        e.text("first")
        e._block_break()
        e._block_break()  # a second break must not add a second blank
        e.text("second")
        block = e.result()
    lines = ["".join(c.char for c in block.row(y)).rstrip() for y in range(block.height)]
    assert lines[0] != ""  # no leading blank
    assert lines[-1] != ""  # no trailing blank
    assert not any(lines[i] == "" and lines[i + 1] == "" for i in range(len(lines) - 1))  # no doubles


def test_tool_summary_rails_each_call_by_its_own_status():
    from siftd.output.painted_bridge import PaintedEmitter
    from siftd.output.theme import domain_styles

    with use_theme(siftd_theme):
        e = PaintedEmitter(domain_styles(), 0, width=80, ascii_mode=True)
        e.tool_summary([("Read", 1, "success"), ("Bash", 1, "error")])
        block = e.result()
    rows = [("".join(c.char for c in block.row(y)), block.row(y)) for y in range(block.height)]
    read_lead = next(r[0] for r, _ in rows if "Read" in r)
    bash_lead = next(r[0] for r, _ in rows if "Bash" in r)
    assert read_lead == "+"  # ✓ pass
    assert bash_lead == "x"  # ✗ fail
