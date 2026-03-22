import pytest

from siftd.output import format_registry as reg


def test_select_format_fallback_and_empty_registry(monkeypatch):
    monkeypatch.setattr(reg, "get_format", lambda _name: None)
    monkeypatch.setattr(reg, "_ensure_loaded", lambda: {"markdown": object(), "json": object()})
    assert reg.select_format(is_tty=True)

    monkeypatch.setattr(reg, "_ensure_loaded", lambda: {})
    with pytest.raises(ValueError, match="No output formats available"):
        reg.select_format(is_tty=True)
