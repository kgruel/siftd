"""Tests for token acquisition from remote config."""

import pytest

from siftd.api.auth import AuthError, acquire_token


class TestAcquireToken:
    def test_token_command(self):
        auth = {"token_command": "echo test-token-123"}
        token = acquire_token(auth)
        assert token == "test-token-123"

    def test_token_env_var(self, monkeypatch):
        monkeypatch.setenv("SIFTD_TOKEN", "env-token-456")
        auth = {"token": "env:SIFTD_TOKEN"}
        token = acquire_token(auth)
        assert token == "env-token-456"

    def test_token_file(self, tmp_path):
        token_file = tmp_path / "token.txt"
        token_file.write_text("file-token-789\n")
        auth = {"token": f"file:{token_file}"}
        token = acquire_token(auth)
        assert token == "file-token-789"

    def test_no_auth_config_raises(self):
        with pytest.raises(AuthError, match="no auth configured"):
            acquire_token({})

    def test_none_auth_raises(self):
        with pytest.raises(AuthError, match="no auth configured"):
            acquire_token(None)
