from pathlib import Path

from siftd.adapters import pi_agent
from siftd.domain.source import Source


def test_can_handle_rejects_non_jsonl_suffix():
    assert not pi_agent.can_handle(Source(kind="file", location=Path("/tmp/.pi/agent/sessions/s.log")))


def test_parse_sets_started_at_from_non_session_timestamp(monkeypatch):
    monkeypatch.setattr("siftd.adapters.pi_agent.load_jsonl", lambda _p: [
        {"type": "model_change", "timestamp": "2026-01-02T03:04:05", "modelId": "m"},
        {"type": "message", "timestamp": "2026-01-02T03:04:06", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message", "timestamp": "2026-01-02T03:04:07", "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "yo"}]}},
    ])
    conv = list(pi_agent.parse(Source(kind="file", location=Path("/tmp/s.jsonl"))))[0]
    assert conv.started_at == "2026-01-02T03:04:05"
