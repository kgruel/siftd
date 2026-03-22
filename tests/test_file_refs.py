"""Tests for siftd.api.file_refs — file reference extraction from tool calls."""

from siftd.api.file_refs import _extract_file_content, _strip_line_numbers


class TestStripLineNumbers:
    def test_strips_arrow_prefixes(self):
        text = "     1→line one\n   123→line two"
        result = _strip_line_numbers(text)
        assert "line one" in result and "line two" in result
        assert "→" not in result

    def test_no_prefixes_unchanged(self):
        assert _strip_line_numbers("plain text") == "plain text"


class TestExtractFileContent:
    def test_none_input(self):
        assert _extract_file_content(None) is None

    def test_non_dict_result(self):
        assert _extract_file_content('"just a string"') is None

    def test_no_content_key(self):
        assert _extract_file_content('{"other": "data"}') is None

    def test_string_content(self):
        assert _extract_file_content('{"content": "hello world"}') == "hello world"

    def test_output_key(self):
        assert _extract_file_content('{"output": "hello"}') == "hello"

    def test_list_content_text_blocks(self):
        result = _extract_file_content(
            '{"content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]}'
        )
        assert "line1" in result and "line2" in result

    def test_list_content_string_blocks(self):
        assert "line1" in _extract_file_content('{"content": ["line1", "line2"]}')

    def test_list_content_empty(self):
        assert _extract_file_content('{"content": []}') is None

    def test_non_string_content(self):
        assert _extract_file_content('{"content": 42}') is None


class TestFetchFileRefs:
    def test_empty_ids(self, test_db):
        from siftd.api.file_refs import fetch_file_refs
        from siftd.storage.sqlite import open_database

        conn = open_database(test_db, read_only=True)
        assert fetch_file_refs(conn, []) == {}
        conn.close()
