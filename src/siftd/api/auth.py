"""Authentication helpers for sync remotes and serve."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
        if token_ref.startswith("env:"):
            env_var = token_ref[4:]
            value = os.environ.get(env_var)
            if not value:
                raise AuthError(f"environment variable not set: {env_var}")
            return value
        if token_ref.startswith("file:"):
            path = Path(token_ref[5:]).expanduser()
            if not path.exists():
                raise AuthError(f"token file not found: {path}")
            try:
                return path.read_text().strip()
            except OSError as e:
                raise AuthError(f"cannot read token file: {e.strerror}") from e
        return token_ref  # literal

    raise AuthError("no auth configured for remote")
