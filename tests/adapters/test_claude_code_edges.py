from siftd.adapters import claude_code


def test_normalize_content_unknown_type_returns_empty_list():
    assert claude_code._normalize_content(123) == []
