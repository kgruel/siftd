"""Browser auth-code+PKCE login surface.

The browser is a client like the CLI: GET /auth/config advertises the PUBLIC
OIDC params it needs to acquire a token, and the served shell loads the
client-side flow from /static/auth.js. The server stays a pure validator —
these tests assert the advertisement contract and that the shell wires the
external script, not the flow itself (that runs in a browser).
"""

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.testing import TestClient

from siftd.serve import app as serve_app
from siftd.storage.sqlite import create_database


def _client(tmp_path, auth_config):
    db = tmp_path / "team.db"
    create_database(db)
    app = serve_app.create_app(db_path=db, auth_config=auth_config)
    return TestClient(app, raise_server_exceptions=False)


def test_auth_config_is_public(tmp_path):
    """/auth/config must be reachable without a bearer (it bootstraps login)."""
    with _client(tmp_path, {"issuer": "https://idp.example.com", "browser_client_id": "pub"}) as c:
        assert c.get("/auth/config").status_code == 200


def test_auth_config_disabled_when_no_auth(tmp_path):
    with _client(tmp_path, None) as c:
        assert c.get("/auth/config").json() == {"enabled": False}


def test_auth_config_disabled_in_static_token_mode(tmp_path):
    """static_token deployments have no browser SSO — UI falls back to paste."""
    with _client(tmp_path, {"static_token": "secret"}) as c:
        assert c.get("/auth/config").json() == {"enabled": False}


def test_auth_config_disabled_without_browser_client_id(tmp_path):
    """Issuer mode alone is not enough; the operator must opt in a public client."""
    with _client(tmp_path, {"issuer": "https://idp.example.com", "audience": "siftd"}) as c:
        assert c.get("/auth/config").json() == {"enabled": False}


def test_auth_config_enabled_advertises_public_params(tmp_path):
    cfg = {"issuer": "https://idp.example.com/app/o/siftd/", "browser_client_id": "public-client"}
    with _client(tmp_path, cfg) as c:
        body = c.get("/auth/config").json()
    assert body["enabled"] is True
    # issuer is normalized (trailing slash stripped) so the browser builds a
    # consistent .well-known URL.
    assert body["issuer"] == "https://idp.example.com/app/o/siftd"
    assert body["client_id"] == "public-client"
    # default scopes include offline_access so the browser gets a refresh token
    assert body["scope"] == "openid profile email offline_access"


def test_auth_config_honors_custom_browser_scopes(tmp_path):
    cfg = {
        "issuer": "https://idp.example.com",
        "browser_client_id": "pub",
        "browser_scopes": ["openid", "email"],
    }
    with _client(tmp_path, cfg) as c:
        assert c.get("/auth/config").json()["scope"] == "openid email"


def test_auth_config_never_leaks_secrets(tmp_path):
    """Only public params are advertised — no client_secret, no static_token."""
    cfg = {
        "issuer": "https://idp.example.com",
        "browser_client_id": "pub",
        "client_secret": "SHOULD-NOT-APPEAR",
        "static_token": "ALSO-SECRET",
    }
    with _client(tmp_path, cfg) as c:
        raw = c.get("/auth/config").text
    assert "SHOULD-NOT-APPEAR" not in raw
    assert "ALSO-SECRET" not in raw


def test_shell_loads_external_auth_script(tmp_path):
    """The shell references /static/auth.js and no longer inlines the token logic."""
    with _client(tmp_path, {"issuer": "https://idp.example.com", "browser_client_id": "pub"}) as c:
        shell = c.get("/").text
    # cache-busted with a ?v=<mtime> query (the static router ignores it)
    assert '<script src="/static/auth.js?v=' in shell
    # the old inline token bootstrap is gone (moved to the external file)
    assert "sessionStorage.getItem('siftd_token')" not in shell


def test_auth_js_is_served_and_csp_clean(tmp_path):
    """auth.js serves publicly and uses PKCE without any eval/new Function."""
    with _client(tmp_path, {"issuer": "https://idp.example.com", "browser_client_id": "pub"}) as c:
        resp = c.get("/static/auth.js")
    assert resp.status_code == 200
    js = resp.text
    assert "code_challenge_method" in js and "S256" in js
    # CSP without 'unsafe-eval' forbids these — guard against reintroduction.
    assert "eval(" not in js
    assert "new Function" not in js
