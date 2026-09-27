"""Frozen full-domain characterization of the Pi parser before seam extraction."""

import json
from pathlib import Path

import pytest

from siftd.adapters import pi_agent
from siftd.domain import ContentBlock, Conversation, Harness, Prompt, Response, Source, ToolCall, Usage

NOW = "2026-01-01T00:00:00Z"
HARNESS = Harness("pi_agent", "multi", "jsonl", "Pi Coding Agent")


def message(role, content, **kwargs):
    return {"type": "message", "message": {"role": role, "content": content, **kwargs}}


def text(value):
    return ContentBlock("text", {"text": value})


def call(key, name, arguments):
    return {"type": "toolCall", "id": key, "name": name, "arguments": arguments}


CASES = [
    ([], []),
    ([{"type": "ignored"}], [Conversation("pi_agent::fallback", HARNESS, NOW)]),
    ([
        {"type": "model_change", "modelId": "first", "timestamp": "2026-01-02T00:00:00Z"},
        {"type": "model_change", "modelId": "second"},
        {"type": "session", "id": "session", "cwd": "/fixture", "timestamp": "2026-01-03T00:00:00Z"},
        message("user", ["plain", {"type": "text", "text": "block"}, {"type": "image", "data": "opaque"}]),
        message("assistant", [{"type": "thinking", "thinking": "thought"}, {"type": "text", "text": "answer"}],
                usage={"input": 2, "output": 3, "cacheRead": 4, "cacheWrite": 5, "cost": {"total": 0.1}}),
        {"type": "message", "timestamp": "2026-01-04T00:00:00Z", "message": {"role": "assistant", "model": "override"}},
    ], [Conversation("pi_agent::session", HARNESS, "2026-01-03T00:00:00Z", [
        Prompt(NOW, [text("plain"), text("block"), ContentBlock("image", {"type": "image", "data": "opaque"})], [
            Response(NOW, [ContentBlock("thinking", {"text": "thought"}), text("answer")], usage=Usage(2, 3), model="first",
                     attributes={"cache_read_input_tokens": "4", "cache_creation_input_tokens": "5", "cost": "0.1"}),
            Response("2026-01-04T00:00:00Z", model="override"),
        ]),
    ], workspace_path="/fixture", ended_at="2026-01-04T00:00:00Z")]),
    ([
        message("assistant", [call("discarded", "early", {})]),
        message("toolResult", [{"type": "text", "text": "early result"}], toolCallId="discarded"),
        message("toolResult", [], toolCallId="unmatched"),
        message("user", ["question"]),
        message("assistant", [call("repeat", "overwritten", {}), call("repeat", "last", '{"x":1}'), call("pending", "later", [])]),
        message("toolResult", ["ignored", {"type": "image"}, {"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                toolCallId="repeat", toolName="ignored-alias", isError=True),
    ], [Conversation("pi_agent::fallback", HARNESS, NOW, [Prompt(NOW, [text("question")], [
        Response(NOW, [
            ContentBlock("tool_use", {"id": "repeat", "name": "overwritten", "input": {}}),
            ContentBlock("tool_use", {"id": "repeat", "name": "last", "input": {"x": 1}}),
            ContentBlock("tool_use", {"id": "pending", "name": "later", "input": {}}),
        ], [ToolCall("last", {"x": 1}, {"output": "a\nb"}, "error", "repeat", NOW),
            ToolCall("later", {}, external_id="pending")]),
    ])])]),
]


@pytest.mark.parametrize("records,expected", CASES)
def test_frozen_full_domain(tmp_path, monkeypatch, records, expected):
    monkeypatch.setattr(pi_agent, "now_iso", lambda: NOW)
    path = tmp_path / "fallback.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert list(pi_agent.parse(Source("file", path))) == expected
    assert list(pi_agent._parse_records(records, path)) == expected


def test_module_loader_is_the_parse_boundary(monkeypatch):
    monkeypatch.setattr(pi_agent, "now_iso", lambda: NOW)
    monkeypatch.setattr(pi_agent, "load_jsonl", lambda path: [{"type": "ignored"}])
    assert list(pi_agent.parse(Source("file", Path("fallback.jsonl")))) == [Conversation("pi_agent::fallback", HARNESS, NOW)]
