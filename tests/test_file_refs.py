"""Tests for siftd.api.file_refs — file reference extraction from tool calls."""

import pytest

from siftd.api.file_refs import _extract_file_content, _strip_line_numbers


def test_strip_line_numbers():
    assert "→" not in _strip_line_numbers("     1→line one\n   123→line two")
    assert _strip_line_numbers("plain text") == "plain text"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, None),
        ('"just a string"', None),
        ('{"other": "data"}', None),
        ('{"content": "hello world"}', "hello world"),
        ('{"output": "hello"}', "hello"),
        ('{"content": []}', None),
        ('{"content": 42}', None),
    ],
)
def test_extract_file_content_basics(payload, expected):
    assert _extract_file_content(payload) == expected


def test_extract_file_content_list_blocks():
    out = _extract_file_content('{"content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]}')
    assert "line1" in out and "line2" in out
    assert "line1" in _extract_file_content('{"content": ["line1", "line2"]}')


def test_fetch_file_refs_empty_ids(test_db):
    from siftd.api.file_refs import fetch_file_refs
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db, read_only=True)
    assert fetch_file_refs(conn, []) == {}
    conn.close()


def test_fetch_file_refs_skips_rows_without_file_path():
    from siftd.api.file_refs import fetch_file_refs

    class _Cursor:
        def fetchall(self):
            return [{"prompt_id": "p1", "tool_name": "file.read", "input_json": "{}", "result_json": "{}"}]

    class _Conn:
        def execute(self, *_a, **_k):
            return _Cursor()

    assert fetch_file_refs(_Conn(), ["p1"]) == {}
