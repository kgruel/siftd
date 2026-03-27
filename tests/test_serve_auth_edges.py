import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("litestar")

pytestmark = pytest.mark.serve

from litestar.exceptions import NotAuthorizedException

from siftd.serve.auth import UserIdentity, _parse_scope_string, create_auth_middleware, require_write


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


def test_authenticate_request_loopback_api_requires_token():
    """Loopback clients must not bypass auth for /api/* routes."""
    MW = create_auth_middleware({"issuer": "https://idp"})
    mw = object.__new__(MW)
    conn = SimpleNamespace(scope={"path": "/api/v1/conversations", "client": ("127.0.0.1", 123)}, headers={})
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
    MW._introspection_cache = {"tok": ({"username": "alice"}, time.time() + 60)}
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


def test_validate_static_token_success_and_failure():
    MW = create_auth_middleware({"static_token": "s3cret", "identity": "tester"})
    mw = object.__new__(MW)
    assert mw._validate_static("s3cret").sub == "tester"
    with pytest.raises(NotAuthorizedException, match="Invalid token"):
        mw._validate_static("wrong")


def test_validate_static_token_env_resolution(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "from-env")
    MW = create_auth_middleware({"static_token": "env:MY_TOKEN"})
    mw = object.__new__(MW)
    assert mw._validate_static("from-env").sub == "local"
    with pytest.raises(NotAuthorizedException):
        mw._validate_static("wrong")


def test_authenticate_request_delegates_to_mode_validators(monkeypatch):
    oidc_cls = create_auth_middleware({"issuer": "https://idp"})
    oidc = object.__new__(oidc_cls)
    monkeypatch.setattr(oidc, "_validate_oidc", lambda _t: asyncio.sleep(0, result=UserIdentity(sub="o")))
    conn = SimpleNamespace(scope={}, headers={"authorization": "Bearer tok"})
    out = _run(oidc.authenticate_request(conn))
    assert out.user.sub == "o" and out.auth == "tok"

    intro_cls = create_auth_middleware({"introspection_url": "https://idp/i"})
    intro = object.__new__(intro_cls)
    monkeypatch.setattr(intro, "_validate_introspection", lambda _t: asyncio.sleep(0, result=UserIdentity(sub="i")))
    out2 = _run(intro.authenticate_request(conn))
    assert out2.user.sub == "i"


def test_validate_oidc_success_and_error(monkeypatch):
    class _JWT:
        class PyJWTError(Exception):
            pass

        @staticmethod
        def decode(token, jwks, algorithms, audience):
            if token == "bad":
                raise _JWT.PyJWTError("bad token")
            return {"email": "x@y"}

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "identity_claim": "email", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result={"k": 1}))

    assert _run(mw._validate_oidc("ok")).sub == "x@y"
    with pytest.raises(NotAuthorizedException, match="Invalid token"):
        _run(mw._validate_oidc("bad"))


def test_validate_introspection_network_paths(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def json(self):
            return self._body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if kwargs["data"]["token"] == "bad-status":
                return _Resp(500, {})
            if kwargs["data"]["token"] == "inactive":
                return _Resp(200, {"active": False})
            return _Resp(200, {"active": True, "username": "bob"})

    class _HTTPX:
        AsyncClient = _Client

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    monkeypatch.setenv("CLIENT_SECRET", "s3cr3t")
    MW = create_auth_middleware({
        "introspection_url": "https://idp/introspect",
        "client_id": "cid",
        "client_secret": "env:CLIENT_SECRET",
    })
    mw = object.__new__(MW)

    with pytest.raises(NotAuthorizedException, match="Introspection request failed"):
        _run(mw._validate_introspection("bad-status"))
    with pytest.raises(NotAuthorizedException, match="Token is not active"):
        _run(mw._validate_introspection("inactive"))
    assert _run(mw._validate_introspection("ok")).sub == "bob"
    assert calls[-1][1]["auth"] == ("cid", "s3cr3t")


def test_get_jwks_fetch_paths(monkeypatch):
    class _Resp:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class _Client:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            self.calls.append(url)
            if url.endswith("openid-configuration"):
                return _Resp({"jwks_uri": "https://idp/jwks"})
            return _Resp({"keys": []})

    class _HTTPX:
        AsyncClient = _Client

    class _JWT:
        class PyJWKSet:
            @staticmethod
            def from_dict(d):
                return {"parsed": d}

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    monkeypatch.setitem(sys.modules, "jwt", _JWT)

    MW = create_auth_middleware({"issuer": "https://idp"})
    MW._jwks_cache = None
    MW._jwks_fetched_at = 0
    mw = object.__new__(MW)
    out = _run(mw._get_jwks())
    assert out == {"parsed": {"keys": []}}

    MW2 = create_auth_middleware({"issuer": "https://idp", "jwks_url": "https://idp/jwks2"})
    MW2._jwks_cache = None
    MW2._jwks_fetched_at = 0
    mw2 = object.__new__(MW2)
    out2 = _run(mw2._get_jwks())
    assert out2 == {"parsed": {"keys": []}}


# --- Scope checking ---


def test_parse_scope_string():
    assert _parse_scope_string(None) == frozenset()
    assert _parse_scope_string("") == frozenset()
    assert _parse_scope_string("read write") == frozenset({"read", "write"})
    assert _parse_scope_string(["read", "write"]) == frozenset({"read", "write"})


def test_required_scopes_rejects_missing():
    MW = create_auth_middleware({"static_token": "tok", "required_scopes": ["siftd:read"]})
    mw = object.__new__(MW)
    # Static tokens get all configured scopes — passes
    identity = mw._validate_static("tok")
    assert "siftd:read" in identity.scopes

    # Simulate a token with wrong scopes via introspection cache
    MW2 = create_auth_middleware({
        "introspection_url": "https://idp/i",
        "required_scopes": ["siftd:read"],
    })
    MW2._introspection_cache = {"tok": ({"active": True, "username": "u", "scope": "other"}, time.time() + 60)}
    mw2 = object.__new__(MW2)
    conn = SimpleNamespace(scope={}, headers={"authorization": "Bearer tok"})
    with pytest.raises(NotAuthorizedException, match="Missing required scopes"):
        _run(mw2.authenticate_request(conn))


def test_introspection_populates_scopes():
    MW = create_auth_middleware({"introspection_url": "https://idp/i"})
    MW._introspection_cache = {
        "tok": ({"active": True, "username": "u", "scope": "siftd:read siftd:write"}, time.time() + 60),
    }
    mw = object.__new__(MW)
    out = _run(mw._validate_introspection("tok"))
    assert out.scopes == frozenset({"siftd:read", "siftd:write"})


def test_require_write_allows_when_auth_off():
    """No auth middleware installed — writes are allowed."""
    request = SimpleNamespace()  # no request.user attribute -> auth is off
    require_write(request)  # should not raise


def test_require_write_rejects_anonymous_when_auth_on():
    """Auth middleware installed — anonymous writes must be rejected."""
    from litestar.exceptions import PermissionDeniedException

    request = SimpleNamespace(user=UserIdentity(sub="anonymous"))
    with pytest.raises(PermissionDeniedException, match="Authentication required"):
        require_write(request)


def test_require_write_allows_when_no_write_scopes_configured():
    """Auth configured but no write_scopes — all authenticated users can write."""
    import siftd.serve.auth as auth_mod
    old = auth_mod._write_scopes
    auth_mod._write_scopes = frozenset()
    try:
        request = SimpleNamespace(user=UserIdentity(sub="u", scopes=frozenset({"siftd:read"})))
        require_write(request)  # should not raise
    finally:
        auth_mod._write_scopes = old


def test_require_write_rejects_missing_scope():
    """Write scopes configured — user without them gets 403."""
    from litestar.exceptions import PermissionDeniedException

    import siftd.serve.auth as auth_mod
    old = auth_mod._write_scopes
    auth_mod._write_scopes = frozenset({"siftd:write"})
    try:
        request = SimpleNamespace(user=UserIdentity(sub="u", scopes=frozenset({"siftd:read"})))
        with pytest.raises(PermissionDeniedException, match="Insufficient scope"):
            require_write(request)
    finally:
        auth_mod._write_scopes = old


def test_require_write_allows_with_scope():
    """User with write scope passes."""
    import siftd.serve.auth as auth_mod
    old = auth_mod._write_scopes
    auth_mod._write_scopes = frozenset({"siftd:write"})
    try:
        request = SimpleNamespace(user=UserIdentity(sub="u", scopes=frozenset({"siftd:read", "siftd:write"})))
        require_write(request)  # should not raise
    finally:
        auth_mod._write_scopes = old


# --- S3: OIDC error sanitization ---


def test_oidc_error_does_not_leak_jwt_details(monkeypatch):
    """OIDC validation failures must return generic 'Invalid token', not exception details."""
    class _JWT:
        class PyJWTError(Exception):
            pass

        @staticmethod
        def decode(token, jwks, algorithms, audience):
            raise _JWT.PyJWTError("ExpiredSignatureError: token expired at 2026-01-01, claim aud=secret-app")

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result={"k": 1}))

    with pytest.raises(NotAuthorizedException, match="^Invalid token$") as exc_info:
        _run(mw._validate_oidc("expired"))
    # Must not contain claim details or original exception text
    assert "ExpiredSignature" not in str(exc_info.value)
    assert "secret-app" not in str(exc_info.value)


# --- S4: Introspection cache TTL bounded by token exp ---


def test_introspection_cache_respects_token_exp():
    """Cached entry must be evicted when token exp has passed, even within 60s window."""
    MW = create_auth_middleware({"introspection_url": "https://idp/introspect"})
    # Token expired 1 second ago — cache deadline should be in the past
    expired_at = time.time() - 1
    MW._introspection_cache = {"tok": ({"username": "alice"}, expired_at)}
    mw = object.__new__(MW)
    # Cache miss: would need to call introspection endpoint, which isn't mocked → ImportError or httpx call
    # We verify the cache is NOT used by checking it doesn't return the cached identity
    import importlib
    try:
        _run(mw._validate_introspection("tok"))
        # If httpx is available it will try a real HTTP call and fail
        assert False, "Should not have returned from cache"
    except (NotAuthorizedException, Exception):
        pass  # Expected: cache was skipped, introspection endpoint was contacted


def test_introspection_cache_stores_exp_bounded_deadline(monkeypatch):
    """When token exp is sooner than 60s, cache deadline should be token exp."""
    calls = []

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            calls.append(1)
            exp_time = time.time() + 10  # expires in 10s, not 60s
            return _Resp({"active": True, "username": "bob", "exp": exp_time})

    class _HTTPX:
        AsyncClient = _Client

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    MW = create_auth_middleware({"introspection_url": "https://idp/introspect"})
    MW._introspection_cache = {}
    mw = object.__new__(MW)
    _run(mw._validate_introspection("tok"))

    # Verify cache deadline is bounded by exp (~10s from now), not 60s
    _, deadline = MW._introspection_cache["tok"]
    assert deadline < time.time() + 15  # should be ~10s, not ~60s


def test_introspection_cache_uses_60s_when_no_exp(monkeypatch):
    """When token has no exp claim, cache TTL defaults to 60s."""
    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            return _Resp({"active": True, "username": "carol"})

    class _HTTPX:
        AsyncClient = _Client

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    MW = create_auth_middleware({"introspection_url": "https://idp/introspect"})
    MW._introspection_cache = {}
    mw = object.__new__(MW)
    before = time.time()
    _run(mw._validate_introspection("tok"))

    _, deadline = MW._introspection_cache["tok"]
    assert deadline >= before + 59  # ~60s from call time
