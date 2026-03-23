import pytest

from siftd.api.file_refs import _extract_file_content, _strip_line_numbers


def test_strip_line_numbers_and_extract_content_lists():
    assert "→" not in _strip_line_numbers("     1→line one\n   123→line two")
    out = _extract_file_content('{"content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]}')
    assert "line1" in out and "line2" in out
    assert "line1" in _extract_file_content('{"content": ["line1", "line2"]}')


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('"just a string"', None),
        ('{"other": "data"}', None),
        ('{"content": "hello world"}', "hello world"),
        ('{"output": "hello"}', "hello"),
        ('{"content": []}', None),
        ('{"content": 42}', None),
        (None, None),
    ],
)
def test_extract_content_basics(payload, expected):
    assert _extract_file_content(payload) == expected


def test_fetch_file_refs_edges(test_db):
    from siftd.api.file_refs import fetch_file_refs
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db, read_only=True)
    assert fetch_file_refs(conn, []) == {}
    conn.close()

    class C:
        def execute(self, *_a, **_k):
            return type("R", (), {"fetchall": lambda self: [{"prompt_id": "p1", "tool_name": "file.read", "input_json": "{}", "result_json": "{}"}]})()

    assert fetch_file_refs(C(), ["p1"]) == {}