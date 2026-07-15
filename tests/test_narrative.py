"""Tests for siftd.serialization.narrative + conversations + json_fmt + format_registry."""

from types import SimpleNamespace as NS

from painted import Fidelity

from siftd.serialization.narrative import JsonEmitter, _collapse_tools, walk_narrative


def _fid(*, thinking=True, tools=True, chars=0):
    vis = {"text"}
    if thinking:
        vis.add("thinking")
    if tools:
        vis.add("tools")
    return Fidelity(visible=frozenset(vis), chars=chars)


def _block(bt, content=None, tool_calls=None):
    return NS(block_type=bt, content=content, tool_calls=tool_calls or [])


def _tc(name="tool", count=1, status=None, input=None, result=None):
    return NS(tool_name=name, count=count, status=status, input=input, result=result)


# --- _collapse_tools ---

def test_collapse_single():
    assert _collapse_tools([_tc("read")]) == [("read", 1, None)]


def test_collapse_dup():
    assert _collapse_tools([_tc("read"), _tc("read")]) == [("read", 2, None)]


def test_collapse_error():
    r = _collapse_tools([_tc("read", status="success"), _tc("read", status="error")])
    assert r == [("read", 2, "error")]


# --- walk_narrative ---

def test_tool_result_shown():
    e = JsonEmitter()
    walk_narrative([_block("tool_result", "out")], e, fidelity=_fid())
    assert any(b["type"] == "tool_result" for b in e.blocks)


def test_tool_output_shown():
    e = JsonEmitter()
    walk_narrative([_block("tool_output", "res")], e, fidelity=_fid())
    assert any(b["type"] == "tool_output" for b in e.blocks)


def test_tool_result_hidden():
    e = JsonEmitter()
    walk_narrative([_block("tool_result", "out")], e, fidelity=_fid(tools=False))
    assert not any(b.get("type") == "tool_result" for b in e.blocks)


def test_tool_summary():
    e = JsonEmitter()
    walk_narrative([_block("tool_calls", tool_calls=[_tc("r")])], e, fidelity=_fid(tools=False))
    assert any(b["type"] == "tool_calls" for b in e.blocks)


# --- conversations serialization ---

def test_summary_owner():
    from siftd.serialization.conversations import serialize_conversation_summary
    conv = NS(id="c1", started_at="d", model="m", workspace_path="/p",
              prompt_count=1, response_count=1, total_tokens=100, cost=0.01,
              tags=["t"], owner="alice")
    assert serialize_conversation_summary(conv)["owner"] == "alice"


def test_detail_default_fidelity():
    from siftd.serialization.conversations import serialize_conversation_detail
    d = NS(id="c1", started_at="d", ended_at="d", model="m",
           workspace_path="/p", summary="hi", exchange_count=1,
           total_tokens=50, source="cli", tags=[], turns=[])
    assert "turns" in serialize_conversation_detail(d)


def test_detail_fallback_tokens():
    from siftd.serialization.conversations import serialize_conversation_detail
    d = NS(id="c1", started_at="d", ended_at="d", model="m",
           workspace_path="/p", summary="hi", exchange_count=1,
           source="cli", tags=[], turns=[],
           total_input_tokens=30, total_output_tokens=20)
    assert serialize_conversation_detail(d)["total_tokens"] == 50


# --- json_fmt renderers ---

def test_json_fmt_stats(monkeypatch):
    from siftd.output.json_fmt import render_stats
    monkeypatch.setattr("siftd.serialization.stats.serialize_stats", lambda s: {"ok": 1})
    assert render_stats("x", Fidelity(depth=1)) == {"ok": 1}


# --- format_registry ---

def test_select_format():
    from siftd.output.format_registry import select_format
    assert select_format() is not None


# --- block_id threading (block action surface) ---

def test_walker_threads_block_id_to_json():
    """The walker forwards a source block's block_id (its event_content ULID)
    to text/thinking/tool_output; JsonEmitter emits it default-on so JSON
    consumers can address blocks (a block_id is a `siftd tag block <id>`
    target). Blocks without one — peek narratives, aggregates — omit the key."""
    e = JsonEmitter()
    walk_narrative(
        [
            NS(block_type="text", content="hi", tool_calls=[],
               event_id="01EVT", block_id="01BLKTEXT"),
            NS(block_type="thinking", content="hm", tool_calls=[],
               event_id="01EVT", block_id="01BLKTHINK"),
            NS(block_type="tool_result", content="out", tool_calls=[],
               event_id="01EVT", block_id="01BLKRES"),
            _block("text", "no id"),  # duck-typed block without block_id
        ],
        e, fidelity=_fid(),
    )
    by_content = {b.get("content"): b for b in e.blocks}
    assert by_content["hi"]["block_id"] == "01BLKTEXT"
    assert by_content["hm"]["block_id"] == "01BLKTHINK"
    assert by_content["out"]["block_id"] == "01BLKRES"
    assert "block_id" not in by_content["no id"]
