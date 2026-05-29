"""Authentication helpers for sync remotes and serve."""

from __future__ import annotations

import subprocess


class AuthError(Exception):
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
