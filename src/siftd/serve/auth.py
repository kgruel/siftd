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


def create_auth_middleware(auth_config: dict) -> type[AbstractAuthenticationMiddleware]:
    """Create an auth middleware class bound to the given config.

    Uses a closure because AbstractAuthenticationMiddleware.__init__ doesn't
    accept custom kwargs.
    """

    class SiftdAuthMiddleware(AbstractAuthenticationMiddleware):
        """Bearer token authentication middleware."""

        _config = auth_config
        _jwks_cache: object | None = None
        _jwks_fetched_at: float = 0
        _introspection_cache: dict[str, tuple[dict, float]] = {}

        async def authenticate_request(
            self, connection: ASGIConnection,
        ) -> AuthenticationResult:
            # Check opt-out on route handler
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
            return UserIdentity(sub=self._config.get("identity", "local"))

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
                    sub=payload.get(identity_claim, payload.get("sub", "unknown"))
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
                    return UserIdentity(sub=cached.get(identity_claim, "unknown"))

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
            return UserIdentity(sub=body.get(identity_claim, "unknown"))

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
