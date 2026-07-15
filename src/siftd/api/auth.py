"""Authentication helpers for sync remotes and serve."""

from __future__ import annotations

import os
import subprocess

from siftd.errors import SiftdError


class AuthError(SiftdError):
    """Raised when token acquisition fails."""


def acquire_token(auth: dict | None) -> str:
    """Acquire a bearer token from auth config.

    Resolution order: token_command > token (env:/file:/literal).

    Raises:
        AuthError: If no auth is configured or token acquisition fails.
    """
    if not auth:
        raise AuthError("no auth configured for remote")

    if cmd := auth.get("token_command"):
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise AuthError(f"token command failed: {result.stderr.strip()}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired as e:
            raise AuthError(f"token command timed out: {cmd}") from e

    if token_ref := auth.get("token"):
        from siftd.credentials import TokenRefError, resolve_token_ref

        try:
            return resolve_token_ref(token_ref)
        except TokenRefError as e:
            raise AuthError(str(e)) from e

    raise AuthError("no auth configured for remote")


def configured_issuer() -> str | None:
    """Return the client-side acquisition issuer (``[auth].issuer``), or None."""
    try:
        from siftd.config import get_config

        return get_config("auth.issuer") or None
    except Exception:
        return None


def resolve_client_bearer() -> tuple[str | None, str | None]:
    """Resolve a bearer token and its *source* from the ``[auth]`` namespace.

    This is the single client-side token resolver shared by both serve
    delegation (reads) and sync push/pull (writes). It reads only ``[auth].*``
    — never ``serve.auth.*`` (the SERVER's validation config). Precedence, with
    the source tag returned alongside the token:

    1) Env var SIFTD_SERVE_TOKEN, then SIFTD_SERVE_DELEGATION_TOKEN  -> "env"
    2) Device-code credential (`siftd auth login`) via [auth].issuer,
       proactively refreshed when near expiry                       -> "device-code"
    3) Static [auth].token reference (env:/file:/literal)            -> "static"

    Returns ``(None, None)`` when nothing resolves. Only "device-code" is
    refreshable; the reactive 401 retry keys off this tag so a rejected
    env/static token is never swapped for an unrelated device credential.
    """
    env = os.environ.get("SIFTD_SERVE_TOKEN") or os.environ.get("SIFTD_SERVE_DELEGATION_TOKEN")
    if env:
        return env, "env"

    issuer = configured_issuer()
    if issuer:
        try:
            from siftd.credentials import resolve_live_bearer

            token = resolve_live_bearer(issuer)
            if token:
                return token, "device-code"
        except Exception:
            pass  # never let acquisition break the caller

    try:
        from siftd.config import get_config
    except Exception:
        return None, None

    ref = get_config("auth.token")
    if not ref:
        return None, None
    try:
        from siftd.credentials import resolve_token_ref

        return resolve_token_ref(str(ref)), "static"
    except Exception:
        return None, None  # unresolvable static ref → no token


def resolve_sync_bearer(remote_auth: dict | None) -> tuple[str | None, str | None]:
    """Resolve a bearer + source for a sync remote.

    A per-remote ``[sync.remotes.<name>.auth]`` block (token_command / token)
    takes precedence — source "remote" — so existing per-remote setups are
    unchanged. Otherwise this falls back to the shared ``[auth]`` resolver
    (env / device-code / static), so a single ``siftd auth login`` credential
    serves both reads and writes.
    """
    if remote_auth:
        try:
            return acquire_token(remote_auth), "remote"
        except AuthError:
            pass
    return resolve_client_bearer()


def refresh_bearer_after_401(token: str, source: str) -> str | None:
    """Gated reactive refresh after a 401. Only "device-code" tokens are
    refreshable; returns a new token (distinct from the rejected one) or None.

    ``token`` must be the raw bearer (no "Bearer " prefix).
    """
    if source != "device-code":
        return None
    issuer = configured_issuer()
    if not issuer:
        return None
    try:
        from siftd.credentials import refresh_after_rejection

        new = refresh_after_rejection(issuer, token)
    except Exception:
        return None
    return new if new and new != token else None
