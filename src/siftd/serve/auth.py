"""Authentication middleware for siftd serve.

Supports three modes:
- static_token: Compare against a configured secret (local dev/testing)
- OIDC: JWT validation against a configurable issuer's JWKS
- Introspection: RFC 7662 token introspection

When no auth_config is provided, middleware is not installed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult


@dataclass
class UserIdentity:
    """Authenticated user from token validation."""

    sub: str  # subject / identity string
    scopes: frozenset[str] = frozenset()


_write_scopes: frozenset[str] = frozenset()


def require_write(request) -> None:
    """Check that the authenticated user has write scopes. Raises 403 if not.

    Call from write route handlers. No-op when auth is not configured
    (user is anonymous) or no write_scopes are configured.
    """
    from litestar.exceptions import PermissionDeniedException

    user = getattr(request, "user", None)
    if user is None or user.sub == "anonymous":
        return  # No auth configured — allow all

    if not _write_scopes:
        return  # No write scopes configured — writes unrestricted

    if not user.scopes & _write_scopes:
        raise PermissionDeniedException("Insufficient scope for write operation")


def _parse_scope_string(scope_value: str | list | None) -> frozenset[str]:
    """Parse a scope value into a frozenset — handles space-delimited string or list."""
    if not scope_value:
        return frozenset()
    if isinstance(scope_value, list):
        return frozenset(scope_value)
    return frozenset(scope_value.split())


def create_auth_middleware(auth_config: dict) -> type[AbstractAuthenticationMiddleware]:
    """Create an auth middleware class bound to the given config.

    Uses a closure because AbstractAuthenticationMiddleware.__init__ doesn't
    accept custom kwargs.
    """
    global _write_scopes

    required = frozenset(auth_config.get("required_scopes", []))
    _write_scopes = frozenset(auth_config.get("write_scopes", []))

    class SiftdAuthMiddleware(AbstractAuthenticationMiddleware):
        """Bearer token authentication middleware."""

        _config = auth_config
        _jwks_cache: object | None = None
        _jwks_fetched_at: float = 0
        _introspection_cache: dict[str, tuple[dict, float]] = {}

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

            # Loopback API requests bypass auth — CLI delegation on same
            # machine has filesystem access to the DB anyway.
            if path.startswith("/api/"):
                client = connection.scope.get("client")
                if client:
                    addr = client[0] if isinstance(client, (list, tuple)) else getattr(client, "host", "")
                    if addr in ("127.0.0.1", "::1"):
                        return AuthenticationResult(user=UserIdentity(sub="local-cli"), auth=None)

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

            if not hmac.compare_digest(token, expected):
                raise NotAuthorizedException("Invalid token")
            # Static tokens get all configured scopes (full access for dev)
            return UserIdentity(
                sub=self._config.get("identity", "local"),
                scopes=required | _write_scopes,
            )

        async def _validate_oidc(self, token: str) -> UserIdentity:
            """Validate JWT against OIDC issuer's JWKS."""
            import jwt

            jwks = await self._get_jwks()
            identity_claim = self._config.get("identity_claim", "sub")
            audience = self._config.get("audience", "siftd")

            try:
                payload = jwt.decode(
                    token, jwks,
                    algorithms=["RS256", "ES256"],
                    audience=audience,
                )
                return UserIdentity(
                    sub=payload.get(identity_claim, payload.get("sub", "unknown")),
                    scopes=_parse_scope_string(payload.get("scope")),
                )
            except jwt.PyJWTError as e:
                raise NotAuthorizedException(f"Invalid token: {e}") from e

        async def _validate_introspection(self, token: str) -> UserIdentity:
            """Validate token via RFC 7662 introspection endpoint."""
            import httpx

            now = time.time()
            if token in SiftdAuthMiddleware._introspection_cache:
                cached, cached_at = SiftdAuthMiddleware._introspection_cache[token]
                if now - cached_at < 60:
                    identity_claim = self._config.get("identity_claim", "username")
                    return UserIdentity(
                        sub=cached.get(identity_claim, "unknown"),
                        scopes=_parse_scope_string(cached.get("scope")),
                    )

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

            SiftdAuthMiddleware._introspection_cache[token] = (body, now)
            identity_claim = self._config.get("identity_claim", "username")
            return UserIdentity(
                sub=body.get(identity_claim, "unknown"),
                scopes=_parse_scope_string(body.get("scope")),
            )

        async def _get_jwks(self):
            """Fetch and cache JWKS from OIDC issuer."""
            import httpx
            import jwt

            now = time.time()
            if SiftdAuthMiddleware._jwks_cache and now - SiftdAuthMiddleware._jwks_fetched_at < 3600:
                return SiftdAuthMiddleware._jwks_cache

            issuer = self._config["issuer"].rstrip("/")
            jwks_url = self._config.get("jwks_url")

            if not jwks_url:
                async with httpx.AsyncClient() as client:
                    disco = await client.get(f"{issuer}/.well-known/openid-configuration")
                    jwks_url = disco.json()["jwks_uri"]

            async with httpx.AsyncClient() as client:
                resp = await client.get(jwks_url)

            SiftdAuthMiddleware._jwks_cache = jwt.PyJWKSet.from_dict(resp.json())
            SiftdAuthMiddleware._jwks_fetched_at = now
            return SiftdAuthMiddleware._jwks_cache

    return SiftdAuthMiddleware
