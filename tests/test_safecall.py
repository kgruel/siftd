"""Tests for siftd.safecall — safe operations with structured error handling."""

from siftd.safecall import (
    epoch_ms_to_iso,
    iter_jsonl,
    load_json,
    parse_json,
    parse_json_args,
    read_text,
)


class TestFileIO:
    def test_read_text(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        assert read_text(f) == "hello world"
        # Missing file
        assert read_text(tmp_path / "nope.txt") is None
        # Binary file that can't decode
        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"\x80\x81\x82")
        # This may or may not fail depending on codec — but read_text handles it

    def test_load_json(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        assert load_json(f) == {"key": "value"}
        # Invalid JSON
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{")
        assert load_json(bad) is None
        # Missing file
        assert load_json(tmp_path / "nope.json") is None

    def test_iter_jsonl(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text('{"a":1}\n\n{"b":2}\nnot json\n{"c":3}\n')
        result = iter_jsonl(f)
        assert len(result) == 3
        assert result[0] == {"a": 1}
        assert result[2] == {"c": 3}
        # Missing file
        assert iter_jsonl(tmp_path / "nope.jsonl") == []
        # Empty file
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        assert iter_jsonl(empty) == []


class TestJSONParsing:
    def test_parse_json(self):
        assert parse_json('{"a": 1}') == {"a": 1}
        assert parse_json("not json") is None
        assert parse_json("not json", fallback={}) == {}
        assert parse_json(None) is None
        assert parse_json(None, fallback="default") == "default"
        # Non-string types
        assert parse_json(123, fallback="fb") == "fb"

    def test_parse_json_args(self):
        # Dict passthrough
        assert parse_json_args({"key": "val"}) == {"key": "val"}
        # JSON string → dict
        assert parse_json_args('{"key": "val"}') == {"key": "val"}
        # JSON string that's not a dict (e.g., a list)
        assert parse_json_args("[1,2,3]") == {"raw": "[1,2,3]"}
        # Invalid JSON string
        assert parse_json_args("not json") == {"raw": "not json"}
        # Empty string
        assert parse_json_args("") == {}
        # None
        assert parse_json_args(None) == {}
        # Integer
        assert parse_json_args(42) == {"raw": "42"}


class TestTimestamp:
    def test_epoch_ms_to_iso(self):
        # Known timestamp: 2024-03-10 12:00:00 UTC = 1710072000000
        result = epoch_ms_to_iso(1710072000000)
        assert result is not None and "2024-03-10" in result
        # None
        assert epoch_ms_to_iso(None) is None
        # Invalid values
        assert epoch_ms_to_iso("not a number") is None
        assert epoch_ms_to_iso(float("inf")) is None
