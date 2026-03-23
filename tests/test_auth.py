"""Tests for siftd.api.auth — token acquisition."""

import subprocess

import pytest

from siftd.api.auth import AuthError, acquire_token


def test_command_success_and_timeout(monkeypatch):
    assert acquire_token({"token_command": "echo secret123"}) == "secret123"

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="sleep 999", timeout=30)),
    )
    with pytest.raises(AuthError, match="timed out"):
        acquire_token({"token_command": "sleep 999"})


@pytest.mark.parametrize(
    ("auth", "match"),
    [
        (None, "no auth"),
        ({"token_command": "exit 1"}, "failed"),
        ({"token": "env:SIFTD_TEST_MISSING"}, "not set"),
        ({"token": "file:/nonexistent/token.txt"}, "not found"),
        ({"other_key": "value"}, "no auth"),
    ],
)
def test_error_cases(auth, match, monkeypatch):
    monkeypatch.delenv("SIFTD_TEST_MISSING", raising=False)
    with pytest.raises(AuthError, match=match):
        acquire_token(auth)


def test_env_file_and_literal_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("SIFTD_TEST_TOKEN", "env_secret")
    assert acquire_token({"token": "env:SIFTD_TEST_TOKEN"}) == "env_secret"

    p = tmp_path / "t.txt"
    p.write_text("file_secret\n")
    assert acquire_token({"token": f"file:{p}"}) == "file_secret"

    assert acquire_token({"token": "literal_secret"}) == "literal_secret"
