"""Tests for siftd.output.common — pure formatting helpers."""

from dataclasses import dataclass
from datetime import timedelta, timezone

from siftd.output.common import (
    fmt_ago,
    fmt_model,
    fmt_timestamp,
    fmt_tokens,
    fmt_workspace,
    format_refs_annotation,
    prefers_ascii,
    print_refs_content,
    role_label,
    should_use_ansi,
    split_match_segments,
    supports_unicode,
    truncate_text,
)

# --- fmt_timestamp ---


def test_fmt_timestamp_converts_aware_timestamps_to_local_timezone():
    eastern = timezone(timedelta(hours=-5))
    assert fmt_timestamp("2024-01-15T10:00:00Z", local_tz=eastern) == "2024-01-15 05:00"
    assert fmt_timestamp("2024-01-15T10:00:00Z", time_only=True, local_tz=eastern) == "05:00"


def test_fmt_timestamp_leaves_naive_timestamps_unchanged():
    eastern = timezone(timedelta(hours=-5))
    assert fmt_timestamp("2024-01-15T10:00:00", local_tz=eastern) == "2024-01-15 10:00"


def test_fmt_timestamp_preserves_short_date_strings():
    assert fmt_timestamp("2024-01-15") == "2024-01-15"
    assert fmt_timestamp("2024-01-15", time_only=True) == ""


def test_fmt_timestamp_none_and_empty():
    assert fmt_timestamp(None) == ""
    assert fmt_timestamp("") == ""


def test_fmt_timestamp_invalid_iso_fallback():
    # iso_timestamp[:16].replace("T", " ") for invalid strings
    assert fmt_timestamp("not-a-timestamp-at-all") == "not-a-timestamp-"
    # time_only: iso_timestamp[11:16]
    assert fmt_timestamp("not-a-timestamp-at-all", time_only=True) == "tamp-"


# --- fmt_tokens ---


def test_fmt_tokens_formats_thousands():
    assert fmt_tokens(500) == "500"
    assert fmt_tokens(1234) == "1.2k"
    assert fmt_tokens(12345) == "12.3k"
    assert fmt_tokens(0) == "0"


def test_fmt_tokens_rolls_over_to_millions_and_billions():
    # 'k' must not run away on billion-scale corpora (post cache-fold).
    assert fmt_tokens(1_000_000) == "1.0M"
    assert fmt_tokens(1_500_000) == "1.5M"
    assert fmt_tokens(999_999_999) == "1000.0M"  # just under the B threshold
    assert fmt_tokens(1_000_000_000) == "1.0B"
    assert fmt_tokens(46_594_425_913) == "46.6B"


# --- fmt_workspace ---


def test_fmt_workspace():
    assert fmt_workspace(None) == ""
    assert fmt_workspace("/") == "(root)"
    assert fmt_workspace("") == "(root)"
    assert fmt_workspace("/home/user/my-project") == "my-project"


# --- fmt_ago ---


def test_fmt_ago():
    assert fmt_ago(30) == "just now"
    assert fmt_ago(120) == "2m ago"
    assert fmt_ago(3700) == "1h 1m ago"
    assert fmt_ago(7200) == "2h ago"


# --- fmt_model ---


def test_fmt_model():
    assert fmt_model(None) == ""
    assert fmt_model("") == ""
    assert fmt_model("claude-opus-4-5-20251101") == "claude-opus-4-5"
    assert fmt_model("gpt-4o") == "gpt-4o"
    assert fmt_model("claude-opus-4-5-20251101", strip_date=False) == "claude-opus-4-5-20251101"


# --- truncate_text ---


def test_truncate_text():
    assert truncate_text("hello world", 5) == "hello..."
    assert truncate_text("hi", 10) == "hi"
    assert truncate_text("hello", 0) == "hello"  # 0 means no truncation
    assert truncate_text("hello world", 5, suffix="~") == "hello~"


# --- supports_unicode ---


def test_supports_unicode(monkeypatch):
    class _Std:
        def __init__(self, encoding):
            self.encoding = encoding

    monkeypatch.setattr("sys.stdout", _Std("utf-8"))
    assert supports_unicode() is True

    monkeypatch.setattr("sys.stdout", _Std("ascii"))
    assert supports_unicode() is False

    # Missing/None encoding degrades to the ASCII assumption (False).
    monkeypatch.setattr("sys.stdout", _Std(None))
    assert supports_unicode() is False


# --- prefers_ascii ---


def test_prefers_ascii(monkeypatch):
    import io

    class _Std:
        def __init__(self, encoding, tty):
            self.encoding = encoding
            self._tty = tty

        def isatty(self):
            return self._tty

    # A non-TTY (a pipe) always prefers ASCII, even with a UTF-8 encoding.
    monkeypatch.setattr("sys.stdout", _Std("utf-8", False))
    assert prefers_ascii() is True

    # A UTF-8 TTY is the one case that earns the Unicode forms.
    monkeypatch.setattr("sys.stdout", _Std("utf-8", True))
    assert prefers_ascii() is False

    # A non-UTF-8 TTY (LANG=C) is a TTY but can't encode the glyphs → ASCII.
    monkeypatch.setattr("sys.stdout", _Std("ascii", True))
    assert prefers_ascii() is True

    # An explicit stream drives the isatty() check; encoding capability still
    # tracks sys.stdout (the established couplet this names).
    class _Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdout", _Std("utf-8", True))
    assert prefers_ascii(_Pipe()) is True


# --- should_use_ansi (NO_COLOR) ---


def test_should_use_ansi_honors_tty_and_no_color(monkeypatch):
    import io

    class _Std(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    monkeypatch.delenv("NO_COLOR", raising=False)
    # A TTY without NO_COLOR gets colour; a pipe never does.
    monkeypatch.setattr("sys.stdout", _Std(True))
    assert should_use_ansi() is True
    monkeypatch.setattr("sys.stdout", _Std(False))
    assert should_use_ansi() is False

    # NO_COLOR (non-empty, the widely-adopted reading) disables colour on a TTY —
    # painted itself never checks the var, so this is the lever that honours it.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr("sys.stdout", _Std(True))
    assert should_use_ansi() is False

    # An empty NO_COLOR does not disable (non-empty reading).
    monkeypatch.setenv("NO_COLOR", "")
    assert should_use_ansi() is True

    # An explicit stream drives the isatty() check.
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert should_use_ansi(_Std(False)) is False


# --- format_refs_annotation ---


@dataclass
class FakeRef:
    basename: str
    path: str
    op: str
    content: str | None = None


def test_format_refs_annotation_empty():
    assert format_refs_annotation([]) == ""


def test_format_refs_annotation_basic():
    refs = [FakeRef("a.py", "/a.py", "r"), FakeRef("b.py", "/b.py", "w")]
    result = format_refs_annotation(refs)
    assert result == "refs: a.py(r) b.py(w)"


def test_format_refs_annotation_dedup():
    refs = [
        FakeRef("a.py", "/a.py", "r"),
        FakeRef("a.py", "/other/a.py", "r"),  # same basename+op
        FakeRef("b.py", "/b.py", "w"),
    ]
    result = format_refs_annotation(refs)
    assert result == "refs: a.py(r) b.py(w)"


def test_format_refs_annotation_overflow():
    refs = [FakeRef(f"f{i}.py", f"/f{i}.py", "r") for i in range(8)]
    result = format_refs_annotation(refs, max_shown=3)
    assert "+5 more" in result
    assert "f0.py(r)" in result


# --- print_refs_content ---


def test_print_refs_content_empty(capsys):
    print_refs_content([])
    assert capsys.readouterr().out == ""


def test_print_refs_content_basic(capsys):
    refs = [FakeRef("main.py", "/src/main.py", "r", content="import os")]
    print_refs_content(refs)
    out = capsys.readouterr().out
    assert "main.py" in out
    assert "(read)" in out
    assert "import os" in out


def test_print_refs_content_no_content(capsys):
    refs = [FakeRef("empty.py", "/empty.py", "w", content=None)]
    print_refs_content(refs)
    out = capsys.readouterr().out
    assert "(no content available)" in out


def test_print_refs_content_dedup(capsys):
    refs = [
        FakeRef("a.py", "/a.py", "r", content="v1"),
        FakeRef("a.py", "/a.py", "r", content="v2"),  # same path+op, kept first
    ]
    print_refs_content(refs)
    out = capsys.readouterr().out
    assert out.count("[1]") == 1  # only one entry
    assert "v1" in out


def test_print_refs_content_filter(capsys):
    refs = [
        FakeRef("a.py", "/a.py", "r", content="aaa"),
        FakeRef("b.py", "/b.py", "w", content="bbb"),
    ]
    print_refs_content(refs, filter_basenames=["b.py"])
    out = capsys.readouterr().out
    assert "bbb" in out
    assert "aaa" not in out


def test_print_refs_content_filter_no_match(capsys):
    refs = [FakeRef("a.py", "/a.py", "r", content="aaa")]
    print_refs_content(refs, filter_basenames=["missing.py"])
    # The empty-state routes through status.info → stderr.
    assert "No file references matching" in capsys.readouterr().err


# --- split_match_segments (the painted-free FTS marker splitter) ---


def test_split_match_segments_balanced():
    assert split_match_segments("API >>>error<<<: 500") == [
        ("API ", False),
        ("error", True),
        (": 500", False),
    ]


def test_split_match_segments_multiple_and_adjacent():
    # Two spread matches, then two adjacent matches with no literal between.
    assert split_match_segments(">>>a<<< mid >>>b<<<>>>c<<<") == [
        ("a", True),
        (" mid ", False),
        ("b", True),
        ("c", True),
    ]


def test_split_match_segments_unbalanced_open_stays_literal():
    # A dangling open marker (e.g. snippet truncated mid-pair) is NOT consumed —
    # it stays literal text so real content '>>>' (a REPL prompt) is never eaten.
    assert split_match_segments("foo >>>bar baz") == [("foo >>>bar baz", False)]


def test_split_match_segments_no_markers_is_one_literal():
    assert split_match_segments("just text") == [("just text", False)]


def test_split_match_segments_empty_falls_back_to_one_literal():
    assert split_match_segments("") == [("", False)]


# --- role_label (casing + abbreviation, shared by detail + search) ---


def test_role_label_full_lowercases():
    assert role_label("USER") == "user"
    assert role_label("Assistant") == "assistant"
    assert role_label("user") == "user"


def test_role_label_abbrev_only_collapses_assistant():
    assert role_label("assistant", abbrev=True) == "asst"
    assert role_label("ASSISTANT", abbrev=True) == "asst"
    # user and everything else stay full even when abbreviated.
    assert role_label("user", abbrev=True) == "user"
    assert role_label("tool", abbrev=True) == "tool"


def test_role_label_unknown_role_passthrough():
    assert role_label("System") == "system"
    assert role_label("System", abbrev=True) == "system"
