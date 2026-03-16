"""Tests for the stdlib-only siftd-serve HTTP client."""


class TestParseTarget:
    def test_preserves_path_prefix(self):
        from siftd.serve.client import _parse_target

        target = _parse_target("https://example.com/siftd/")
        assert target.scheme == "https"
        assert target.host == "example.com"
        assert target.path_prefix == "/siftd"

    def test_empty_prefix(self):
        from siftd.serve.client import _parse_target

        target = _parse_target("http://127.0.0.1:8484")
        assert target.path_prefix == ""

