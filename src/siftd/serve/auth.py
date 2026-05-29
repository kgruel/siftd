"""Authentication middleware for siftd serve.

Supports three modes:
- static_token: Compare against a configured secret (local dev/testing)
- OIDC: JWT validation against a configurable issuer's JWKS
- Introspection: RFC 7662 token introspection

When no auth_config is provided, middleware is not installed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult

# JWKS cache lifetime, and the minimum interval between forced refetches (a
# forced refetch happens on an unknown-kid miss so IdP key rotation is picked up
# without waiting out the TTL — rate-limited so bogus-kid tokens can't hammer it).
_JWKS_CACHE_TTL_S = 3600
_JWKS_FORCE_REFETCH_MIN_S = 60


@dataclass
class UserIdentity:
    """Authenticated user from token validation."""

    sub: str  # subject / identity string
    scopes: frozenset[str] = frozenset()


_write_scopes: frozenset[str] = frozenset()


def require_write(request) -> None:
    """Check that the authenticated user has write scopes. Raises 403 if not.

    Call from write route handlers.

    Behavior:
    - If auth middleware is not installed (auth off): allow (no-op).
    - If auth middleware is installed: require an authenticated, non-anonymous user.
    - If write scopes are configured: require at least one write scope.
    """
    from litestar.exceptions import PermissionDeniedException

    try:
        user = request.user
    except Exception:
        return  # No auth middleware installed — allow all
    if user is None or getattr(user, "sub", None) in (None, "anonymous"):
        raise PermissionDeniedException("Authentication required for write operation")

    if not _write_scopes:
        return  # No write scopes configured — writes unrestricted

    scopes = getattr(user, "scopes", frozenset()) or frozenset()
    if not scopes & _write_scopes:
        raise PermissionDeniedException("Insufficient scope for write operation")


def _parse_scope_string(scope_value: str | list | None) -> frozenset[str]:
    """Parse a scope value into a frozenset — handles space-delimited string or list."""
    if not scope_value:
        return frozenset()
    if isinstance(scope_value, list):
        return frozenset(scope_value)
    return frozenset(scope_value.split())


def _jwks_origin_matches_issuer(jwks_uri: str, issuer: str) -> bool:
    """Return True when jwks_uri is on the same origin as the issuer.

    "Same origin" means scheme + host + port match exactly. We don't allow
    cross-origin JWKS even when both URIs share a parent domain: the issuer
    declares the trust boundary, and any redirection to a different host
    expands that boundary in a way an OIDC client cannot reason about.

    Returns False on any parsing failure — including malformed ports (which
    ``urlparse().port`` raises ``ValueError`` for on access, not at parse
    time) — so a hostile discovery document can't escape as a 500.
    """
    from urllib.parse import urlparse

    try:
        a = urlparse(jwks_uri)
        b = urlparse(issuer)
        if not a.scheme or not a.hostname:
            return False
        a_port = a.port or _default_port(a.scheme)
        b_port = b.port or _default_port(b.scheme)
    except (ValueError, Exception):
        return False
    return (
        a.scheme == b.scheme
        and a.hostname == b.hostname
        and a_port == b_port
    )


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _identity_from_introspection(body: dict, identity_claim: str) -> UserIdentity:
    """Build a UserIdentity from an introspection response.

    Mirrors the OIDC-path policy: reject if the configured identity claim is
    missing or empty, instead of collapsing distinct tokens under a synthetic
    "unknown" owner.
    """
    identity = body.get(identity_claim)
    if not isinstance(identity, str) or not identity:
        raise NotAuthorizedException(
            f"Introspection response missing identity claim: {identity_claim!r}",
        )
    return UserIdentity(
        sub=identity,
        scopes=_parse_scope_string(body.get("scope")),
    )


# The auth modes the server can validate. A non-empty serve.auth table must
# select exactly one of these; otherwise middleware installs but every request
# falls through to "No auth mode configured" (a fail-closed 401 on everything).
_AUTH_MODES = ("static_token", "issuer", "introspection_url")


def validate_auth_config(auth_config: dict | None) -> None:
    """Startup preflight for the serve.auth table.

    Raises ValueError if the table is non-empty but names no recognized auth
    mode — turning a per-request, opaque "No auth mode configured" 401 into a
    loud, single boot-time error. A common cause is a stale ``delegation_token``
    (now a client-side ``[auth]`` key), so that case gets a targeted hint.
    """
    if not auth_config:
        return  # empty/None → no middleware installed → nothing to validate
    if any(auth_config.get(m) for m in _AUTH_MODES):
        return
    hint = ""
    if auth_config.get("delegation_token"):
        hint = (
            " — `delegation_token` is a CLIENT key: set it under [auth].token, and "
            "for a shared secret set serve.auth.static_token to the same value"
        )
    raise ValueError(
        f"[serve.auth] is configured but names no auth mode "
        f"(one of: {', '.join(_AUTH_MODES)}){hint}",
    )


def create_auth_middleware(auth_config: dict) -> type[AbstractAuthenticationMiddleware]:
    """Create an auth middleware class bound to the given config.

    Uses a closure because AbstractAuthenticationMiddleware.__init__ doesn't
    accept custom kwargs.
    """
    global _write_scopes

    required = frozenset(auth_config.get("required_scopes", []))
    write = frozenset(auth_config.get("write_scopes", []))
    _write_scopes = write  # module-level for require_write()

    class SiftdAuthMiddleware(AbstractAuthenticationMiddleware):
        """Bearer token authentication middleware."""

        _config = auth_config
        _jwks_cache: object | None = None
        _jwks_fetched_at: float = 0
        # Keyed by sha256(token) (not the raw bearer) and size-capped, so a
        # long-running introspection server neither grows unbounded nor retains
        # plaintext tokens in memory.
        _introspection_cache: dict[str, tuple[dict, float]] = {}
        _introspection_cache_max: int = 1024

        async def authenticate_request(
            self, connection: ASGIConnection,
        ) -> AuthenticationResult:
            # Static assets and opt-out routes bypass auth
            path = connection.scope.get("path", "")
            if path.startswith("/static/"):
                return AuthenticationResult(user=UserIdentity(sub="anonymous"), auth=None)

            handler = connection.scope.get("route_handler")
            if handler and getattr(handler, "opt", {}).get("no_auth"):
                return AuthenticationResult(user=UserIdentity(sub="anonymous"), auth=None)

            auth_header = connection.headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                raise NotAuthorizedException("Missing bearer token")

            token = auth_header[7:]

            if "static_token" in self._config:
                identity = self._validate_static(token)
            elif "issuer" in self._config:
                identity = await self._validate_oidc(token)
            elif "introspection_url" in self._config:
                identity = await self._validate_introspection(token)
            else:
                raise NotAuthorizedException("No auth mode configured")

            # Check required scopes (applies to all modes)
            if required and not required <= identity.scopes:
                missing = required - identity.scopes
                raise NotAuthorizedException(f"Missing required scopes: {', '.join(sorted(missing))}")

            return AuthenticationResult(user=identity, auth=token)

        def _validate_static(self, token: str) -> UserIdentity:
            """Compare token against a configured static secret."""
            import hmac

            expected = self._config["static_token"]
            # Resolve env: prefix
            if expected.startswith("env:"):
                import os

                expected = os.environ.get(expected[4:], "")

            if not expected:
                raise NotAuthorizedException("Static token is not configured (empty or missing env var)")
            if not hmac.compare_digest(token, expected):
                raise NotAuthorizedException("Invalid token")
            # Static tokens get all configured scopes (full access for dev)
            return UserIdentity(
                sub=self._config.get("identity", "local"),
                scopes=required | write,
            )

        async def _validate_oidc(self, token: str) -> UserIdentity:
            """Validate JWT against OIDC issuer's JWKS."""
            import jwt

            jwks = await self._get_jwks()
            identity_claim = self._config.get("identity_claim", "sub")
            audience = self._config.get("audience", "siftd")
            issuer = self._config["issuer"].rstrip("/")

            try:
                header = jwt.get_unverified_header(token)
                kid = header.get("kid")
                signing_key = self._signing_key_for_kid(jwks, kid)
                if signing_key is None:
                    # kid absent from the cached JWKS — the IdP may have rotated
                    # signing keys. Force one (rate-limited) refetch before
                    # rejecting, mirroring PyJWKClient.get_signing_key's
                    # refresh-and-retry-once. Without this, a rotation rejects
                    # every fresh token until the TTL lapses (a ~1h auth outage).
                    jwks = await self._get_jwks(force=True)
                    signing_key = self._signing_key_for_kid(jwks, kid)
                if signing_key is None:
                    raise NotAuthorizedException("No matching JWKS key for token")
                try:
                    payload = jwt.decode(
                        token, signing_key.key,
                        algorithms=["RS256", "ES256"],
                        audience=audience,
                        # Validate `iss` ourselves below rather than via PyJWT's
                        # exact-string equality: some IdPs (e.g. Authentik) emit a
                        # trailing-slash issuer (`.../o/<slug>/`) while the configured
                        # issuer is rstripped for JWKS discovery. Compare normalized
                        # so a trailing slash on either side doesn't reject every token.
                        options={"require": ["exp", "iss", "aud"]},
                    )
                except TypeError as e:
                    logging.getLogger(__name__).debug("OIDC key/decode type error: %s", e)
                    raise NotAuthorizedException("Invalid token") from e
                token_iss = payload.get("iss")
                if not isinstance(token_iss, str) or token_iss.rstrip("/") != issuer:
                    # iss and the configured issuer are both non-secret. Log loudly:
                    # the client only sees a bare 401 and would otherwise retry-
                    # refresh in a loop, so a config mismatch is invisible without
                    # this. (The token's signature already validated above.)
                    logging.getLogger(__name__).warning(
                        "OIDC token iss %r does not match configured serve.auth.issuer %r",
                        token_iss, issuer,
                    )
                    raise NotAuthorizedException("Token issuer mismatch")
                # Reject if the configured identity claim is missing — collapsing
                # multiple tokens under a synthetic "unknown" owner would conflate
                # distinct subjects in conversation_owners.
                identity = payload.get(identity_claim)
                if not isinstance(identity, str) or not identity:
                    raise NotAuthorizedException(
                        f"Token missing required identity claim: {identity_claim!r}",
                    )
                return UserIdentity(
                    sub=identity,
                    scopes=_parse_scope_string(payload.get("scope")),
                )
            except jwt.PyJWTError as e:
                logging.getLogger(__name__).debug("OIDC token validation failed: %s", e)
                raise NotAuthorizedException("Invalid token") from e

        async def _validate_introspection(self, token: str) -> UserIdentity:
            """Validate token via RFC 7662 introspection endpoint."""
            import httpx

            now = time.time()
            cache_key = SiftdAuthMiddleware._cache_key(token)
            cached_entry = SiftdAuthMiddleware._introspection_cache.get(cache_key)
            if cached_entry is not None:
                cached, expires_at = cached_entry
                if now < expires_at:
                    identity_claim = self._config.get("identity_claim", "username")
                    return _identity_from_introspection(cached, identity_claim)

            url = self._config["introspection_url"]
            client_id = self._config.get("client_id", "")
            client_secret = self._config.get("client_secret", "")

            # Resolve env: prefix on client_secret
            if client_secret.startswith("env:"):
                import os

                client_secret = os.environ.get(client_secret[4:], "")

            kwargs: dict = {"data": {"token": token}}
            if client_id:
                kwargs["auth"] = (client_id, client_secret)

            async with httpx.AsyncClient() as client:
                resp = await client.post(url, **kwargs)

            if resp.status_code != 200:
                raise NotAuthorizedException("Introspection request failed")

            body = resp.json()
            if not body.get("active", False):
                raise NotAuthorizedException("Token is not active")

            # Bound cache TTL by token exp claim — never cache past expiry
            cache_deadline = now + 60
            token_exp = body.get("exp")
            if isinstance(token_exp, (int, float)) and token_exp > 0:
                cache_deadline = min(cache_deadline, float(token_exp))
            self._store_introspection(cache_key, body, cache_deadline, now)
            identity_claim = self._config.get("identity_claim", "username")
            return _identity_from_introspection(body, identity_claim)

        @staticmethod
        def _cache_key(token: str) -> str:
            """sha256 of the bearer — so plaintext tokens aren't kept as keys."""
            import hashlib

            return hashlib.sha256(token.encode()).hexdigest()

        @staticmethod
        def _store_introspection(key: str, body: dict, deadline: float, now: float) -> None:
            """Insert a cache entry, enforcing the size cap.

            When full and inserting a new key, drop expired entries first; if
            still at the cap, evict in insertion (roughly oldest-first) order.
            """
            cache = SiftdAuthMiddleware._introspection_cache
            cap = SiftdAuthMiddleware._introspection_cache_max
            if key not in cache and len(cache) >= cap:
                for expired in [k for k, (_, exp) in cache.items() if exp <= now]:
                    del cache[expired]
                while len(cache) >= cap:
                    cache.pop(next(iter(cache)))
            cache[key] = (body, deadline)

        @staticmethod
        def _signing_key_for_kid(jwks, kid):
            """Return the JWKS key matching ``kid``, or None (exact match only)."""
            return next((k for k in jwks.keys if k.key_id == kid), None)

        async def _get_jwks(self, *, force: bool = False):
            """Fetch and cache JWKS from OIDC issuer.

            ``force=True`` bypasses the TTL to pick up a freshly-rotated signing
            key on an unknown-kid miss, but is itself rate-limited
            (``_JWKS_FORCE_REFETCH_MIN_S``) so a flood of bogus-kid tokens can't
            hammer the JWKS endpoint.

            The discovered ``jwks_uri`` must live under the configured issuer's
            origin (scheme + host[:port]); otherwise a compromised or misconfigured
            issuer endpoint could redirect us to an attacker-controlled JWKS, and
            issuer-claim validation on the JWT wouldn't help because the attacker
            could mint tokens with the configured ``iss`` value.
            """
            import httpx
            import jwt

            now = time.time()
            if SiftdAuthMiddleware._jwks_cache is not None:
                age = now - SiftdAuthMiddleware._jwks_fetched_at
                if not force and age < _JWKS_CACHE_TTL_S:
                    return SiftdAuthMiddleware._jwks_cache
                if force and age < _JWKS_FORCE_REFETCH_MIN_S:
                    return SiftdAuthMiddleware._jwks_cache

            issuer = self._config["issuer"].rstrip("/")
            jwks_url = self._config.get("jwks_url")

            if not jwks_url:
                async with httpx.AsyncClient() as client:
                    disco = await client.get(f"{issuer}/.well-known/openid-configuration")
                    jwks_url = disco.json()["jwks_uri"]
                if not _jwks_origin_matches_issuer(jwks_url, issuer):
                    raise NotAuthorizedException(
                        "Discovered jwks_uri origin does not match issuer",
                    )

            async with httpx.AsyncClient() as client:
                resp = await client.get(jwks_url)

            SiftdAuthMiddleware._jwks_cache = jwt.PyJWKSet.from_dict(resp.json())
            SiftdAuthMiddleware._jwks_fetched_at = now
            return SiftdAuthMiddleware._jwks_cache

    return SiftdAuthMiddleware
