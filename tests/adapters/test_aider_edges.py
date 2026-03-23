from pathlib import Path

from siftd.adapters import aider
from siftd.domain.source import Source


def test_can_handle_analytics_jsonl_under_default_location(monkeypatch, tmp_path):
    root = tmp_path / "aider-home"
    p = root / "analytics.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"event":"x"}\n')

    monkeypatch.setattr(aider, "DEFAULT_LOCATIONS", [str(root).lower()])

    assert aider.can_handle(Source(kind="file", location=Path(p)))
