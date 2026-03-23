from siftd.adapters import aider
from siftd.domain.source import Source


def test_can_handle_analytics_jsonl_under_default_location(monkeypatch, tmp_path):
    p = tmp_path / "aider-home/analytics.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"event":"x"}\n')
    monkeypatch.setattr(aider, "DEFAULT_LOCATIONS", [str(p.parent).lower()])
    assert aider.can_handle(Source(kind="file", location=p))
