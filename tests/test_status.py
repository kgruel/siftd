"""Contract tests for the status vocabulary (output/status.py).

Under capsys the captured streams are not TTYs, so status emits ASCII glyphs and
no ANSI — deterministic to assert on. The Unicode/color path is exercised
separately with a fake-TTY stream.
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

from siftd.output import status


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_confirm_goes_to_stdout_with_ascii_glyph(capsys):
    status.confirm("Imported 412 files")
    out, err = capsys.readouterr()
    assert "+ Imported 412 files" in out
    assert err == ""
    assert "\x1b[" not in out  # color stripped for a non-TTY stream


def test_error_goes_to_stderr(capsys):
    status.error("Database not found")
    out, err = capsys.readouterr()
    assert out == ""
    assert "x Database not found" in err


def test_info_and_warning_go_to_stderr(capsys):
    status.info("No results for: error")
    status.warning("adapter failed")
    out, err = capsys.readouterr()
    assert out == ""
    assert "i No results for: error" in err
    assert "! adapter failed" in err


def test_hint_renders_as_arrow_continuation(capsys):
    status.error("Database not found", hint="Run 'siftd ingest'")
    _, err = capsys.readouterr()
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert "x Database not found" in lines[0]
    assert "-> Run 'siftd ingest'" in lines[1]


def test_detail_renders_as_continuation(capsys):
    status.error("Failed", detail="permission denied")
    _, err = capsys.readouterr()
    assert "permission denied" in err


def test_db_missing_is_the_couplet(capsys):
    status.db_missing("/tmp/x.db")
    _, err = capsys.readouterr()
    assert "Database not found: /tmp/x.db" in err
    assert "Run 'siftd ingest'" in err


def test_caveats_map_severity_to_glyph(capsys):
    items = [
        SimpleNamespace(severity="warning", message="deprecated flag", fix_command=None),
        SimpleNamespace(severity="info", message="indexing pending", fix_command="siftd ingest"),
    ]
    status.caveats(items)
    _, err = capsys.readouterr()
    assert "! deprecated flag" in err
    assert "i indexing pending" in err
    assert "-> siftd ingest" in err


def test_stream_override_redirects(capsys):
    status.error("to stdout", stream=sys.stdout)
    out, err = capsys.readouterr()
    assert "x to stdout" in out
    assert err == ""


def test_unicode_glyph_and_color_on_a_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    # Force the Unicode path: status now gates on prefers_ascii (the named
    # isatty+encoding couplet), not a bare supports_unicode call.
    monkeypatch.setattr(status, "prefers_ascii", lambda stream=None: False)
    buf = _FakeTTY()
    status.confirm("ok", stream=buf)
    text = buf.getvalue()
    assert "✓" in text and "+" not in text  # Unicode glyph, not the ASCII fallback
    assert "ok" in text
    assert "\x1b[" in text  # color applied for a TTY stream


# --- severity_glyph / severity_mark (the severity-vocabulary primitives) ---


def test_severity_glyph_is_sourced_from_the_status_module():
    # severity_glyph now lives beside the callout severity vocabulary (it used to
    # live in doctor.view, forcing a cli -> doctor import). The contract holds.
    from siftd.output.status import severity_glyph

    assert severity_glyph("error") == ("✗", "error")
    assert severity_glyph(None) == ("✓", "success")  # pass / all-clear
    assert severity_glyph("error", as_ascii=True) == ("x", "error")
    assert severity_glyph("nonsense") == ("?", "muted")  # unknown → neutral, never all-clear


def test_severity_mark_resolves_the_glyph_to_a_palette_style():
    from painted import Style, use_theme

    from siftd.output.status import severity_mark
    from siftd.output.theme import siftd_theme

    with use_theme(siftd_theme):
        warn_glyph, warn_style = severity_mark("warning")
        ok_glyph, ok_style = severity_mark(None)
    assert warn_glyph == "⚠" and ok_glyph == "✓"
    assert isinstance(warn_style, Style) and isinstance(ok_style, Style)
    # Distinct roles (warning vs success) → distinct styles, so a glyph dropped
    # into a definitions value carries its own colour.
    assert warn_style != ok_style
    # ASCII degradation is forwarded to the glyph.
    assert severity_mark("warning", as_ascii=True)[0] == "!"
