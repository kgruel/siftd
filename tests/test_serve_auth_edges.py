import asyncio
import time
from types import SimpleNamespace

import pytest
from litestar.exceptions import NotAuthorizedException

from siftd.serve.auth import UserIdentity, create_auth_middleware


def _run(coro):
    return asyncio.run(coro)


def test_authenticate_request_allows_no_auth_opt_route():
    MW = create_auth_middleware({})
    mw = object.__new__(MW)
    conn = SimpleNamespace(scope={"route_handler": SimpleNamespace(opt={"no_auth": True})}, headers={})
    out = _run(mw.authenticate_request(conn))
    assert out.user.sub == "anonymous"


def test_authenticate_request_missing_bearer_token_raises():
    MW = create_auth_middleware({"issuer": "https://idp"})
    mw = object.__new__(MW)
    conn = SimpleNamespace(scope={}, headers={})
    with pytest.raises(NotAuthorizedException, match="Missing bearer token"):
        _run(mw.authenticate_request(conn))


def test_authenticate_request_no_mode_configured_raises():
    MW = create_auth_middleware({})
    mw = object.__new__(MW)
    conn = SimpleNamespace(scope={}, headers={"authorization": "Bearer t"})
    with pytest.raises(NotAuthorizedException, match="No auth mode configured"):
        _run(mw.authenticate_request(conn))


def test_validate_introspection_cache_hit_returns_identity():
    MW = create_auth_middleware({"introspection_url": "https://idp/introspect"})
    MW._introspection_cache = {"tok": ({"username": "alice"}, time.time())}
    mw = object.__new__(MW)
    out = _run(mw._validate_introspection("tok"))
    assert isinstance(out, UserIdentity)
    assert out.sub == "alice"


def test_get_jwks_cache_hit_returns_cached_value():
    MW = create_auth_middleware({"issuer": "https://issuer"})
    MW._jwks_cache = {"k": 1}
    MW._jwks_fetched_at = time.time()
    mw = object.__new__(MW)
    assert _run(mw._get_jwks()) == {"k": 1}
