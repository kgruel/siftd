"""Tests for serve/delegation.py — generalized CLI-to-serve delegation policy."""

import json
import os
from unittest.mock import patch

from siftd.serve.delegation import (
    can_delegate,
    delegation_enabled,
    is_loopback_url,
    resolve_serve_url,
    try_delegate,
)


class TestDelegationEnabled:
    """delegation_enabled() checks env and config."""

    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_DELEGATE", raising=False)
        assert delegation_enabled() is True

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("SIFTD_SERVE_DELEGATE", "false")
        assert delegation_enabled() is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("SIFTD_SERVE_DELEGATE", "true")
        assert delegation_enabled() is True

    def test_disabled_via_serve_delegate_config(self, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_DELEGATE", raising=False)

        def fake_get(key):
            if key == "serve.delegate":
                return "false"
            return None

        with patch("siftd.config.get_config", side_effect=fake_get):
            assert delegation_enabled() is False

    def test_defaults_true_when_no_config(self, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_DELEGATE", raising=False)
        with patch("siftd.config.get_config") as mock_cfg:
            mock_cfg.return_value = None
            assert delegation_enabled() is True


class TestIsLoopbackUrl:
    """is_loopback_url identifies loopback addresses."""

    def test_localhost(self):
        assert is_loopback_url("http://localhost:8484") is True

    def test_127(self):
        assert is_loopback_url("http://127.0.0.1:8484") is True

    def test_ipv6_loopback(self):
        assert is_loopback_url("http://[::1]:8484") is True

    def test_remote_host(self):
        assert is_loopback_url("http://example.com:8484") is False

    def test_invalid_url(self):
        assert is_loopback_url("not a url") is False


class TestResolveServeUrl:
    """resolve_serve_url follows the correct precedence chain."""

    def test_env_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("SIFTD_SERVE_URL", "http://custom:9999")
        url, explicit = resolve_serve_url()
        assert url == "http://custom:9999"
        assert explicit is True

    def test_default_localhost(self, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)
        with patch("siftd.config.get_config", return_value=None):
            url, explicit = resolve_serve_url()
            assert url == "http://127.0.0.1:8484"
            assert explicit is False

    def test_state_file_discovery(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)
        with patch("siftd.config.get_config", return_value=None):
            state_file = tmp_path / "serve.json"
            state_file.write_text(json.dumps({
                "pid": os.getpid(),  # Current process — guaranteed alive
                "port": 9876,
                "db_path": "/tmp/test.db",
            }))
            with patch("siftd.paths.state_dir", return_value=tmp_path):
                url, explicit = resolve_serve_url()
                assert url == "http://127.0.0.1:9876"
                assert explicit is False

    def test_config_port_overrides_state_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)

        state_file = tmp_path / "serve.json"
        state_file.write_text(json.dumps({
            "pid": os.getpid(),
            "port": 9876,
            "db_path": "/tmp/test.db",
        }))

        def fake_get(key):
            if key == "serve.port":
                return "7777"
            return None

        with patch("siftd.config.get_config", side_effect=fake_get):
            url, explicit = resolve_serve_url()
            assert url == "http://127.0.0.1:7777"
            assert explicit is False


class TestCanDelegate:
    """can_delegate applies policy guards."""

    def test_disabled_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SIFTD_SERVE_DELEGATE", "false")
        assert can_delegate(db=tmp_path / "test.db") is False

    def test_enabled_loopback_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_DELEGATE", raising=False)
        monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)
        with patch("siftd.config.get_config", return_value=None):
            assert can_delegate(db=tmp_path / "test.db") is True


class TestTryDelegate:
    """try_delegate returns None on any failure."""

    def test_returns_none_when_serve_down(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_DELEGATE", raising=False)
        monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)
        with patch("siftd.config.get_config", return_value=None):
            result = try_delegate("/api/v1/stats", db=tmp_path / "test.db")
            assert result is None

    def test_returns_none_when_db_path_mismatch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_DELEGATE", raising=False)
        monkeypatch.delenv("SIFTD_SERVE_URL", raising=False)
        with patch("siftd.config.get_config", return_value=None):
            with patch("siftd.serve.client.probe_health", return_value={
                "service": "siftd",
                "status": "ok",
                "db_path": "/wrong/path.db",
            }):
                db = tmp_path / "test.db"
                db.touch()
                result = try_delegate("/api/v1/stats", db=db)
                assert result is None

    def test_returns_none_when_delegation_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SIFTD_SERVE_DELEGATE", "false")
        result = try_delegate("/api/v1/stats", db=tmp_path / "test.db")
        assert result is None
