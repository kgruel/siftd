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
    format_table,
    print_indented,
    print_refs_content,
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


# --- format_table ---


def test_format_table_alignment():
    result = format_table(["Name", "Val"], [["a", "long"], ["bb", "x"]])
    lines = result.split("\n")
    assert len(lines) == 4
    assert "Name" in lines[0]
    assert "---" in lines[1] or "──" in lines[1]


# --- print_indented ---


def test_print_indented(capsys):
    print_indented("line1\nline2\nline3")
    out = capsys.readouterr().out
    assert out == "  line1\n  line2\n  line3\n"


def test_print_indented_custom_prefix(capsys):
    print_indented("hello", indent=">> ")
    out = capsys.readouterr().out
    assert out == ">> hello\n"


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
    out = capsys.readouterr().out
    assert "No file references matching" in out
