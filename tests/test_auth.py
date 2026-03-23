"""Tests for siftd.api.auth — token acquisition."""

import subprocess

import pytest

from siftd.api.auth import AuthError, acquire_token


class TestAcquireToken:
    def test_no_auth(self):
        with pytest.raises(AuthError, match="no auth"):
            acquire_token(None)

    def test_token_command_success(self):
        assert acquire_token({"token_command": "echo secret123"}) == "secret123"

    def test_token_command_failure(self):
        with pytest.raises(AuthError, match="failed"):
            acquire_token({"token_command": "exit 1"})

    def test_token_command_timeout(self, monkeypatch):
        def _timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="sleep 999", timeout=30)

        monkeypatch.setattr("subprocess.run", _timeout)
        with pytest.raises(AuthError, match="timed out"):
            acquire_token({"token_command": "sleep 999"})

    def test_env_token(self, monkeypatch):
        monkeypatch.setenv("SIFTD_TEST_TOKEN", "env_secret")
        assert acquire_token({"token": "env:SIFTD_TEST_TOKEN"}) == "env_secret"

    def test_env_token_missing(self, monkeypatch):
        monkeypatch.delenv("SIFTD_TEST_MISSING", raising=False)
        with pytest.raises(AuthError, match="not set"):
            acquire_token({"token": "env:SIFTD_TEST_MISSING"})

    def test_file_token(self, tmp_path):
        (tmp_path / "t.txt").write_text("file_secret\n")
        assert acquire_token({"token": f"file:{tmp_path / 't.txt'}"}) == "file_secret"

    def test_file_token_missing(self):
        with pytest.raises(AuthError, match="not found"):
            acquire_token({"token": "file:/nonexistent/token.txt"})

    def test_literal_token(self):
        assert acquire_token({"token": "literal_secret"}) == "literal_secret"

    def test_no_token_or_command(self):
        with pytest.raises(AuthError, match="no auth"):
            acquire_token({"other_key": "value"})
