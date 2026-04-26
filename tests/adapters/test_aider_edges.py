from siftd.adapters import aider
from siftd.domain.source import Source


def test_can_handle_analytics_jsonl_not_handled(monkeypatch, tmp_path):
    """analytics.jsonl is no longer handled — discovery deferred until schema documented."""
    p = tmp_path / "aider-home/analytics.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text('{"event":"x"}\n')
    monkeypatch.setattr(aider, "DEFAULT_LOCATIONS", [str(p.parent).lower()])
    assert not aider.can_handle(Source(kind="file", location=p))
