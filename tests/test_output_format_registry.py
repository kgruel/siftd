"""Focused tests for output format registry fallback branches."""

from siftd.output import format_registry as reg


def test_select_format_falls_back_to_first_available(monkeypatch):
    monkeypatch.setattr(reg, "get_format", lambda name: None)
    monkeypatch.setattr(reg, "_ensure_loaded", lambda: {"markdown": object(), "json": object()})

    fmt = reg.select_format(is_tty=True)
    assert fmt is not None


def test_select_format_raises_when_no_formats(monkeypatch):
    monkeypatch.setattr(reg, "get_format", lambda name: None)
    monkeypatch.setattr(reg, "_ensure_loaded", lambda: {})

    try:
        reg.select_format(is_tty=True)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "No output formats available" in str(exc)
