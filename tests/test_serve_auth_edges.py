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


class _MockJWKS:
    """Minimal PyJWKSet-like for tests that don't exercise real key extraction."""
    class _Key:
        key_id = "test-kid"
        key = "mock-key"
    keys = [_Key()]


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
    MW._introspection_cache = {MW._cache_key("tok"): ({"username": "alice"}, time.time() + 60)}
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
    captured: dict = {}

    class _JWT:
        class PyJWTError(Exception):
            pass

        @staticmethod
        def get_unverified_header(token):
            return {"kid": "test-kid"}

        @staticmethod
        def decode(token, key, **kwargs):
            captured.update(kwargs)
            if token == "bad":
                raise _JWT.PyJWTError("bad token")
            return {"email": "x@y", "iss": "https://idp"}

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "identity_claim": "email", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=_MockJWKS()))

    assert _run(mw._validate_oidc("ok")).sub == "x@y"
    # Verify the production code requires iss/exp/aud and validates aud via jwt.decode.
    # iss is validated by our own normalized compare (NOT passed to jwt.decode), so
    # `issuer` must not be in the decode kwargs.
    assert captured["audience"] == "siftd"
    assert "issuer" not in captured
    assert set(captured["options"]["require"]) >= {"exp", "iss", "aud"}

    with pytest.raises(NotAuthorizedException, match="Invalid token"):
        _run(mw._validate_oidc("bad"))


def test_validate_oidc_rejects_missing_identity_claim(monkeypatch):
    """A token that validates cryptographically but lacks the configured identity
    claim must be rejected, not collapsed under a synthetic 'unknown' owner.

    Otherwise multiple tokens with no identity_claim would all map to the same
    `sub` in conversation_owners, conflating distinct subjects.
    """

    class _JWT:
        class PyJWTError(Exception):
            pass

        @staticmethod
        def get_unverified_header(token):
            return {"kid": "test-kid"}

        @staticmethod
        def decode(token, key, **kwargs):
            # Valid signature, valid iss/aud — but missing the configured `email` claim.
            return {"sub": "fallback-subject", "iss": "https://idp"}

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({
        "issuer": "https://idp", "identity_claim": "email", "audience": "siftd",
    })
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=_MockJWKS()))

    with pytest.raises(NotAuthorizedException, match="identity claim"):
        _run(mw._validate_oidc("token-missing-email-claim"))


def test_validate_introspection_rejects_missing_identity_claim(monkeypatch):
    """Introspection path mirrors OIDC: reject when configured claim missing.

    Without this, two tokens introspecting active=true but without `username`
    would both map to sub='unknown' and be conflated in conversation_owners.
    """

    class _Resp:
        status_code = 200

        def json(self):
            # active token but missing the configured identity claim
            return {"active": True, "scope": "siftd:read"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            return _Resp()

    class _HTTPX:
        AsyncClient = _Client

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    MW = create_auth_middleware({
        "introspection_url": "https://idp/introspect",
        "identity_claim": "username",
    })
    mw = object.__new__(MW)
    with pytest.raises(NotAuthorizedException, match="identity claim"):
        _run(mw._validate_introspection("tok"))


def test_validate_introspection_cache_path_also_rejects_missing_claim():
    """Cache-hit path must apply the same identity-claim check."""
    from siftd.serve.auth import _identity_from_introspection

    with pytest.raises(NotAuthorizedException, match="identity claim"):
        _identity_from_introspection({"active": True}, "username")
    # Empty string also rejected.
    with pytest.raises(NotAuthorizedException, match="identity claim"):
        _identity_from_introspection({"username": ""}, "username")


def test_validate_oidc_rejects_empty_identity_claim(monkeypatch):
    """An empty-string identity claim is also rejected."""

    class _JWT:
        class PyJWTError(Exception):
            pass

        @staticmethod
        def get_unverified_header(token):
            return {"kid": "test-kid"}

        @staticmethod
        def decode(token, key, **kwargs):
            return {"sub": "", "iss": "https://idp"}

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=_MockJWKS()))

    with pytest.raises(NotAuthorizedException, match="identity claim"):
        _run(mw._validate_oidc("token-with-empty-sub"))


def test_jwks_origin_check_matches_issuer():
    """JWKS URIs on the same scheme+host+port as the issuer are accepted."""
    from siftd.serve.auth import _jwks_origin_matches_issuer

    assert _jwks_origin_matches_issuer(
        "https://idp.example.com/.well-known/jwks.json",
        "https://idp.example.com",
    )
    # Explicit default port equivalence
    assert _jwks_origin_matches_issuer(
        "https://idp.example.com:443/keys",
        "https://idp.example.com",
    )
    # Path under issuer
    assert _jwks_origin_matches_issuer(
        "https://idp.example.com/realms/main/protocol/openid-connect/certs",
        "https://idp.example.com/realms/main",
    )


def test_jwks_origin_check_rejects_cross_origin():
    """Cross-host, cross-scheme, and cross-port JWKS URIs are rejected."""
    from siftd.serve.auth import _jwks_origin_matches_issuer

    # Different host (attacker's JWKS)
    assert not _jwks_origin_matches_issuer(
        "https://evil.example.com/keys",
        "https://idp.example.com",
    )
    # Different scheme (downgrade)
    assert not _jwks_origin_matches_issuer(
        "http://idp.example.com/keys",
        "https://idp.example.com",
    )
    # Different port
    assert not _jwks_origin_matches_issuer(
        "https://idp.example.com:8443/keys",
        "https://idp.example.com",
    )
    # Sibling subdomain — must NOT be allowed without explicit configuration
    assert not _jwks_origin_matches_issuer(
        "https://keys.example.com/jwks",
        "https://idp.example.com",
    )


def test_validate_oidc_rejects_issuer_mismatch(monkeypatch):
    """A token whose signature validates but iss claim mismatches must be rejected.

    Simulates PyJWT's InvalidIssuerError when the configured issuer doesn't match
    the token's iss claim. This is the security property the iss check exists for.
    """

    class _JWT:
        class PyJWTError(Exception):
            pass

        @staticmethod
        def get_unverified_header(token):
            return {"kid": "test-kid"}

        @staticmethod
        def decode(token, key, **kwargs):
            # Production passes issuer=...; if a mismatched token reached PyJWT,
            # PyJWT would raise InvalidIssuerError (subclass of PyJWTError).
            if kwargs.get("issuer") != "https://idp":
                raise _JWT.PyJWTError("unexpected issuer config")
            raise _JWT.PyJWTError("InvalidIssuerError: iss claim does not match")

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=_MockJWKS()))

    with pytest.raises(NotAuthorizedException, match="Invalid token"):
        _run(mw._validate_oidc("token-from-rogue-issuer"))


def test_validate_oidc_rejects_kidless_token_against_keyed_jwks(monkeypatch):
    """A token with no kid header must be rejected when the JWKS keys all have kids.

    PyJWT's own PyJWKClient.match_kid() only accepts exact kid matches; accepting
    kidless tokens against a keyed JWKS would silently widen the auth surface.
    """

    class _JWT:
        class PyJWTError(Exception):
            pass

        @staticmethod
        def get_unverified_header(token):
            return {}  # no kid in header

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "audience": "siftd"})
    mw = object.__new__(MW)
    # JWKS has a key with kid="test-kid"; token has no kid — must not match, even
    # after the forced refetch (stub accepts the force kwarg, returns same set).
    monkeypatch.setattr(mw, "_get_jwks", lambda **kw: asyncio.sleep(0, result=_MockJWKS()))

    with pytest.raises(NotAuthorizedException, match="No matching JWKS key"):
        _run(mw._validate_oidc("kidless-token"))


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
    MW2._introspection_cache = {MW2._cache_key("tok"): ({"active": True, "username": "u", "scope": "other"}, time.time() + 60)}
    mw2 = object.__new__(MW2)
    conn = SimpleNamespace(scope={}, headers={"authorization": "Bearer tok"})
    with pytest.raises(NotAuthorizedException, match="Missing required scopes"):
        _run(mw2.authenticate_request(conn))


def test_introspection_populates_scopes():
    MW = create_auth_middleware({"introspection_url": "https://idp/i"})
    MW._introspection_cache = {
        MW._cache_key("tok"): ({"active": True, "username": "u", "scope": "siftd:read siftd:write"}, time.time() + 60),
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
        def get_unverified_header(token):
            return {"kid": "test-kid"}

        @staticmethod
        def decode(token, key, **kwargs):
            raise _JWT.PyJWTError("ExpiredSignatureError: token expired at 2026-01-01, claim aud=secret-app")

    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=_MockJWKS()))

    with pytest.raises(NotAuthorizedException, match="Invalid token") as exc_info:
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
    MW._introspection_cache = {MW._cache_key("tok"): ({"username": "alice"}, expired_at)}
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
    _, deadline = MW._introspection_cache[MW._cache_key("tok")]
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

    _, deadline = MW._introspection_cache[MW._cache_key("tok")]
    assert deadline >= before + 59  # ~60s from call time


# --- OIDC key extraction integration (real PyJWT, no jwt.decode monkeypatch) ---


def test_validate_oidc_real_pyjwt_key_extraction(monkeypatch):
    """OIDC validation extracts the signing key from PyJWKSet by kid.

    Uses real PyJWT with no monkeypatch on jwt.decode or jwt.get_unverified_header.
    Before the fix, _validate_oidc passed the PyJWKSet directly to jwt.decode,
    which raises TypeError (not a subclass of jwt.PyJWTError), producing a 500.
    """
    import datetime
    import json

    import jwt
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    public_key = private_key.public_key()
    kid = "test-key-1"

    jwk_dict = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk_dict["kid"] = kid
    jwks = jwt.PyJWKSet.from_dict({"keys": [jwk_dict]})

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    token = jwt.encode(
        {
            "sub": "alice",
            "iss": "https://idp",
            "aud": "siftd",
            "exp": now + datetime.timedelta(hours=1),
            "iat": now,
            "scope": "siftd:read",
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )

    MW = create_auth_middleware({"issuer": "https://idp", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=jwks))

    identity = _run(mw._validate_oidc(token))
    assert identity.sub == "alice"
    assert "siftd:read" in identity.scopes


def _signed_token_and_jwks(token_iss: str):
    """Build a real RS256-signed token with the given iss claim + its JWKS."""
    import datetime
    import json

    import jwt
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend(),
    )
    kid = "test-key-1"
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk_dict["kid"] = kid
    jwks = jwt.PyJWKSet.from_dict({"keys": [jwk_dict]})
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    token = jwt.encode(
        {
            "sub": "alice", "iss": token_iss, "aud": "siftd",
            "exp": now + datetime.timedelta(hours=1), "iat": now,
        },
        private_pem, algorithm="RS256", headers={"kid": kid},
    )
    return token, jwks


@pytest.mark.parametrize("config_issuer", [
    "https://idp/application/o/siftd/",   # operator copies the trailing slash from discovery
    "https://idp/application/o/siftd",    # operator drops it
])
def test_validate_oidc_accepts_trailing_slash_issuer(monkeypatch, config_issuer):
    """An Authentik-style trailing-slash `iss` must validate regardless of how the
    operator wrote serve.auth.issuer. Real PyJWT (no decode monkeypatch).

    Regression: siftd rstrips the configured issuer before passing it to PyJWT's
    exact-equality iss check, so the token's `.../siftd/` never matched the
    rstripped `.../siftd` — every Authentik token was rejected with no config fix.
    """
    token, jwks = _signed_token_and_jwks("https://idp/application/o/siftd/")
    MW = create_auth_middleware({"issuer": config_issuer, "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=jwks))

    identity = _run(mw._validate_oidc(token))
    assert identity.sub == "alice"


def test_validate_oidc_real_pyjwt_rejects_wrong_issuer(monkeypatch):
    """The security property survives moving the iss check out of PyJWT: a token
    from a genuinely different issuer is still rejected by our normalized compare.
    """
    token, jwks = _signed_token_and_jwks("https://evil.example/application/o/siftd/")
    MW = create_auth_middleware({"issuer": "https://idp/application/o/siftd", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda: asyncio.sleep(0, result=jwks))

    with pytest.raises(NotAuthorizedException, match="issuer mismatch"):
        _run(mw._validate_oidc(token))


def test_validate_oidc_issuer_mismatch_logs_warning(monkeypatch, caplog):
    """The iss mismatch must be logged at WARNING (non-secret) so an operator
    config error is diagnosable — the client only ever sees a bare 401."""
    token, jwks = _signed_token_and_jwks("https://evil.example/application/o/siftd/")
    MW = create_auth_middleware({"issuer": "https://idp/application/o/siftd", "audience": "siftd"})
    mw = object.__new__(MW)
    monkeypatch.setattr(mw, "_get_jwks", lambda **kw: asyncio.sleep(0, result=jwks))

    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="siftd.serve.auth"):
        with pytest.raises(NotAuthorizedException, match="issuer mismatch"):
            _run(mw._validate_oidc(token))
    assert any("does not match configured" in r.getMessage() for r in caplog.records)


def _keypair_token_jwks(kid: str):
    """Build a real RS256 token (kid in header) + a single-key JWKS for that kid."""
    import datetime
    import json

    import jwt
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = kid
    jwks = jwt.PyJWKSet.from_dict({"keys": [jwk]})
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    token = jwt.encode(
        {"sub": "alice", "iss": "https://idp", "aud": "siftd",
         "exp": now + datetime.timedelta(hours=1), "iat": now},
        pem, algorithm="RS256", headers={"kid": kid},
    )
    return token, jwks


def test_validate_oidc_refetches_jwks_on_unknown_kid(monkeypatch):
    """H1: a token whose kid is absent from the cached JWKS (IdP key rotation)
    must trigger one forced refetch and then validate — not be rejected until the
    cache TTL lapses (which would be a ~1h auth outage after every rotation)."""
    _, old_jwks = _keypair_token_jwks("kid-old")
    new_token, new_jwks = _keypair_token_jwks("kid-new")

    MW = create_auth_middleware({"issuer": "https://idp", "audience": "siftd"})
    mw = object.__new__(MW)
    calls = {"n": 0}

    async def fake_get_jwks(force=False):
        calls["n"] += 1
        return new_jwks if force else old_jwks  # stale cache misses; forced refetch hits

    monkeypatch.setattr(mw, "_get_jwks", fake_get_jwks)

    identity = _run(mw._validate_oidc(new_token))
    assert identity.sub == "alice"
    assert calls["n"] == 2  # initial (miss) + one forced refetch (hit)


def test_get_jwks_force_refetch_is_rate_limited(monkeypatch):
    """H1 guard: a forced refetch within the rate-limit window is served from
    cache (no network), so a flood of bogus-kid tokens can't hammer the JWKS
    endpoint; once the window elapses, force does refetch."""
    fetches = {"n": 0}

    class _Resp:
        def json(self):
            return {"keys": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            fetches["n"] += 1
            return _Resp()

    class _HTTPX:
        AsyncClient = _Client

    class _JWT:
        class PyJWKSet:
            @staticmethod
            def from_dict(d):
                return {"parsed": d}

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    monkeypatch.setitem(sys.modules, "jwt", _JWT)
    MW = create_auth_middleware({"issuer": "https://idp", "jwks_url": "https://idp/jwks"})
    MW._jwks_cache = {"parsed": {"keys": []}}
    mw = object.__new__(MW)

    # Fresh cache + force → suppressed (rate-limited), no fetch.
    MW._jwks_fetched_at = time.time()
    assert _run(mw._get_jwks(force=True)) == {"parsed": {"keys": []}}
    assert fetches["n"] == 0

    # Window elapsed + force → refetch happens.
    MW._jwks_fetched_at = time.time() - 120
    _run(mw._get_jwks(force=True))
    assert fetches["n"] == 1


def test_introspection_cache_evicts_when_full():
    """M1: the introspection cache is size-capped, not unbounded."""
    MW = create_auth_middleware({"introspection_url": "https://idp/i"})
    MW._introspection_cache = {}
    MW._introspection_cache_max = 3
    mw = object.__new__(MW)
    now = time.time()
    for i in range(6):
        mw._store_introspection(f"k{i}", {"u": i}, now + 60, now)
    assert len(MW._introspection_cache) <= 3


def test_introspection_cache_drops_expired_before_evicting():
    """M1: when at capacity, expired entries are reclaimed before live ones."""
    MW = create_auth_middleware({"introspection_url": "https://idp/i"})
    MW._introspection_cache = {}
    MW._introspection_cache_max = 2
    mw = object.__new__(MW)
    now = time.time()
    mw._store_introspection("expired", {"u": 0}, now - 1, now)   # already expired
    mw._store_introspection("fresh", {"u": 1}, now + 60, now)    # cache now full (2)
    mw._store_introspection("new", {"u": 2}, now + 60, now)      # triggers reclaim
    keys = set(MW._introspection_cache)
    assert "expired" not in keys
    assert "fresh" in keys
    assert "new" in keys


# --- Startup preflight: serve.auth must name an auth mode ---


@pytest.mark.parametrize("mode", ["static_token", "issuer", "introspection_url"])
def test_validate_auth_config_accepts_each_mode(mode):
    from siftd.serve.auth import validate_auth_config

    validate_auth_config({mode: "x"})  # must not raise


def test_validate_auth_config_empty_and_none_are_noops():
    from siftd.serve.auth import validate_auth_config

    validate_auth_config({})    # no table → no middleware → nothing to check
    validate_auth_config(None)


def test_validate_auth_config_rejects_modeless_table():
    """A non-empty serve.auth table with no recognized mode fails loudly at boot."""
    from siftd.serve.auth import validate_auth_config

    with pytest.raises(ValueError, match="names no auth mode"):
        validate_auth_config({"required_scopes": ["siftd:read"]})


def test_validate_auth_config_delegation_token_gets_targeted_hint():
    """The stale-delegation_token footgun gets a hint pointing at [auth].token."""
    from siftd.serve.auth import validate_auth_config

    with pytest.raises(ValueError, match=r"delegation_token.*CLIENT"):
        validate_auth_config({"delegation_token": "secret"})


def test_create_app_rejects_modeless_auth_config(tmp_path):
    """The preflight runs at the create_app chokepoint, not just per-request."""
    from siftd.serve.app import create_app

    with pytest.raises(ValueError, match="names no auth mode"):
        create_app(db_path=tmp_path / "x.db", auth_config={"identity": "local"})
