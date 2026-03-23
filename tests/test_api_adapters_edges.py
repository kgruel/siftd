from siftd.api import adapters as api_adapters


def test_list_builtin_adapters_contains_expected_keys():
    out = api_adapters.list_builtin_adapters()
    assert "aider" in out and "claude_code" in out and "codex_cli" in out and "gemini_cli" in out
