"""Client-side auth wiring in serve/client.py.

Covers the device-code precedence tier in _resolve_bearer_token and the gated
reactive 401 retry in _send_authed (which backs _get_json/_post_json).
"""

from __future__ import annotations

import pytest

from siftd.serve import client
from siftd.serve.client import ServeRequest4xx


class TestResolveBearerPrecedence:
    def test_env_beats_device_code(self, monkeypatch):
        monkeypatch.setenv("SIFTD_SERVE_TOKEN", "env-tok")
        monkeypatch.setattr(client, "_configured_issuer", lambda: "https://idp.test")
        monkeypatch.setattr(
            "siftd.credentials.resolve_live_bearer",
            lambda issuer: pytest.fail("env should win before device-code"),
        )
        assert client._resolve_bearer_token() == "env-tok"

    def test_device_code_beats_static_config(self, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_TOKEN", raising=False)
        monkeypatch.delenv("SIFTD_SERVE_DELEGATION_TOKEN", raising=False)
        monkeypatch.setattr(client, "_configured_issuer", lambda: "https://idp.test")
        monkeypatch.setattr("siftd.credentials.resolve_live_bearer", lambda issuer: "dev-tok")
        monkeypatch.setattr("siftd.config.get_config", lambda k: "static-tok")
        assert client._resolve_bearer_token() == "dev-tok"

    def test_falls_through_to_static_when_no_device_credential(self, monkeypatch):
        monkeypatch.delenv("SIFTD_SERVE_TOKEN", raising=False)
        monkeypatch.delenv("SIFTD_SERVE_DELEGATION_TOKEN", raising=False)
        monkeypatch.setattr(client, "_configured_issuer", lambda: "https://idp.test")
        monkeypatch.setattr("siftd.credentials.resolve_live_bearer", lambda issuer: None)
        monkeypatch.setattr(
            "siftd.config.get_config",
            lambda k: "static-tok" if k == "serve.auth.delegation_token" else None,
        )
        assert client._resolve_bearer_token() == "static-tok"


class _FakeSend:
    """Records (method, full_path, Authorization) per call; returns scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, target, method, full_path, headers, body, timeout_s):
        self.calls.append((method, full_path, headers.get("Authorization")))
        return self.responses.pop(0)


class TestSendAuthedRetry:
    def test_static_token_user_no_retry_on_401(self, monkeypatch):
        """No [auth].issuer → a 401 propagates as ServeRequest4xx with NO retry."""
        monkeypatch.setattr(client, "_resolve_bearer_token", lambda: "static-tok")
        monkeypatch.setattr(client, "_configured_issuer", lambda: None)
        fake = _FakeSend([(401, b'{"error": "Unauthorized"}')])
        monkeypatch.setattr(client, "_send", fake)

        with pytest.raises(ServeRequest4xx) as info:
            client._get_json("http://127.0.0.1:8484", "/api/v1/search")
        assert info.value.status == 401
        assert len(fake.calls) == 1  # no retry

    def test_retry_with_refreshed_token_on_401(self, monkeypatch):
        monkeypatch.setattr(client, "_resolve_bearer_token", lambda: "old-tok")
        monkeypatch.setattr(client, "_configured_issuer", lambda: "https://idp.test")
        monkeypatch.setattr(
            "siftd.credentials.refresh_after_rejection",
            lambda issuer, rejected_token: "new-tok",
        )
        fake = _FakeSend([(401, b'{"error": "Unauthorized"}'), (200, b'{"ok": true}')])
        monkeypatch.setattr(client, "_send", fake)

        assert client._get_json("http://127.0.0.1:8484", "/api/v1/search") == {"ok": True}
        assert len(fake.calls) == 2
        assert fake.calls[0][2] == "Bearer old-tok"
        assert fake.calls[1][2] == "Bearer new-tok"  # retried with refreshed token

    def test_refresh_returns_none_propagates_401(self, monkeypatch):
        monkeypatch.setattr(client, "_resolve_bearer_token", lambda: "old-tok")
        monkeypatch.setattr(client, "_configured_issuer", lambda: "https://idp.test")
        monkeypatch.setattr(
            "siftd.credentials.refresh_after_rejection",
            lambda issuer, rejected_token: None,
        )
        fake = _FakeSend([(401, b'{"error": "Unauthorized"}')])
        monkeypatch.setattr(client, "_send", fake)

        with pytest.raises(ServeRequest4xx) as info:
            client._get_json("http://127.0.0.1:8484", "/api/v1/search")
        assert info.value.status == 401
        assert len(fake.calls) == 1  # refresh produced nothing new → no retry

    def test_post_json_retries_and_preserves_body(self, monkeypatch):
        monkeypatch.setattr(client, "_resolve_bearer_token", lambda: "old-tok")
        monkeypatch.setattr(client, "_configured_issuer", lambda: "https://idp.test")
        monkeypatch.setattr(
            "siftd.credentials.refresh_after_rejection",
            lambda issuer, rejected_token: "new-tok",
        )
        fake = _FakeSend([(401, b'{"error": "x"}'), (201, b'{"created": true}')])
        monkeypatch.setattr(client, "_send", fake)

        assert client._post_json("http://127.0.0.1:8484", "/api/v1/tag", body={"x": 1}) == {"created": True}
        assert len(fake.calls) == 2
        assert fake.calls[1][2] == "Bearer new-tok"
