"""Client-side OAuth token acquisition and storage for siftd.

This is the CLIENT side of the auth boundary: `siftd serve` only ever validates
an incoming bearer; every way a token comes into existence lives here. This
module owns the device-authorization grant (RFC 8628), the refresh grant
(RFC 6749 §6), at-rest credential storage keyed by issuer, and a single
``resolve_live_bearer(issuer)`` that proactively refreshes a near-expiry token.

Deliberately stdlib-only and light-import (``http.client``/``urllib``, lazy
``siftd.config``) so it can be consumed by ``serve/client.py`` without dragging
in the optional ``[serve]`` extra or the storage layer.

Security posture:
- Tokens are stored 0600 under a 0700 dir (``paths.atomic_write_secure``).
- Token *values* are never interpolated into log lines or exception messages.
- ``resolve_live_bearer`` never raises — a failure yields ``None`` (header
  omitted), preserving the delegation client's None-on-miss contract.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.parse import urlencode, urlparse

from siftd.errors import SiftdError

logger = logging.getLogger(__name__)

# Refresh proactively this many seconds before the token's true expiry, to
# cover clock skew and in-flight request latency.
_EXPIRY_SKEW_S = 120
# RFC 8628 device-grant polling error codes.
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class AuthLoginError(SiftdError):
    """Raised when interactive token acquisition (`siftd auth login`) fails.

    Only raised on the interactive paths (device_login / explicit refresh).
    resolve_live_bearer never raises — it returns None instead.

    Joins SiftdError directly rather than sharing a family base with
    api/auth.py's AuthError: credentials.py sits in the "utilities" layer
    (tests/architecture/test_imports.py) and cannot import the "api" layer,
    so ``AuthLoginError(AuthError)`` would be a layering violation. Each
    member of the auth trio joins the root independently instead.
    """


class TokenRefError(SiftdError):
    """Raised when an ``env:``/``file:`` token reference cannot be resolved.

    Callers adapt this to their own contract: the sync path wraps it in
    ``AuthError``; the delegation client swallows it and falls through to None.

    Joins SiftdError directly rather than AuthError — see AuthLoginError's
    docstring for why.
    """


def resolve_token_ref(ref: str) -> str:
    """Resolve a static bearer reference to its value.

    Grammar (shared with the sync-remote resolver in ``api/auth.py``):
    - ``env:VAR``   — read from the environment; ``TokenRefError`` if unset/empty.
    - ``file:PATH`` — read+strip the file (``~`` expanded); ``TokenRefError`` if
      missing or unreadable.
    - anything else — the literal token value.

    Lives here (light-import, stdlib-only) so both the delegation client and the
    sync resolver share one grammar that can't drift between them.
    """
    if ref.startswith("env:"):
        name = ref[4:]
        value = os.environ.get(name)
        if not value:
            raise TokenRefError(f"environment variable not set: {name}")
        return value
    if ref.startswith("file:"):
        path = Path(ref[5:]).expanduser()
        if not path.exists():
            raise TokenRefError(f"token file not found: {path}")
        try:
            return path.read_text().strip()
        except OSError as e:
            raise TokenRefError(f"cannot read token file: {e.strerror}") from e
    return ref


@dataclass(frozen=True)
class Credential:
    """A refreshable bearer credential acquired from an OIDC issuer.

    ``expires_at`` is an absolute POSIX timestamp (None when the issuer gives
    us nothing to schedule against — neither ``expires_in`` nor a JWT ``exp``).
    """

    issuer: str
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    token_type: str = "Bearer"
    scope: str = ""

    def is_stale(self, *, now: float | None = None, skew: int = _EXPIRY_SKEW_S) -> bool:
        """True when the token is within ``skew`` seconds of expiry.

        Unknown expiry (``expires_at is None``) is treated as NOT stale: we
        can't schedule a proactive refresh, so the reactive 401 backstop owns
        that case rather than refreshing on every read (the hot path).
        """
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) + skew >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "issuer": self.issuer,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Credential:
        return cls(
            issuer=str(data["issuer"]),
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token") or None,
            expires_at=(
                float(data["expires_at"]) if data.get("expires_at") is not None else None
            ),
            token_type=str(data.get("token_type") or "Bearer"),
            scope=str(data.get("scope") or ""),
        )


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def load(issuer: str) -> Credential | None:
    """Load the stored credential for ``issuer``, or None if absent/corrupt."""
    from siftd.paths import credential_file

    path = credential_file(issuer)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Credential.from_dict(data)
    except (OSError, ValueError, KeyError):
        logger.debug("stored credential unreadable for issuer; ignoring")
        return None


def save(cred: Credential) -> None:
    """Persist ``cred`` atomically at 0600 under a 0700 dir."""
    from siftd.paths import atomic_write_secure, credential_file

    atomic_write_secure(credential_file(cred.issuer), json.dumps(cred.to_dict()))


def delete(issuer: str) -> bool:
    """Delete the stored credential for ``issuer``. Returns True if one existed."""
    from siftd.paths import credential_file

    path = credential_file(issuer)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# --------------------------------------------------------------------------- #
# HTTP (stdlib only — matches serve/client.py)
# --------------------------------------------------------------------------- #

def _post_form(url: str, fields: dict[str, str], *, timeout: float = 30.0) -> tuple[int, dict]:
    """POST ``fields`` as application/x-www-form-urlencoded.

    Returns ``(status, json_body)``. Does NOT raise on 4xx — the device-grant
    poll signals ``authorization_pending``/``slow_down`` as HTTP 400 with a JSON
    ``error`` field, so the caller must branch on status, not on an exception.
    A non-JSON body yields ``{}``.
    """
    parsed = urlparse(url)
    body = urlencode(fields).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    conn = _conn(parsed, timeout)
    try:
        conn.request("POST", _path(parsed), body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    finally:
        conn.close()
    return status, _parse_json(raw)


def _get_discovery(url: str, *, timeout: float = 30.0) -> dict:
    """GET a JSON document (OIDC discovery). Raises AuthLoginError on failure."""
    parsed = urlparse(url)
    conn = _conn(parsed, timeout)
    try:
        conn.request("GET", _path(parsed), headers={"Accept": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise AuthLoginError(f"OIDC discovery failed: HTTP {resp.status}")
    finally:
        conn.close()
    body = _parse_json(raw)
    if not body:
        raise AuthLoginError("OIDC discovery returned a non-JSON document")
    return body


def _conn(parsed, timeout: float):
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        return HTTPSConnection(host, port, timeout=timeout)
    return HTTPConnection(host, port, timeout=timeout)


def _path(parsed) -> str:
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _parse_json(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------- #
# Endpoint resolution
# --------------------------------------------------------------------------- #

def _endpoints(issuer: str) -> dict[str, str]:
    """Resolve device-authorization + token endpoints for ``issuer``.

    Config overrides ([auth].device_authorization_endpoint / [auth].token_endpoint)
    take precedence; otherwise both are discovered from the issuer's
    .well-known/openid-configuration. Raises AuthLoginError if unresolvable.
    """
    from siftd.config import get_config

    device = get_config("auth.device_authorization_endpoint") or ""
    token = get_config("auth.token_endpoint") or ""
    if device and token:
        return {"device_authorization_endpoint": device, "token_endpoint": token}

    disco = _get_discovery(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
    device = device or str(disco.get("device_authorization_endpoint") or "")
    token = token or str(disco.get("token_endpoint") or "")
    if not device or not token:
        raise AuthLoginError(
            "issuer does not advertise device_authorization_endpoint/token_endpoint",
        )
    return {"device_authorization_endpoint": device, "token_endpoint": token}


def _client_id() -> str:
    from siftd.config import get_config

    cid = get_config("auth.client_id") or ""
    if not cid:
        raise AuthLoginError("no [auth].client_id configured for device-code login")
    return cid


def _scope() -> str:
    from siftd.config import get_config

    return get_config("auth.scope") or "openid offline_access"


# --------------------------------------------------------------------------- #
# Grants
# --------------------------------------------------------------------------- #

def _credential_from_token_response(issuer: str, body: dict, *, prior: Credential | None = None) -> Credential:
    """Build a Credential from a token-endpoint 200 body.

    expires_at is absolute: derived from ``expires_in`` when present, else from
    the access token's JWT ``exp`` claim, else None. A refresh response that
    omits ``refresh_token`` keeps the prior one (no rotation).
    """
    access_token = str(body.get("access_token") or "")
    if not access_token:
        raise AuthLoginError("token response missing access_token")

    expires_at: float | None = None
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = time.time() + float(expires_in)
    else:
        expires_at = _jwt_exp(access_token)

    refresh_token = body.get("refresh_token") or (prior.refresh_token if prior else None)
    return Credential(
        issuer=issuer,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        token_type=str(body.get("token_type") or "Bearer"),
        scope=str(body.get("scope") or (prior.scope if prior else "")),
    )


def _jwt_exp(token: str) -> float | None:
    """Read the ``exp`` claim from a JWT WITHOUT verifying it.

    Used only to schedule proactive refresh — never for trust decisions.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return float(exp)
    except (ValueError, TypeError):
        return None
    return None


def device_login(
    issuer: str,
    *,
    on_prompt=None,
    sleep=time.sleep,
    now=time.time,
) -> Credential:
    """Run the RFC 8628 device-authorization grant against ``issuer``.

    ``on_prompt(verification_uri, user_code, verification_uri_complete)`` is
    invoked once with the user-facing instructions; defaults to printing to
    stderr. ``sleep``/``now`` are injectable for tests. Returns and persists the
    acquired Credential. Raises AuthLoginError on denial/expiry/transport error.
    """
    endpoints = _endpoints(issuer)
    client_id = _client_id()

    status, body = _post_form(
        endpoints["device_authorization_endpoint"],
        {"client_id": client_id, "scope": _scope()},
    )
    if status != 200:
        raise AuthLoginError(f"device authorization request failed: HTTP {status}")

    device_code = str(body.get("device_code") or "")
    user_code = str(body.get("user_code") or "")
    verification_uri = str(body.get("verification_uri") or "")
    verification_uri_complete = body.get("verification_uri_complete")
    if not device_code or not user_code or not verification_uri:
        raise AuthLoginError("device authorization response was incomplete")

    interval = int(body.get("interval") or 5)
    expires_in = int(body.get("expires_in") or 600)
    deadline = now() + expires_in

    (on_prompt or _default_prompt)(verification_uri, user_code, verification_uri_complete)

    token_url = endpoints["token_endpoint"]
    while now() < deadline:
        sleep(interval)
        status, body = _post_form(
            token_url,
            {
                "grant_type": _DEVICE_GRANT,
                "device_code": device_code,
                "client_id": client_id,
            },
        )
        if status == 200:
            cred = _credential_from_token_response(issuer, body)
            save(cred)
            return cred
        error = str(body.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5  # RFC 8628 §3.5: permanent interval bump
            continue
        if error == "access_denied":
            raise AuthLoginError("authorization was denied")
        if error == "expired_token":
            raise AuthLoginError("device code expired before authorization")
        raise AuthLoginError(f"token polling failed: HTTP {status} ({error or 'unknown error'})")

    raise AuthLoginError("device code expired before authorization")


def _default_prompt(verification_uri: str, user_code: str, verification_uri_complete) -> None:
    import sys

    print("\nTo authorize siftd, visit:\n", file=sys.stderr)
    print(f"    {verification_uri}", file=sys.stderr)
    print(f"\nand enter code:  {user_code}\n", file=sys.stderr)
    if verification_uri_complete:
        print(f"(or open directly: {verification_uri_complete})\n", file=sys.stderr)
    print("Waiting for authorization...", file=sys.stderr)


def _do_refresh(cred: Credential) -> Credential:
    """Exchange ``cred``'s refresh token for a fresh credential. May raise."""
    if not cred.refresh_token:
        raise AuthLoginError("no refresh token available")
    token_url = _endpoints(cred.issuer)["token_endpoint"]
    status, body = _post_form(
        token_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
            "client_id": _client_id(),
        },
    )
    if status != 200:
        raise AuthLoginError(f"token refresh failed: HTTP {status}")
    return _credential_from_token_response(cred.issuer, body, prior=cred)


def refresh(issuer: str) -> Credential:
    """Explicitly refresh the stored credential for ``issuer`` (interactive path).

    Raises AuthLoginError if there is no stored credential or refresh fails.
    """
    cred = load(issuer)
    if cred is None:
        raise AuthLoginError("no stored credential to refresh; run `siftd auth login`")
    fresh = _do_refresh(cred)
    save(fresh)
    return fresh


# --------------------------------------------------------------------------- #
# Live resolution (consumed by serve/client.py — must NEVER raise)
# --------------------------------------------------------------------------- #

def resolve_live_bearer(issuer: str) -> str | None:
    """Return a fresh access token for ``issuer``, refreshing proactively if stale.

    Contract: NEVER raises, and returns None ONLY when there is no stored
    credential ("None means not logged in"). For a stale token, refresh is
    attempted; on any refresh failure we fall back to the EXISTING access token
    (best-effort: a stale token is usually still valid within the skew window,
    and the reactive 401 backstop owns the genuinely-expired case — so we'd
    rather send a possibly-valid token than guarantee a 401 by omitting it).

    Concurrency: refresh happens under an exclusive lock on a sibling ``.lock``
    file, with a re-read inside the critical section so that under refresh-token
    rotation a second racing process uses the winner's token rather than
    replaying a consumed refresh token.
    """
    cred = None
    try:
        cred = load(issuer)
        if cred is None:
            return None
        if not cred.is_stale():
            return cred.access_token
        refreshed = _locked_refresh(issuer, rejected_token=None)
        return refreshed or cred.access_token
    except Exception as e:  # never propagate into the read path
        logger.debug("proactive refresh degraded, using existing token: %s", type(e).__name__)
        return cred.access_token if cred is not None else None


def refresh_after_rejection(issuer: str, rejected_token: str) -> str | None:
    """Reactive backstop: a request with ``rejected_token`` got a 401.

    Under lock, re-read first — another process may already have rotated to a
    newer token, in which case use it without burning our refresh token.
    Otherwise refresh. NEVER raises; returns None on any failure.
    """
    try:
        return _locked_refresh(issuer, rejected_token=rejected_token)
    except Exception as e:
        logger.debug("reactive token refresh failed: %s", type(e).__name__)
        return None


def _locked_refresh(issuer: str, *, rejected_token: str | None) -> str | None:
    """Refresh under an exclusive sibling-file lock, with in-critical-section recheck.

    Returns the resulting access token, or None if there's nothing to refresh
    with. May raise on transport failure (callers wrap and swallow).
    """
    from siftd.paths import credential_file

    lock_path = credential_file(issuer).with_suffix(".lock")
    with _file_lock(lock_path):
        current = load(issuer)
        if current is None:
            return None
        # Someone else may have refreshed while we waited for the lock.
        if rejected_token is not None:
            # A different stored token means a concurrent winner rotated one in —
            # but only short-circuit if that winner is itself fresh. A stale
            # winner must fall through to a refresh rather than re-send a token
            # the server will also reject (the backstop's whole purpose).
            if current.access_token != rejected_token and not current.is_stale():
                return current.access_token
        elif not current.is_stale():
            return current.access_token
        if not current.refresh_token:
            return None
        fresh = _do_refresh(current)
        save(fresh)
        return fresh.access_token


class _file_lock:
    """Context manager: exclusive flock on a dedicated lockfile.

    Locks a stable sibling ``.lock`` file rather than the credential file
    itself, because ``os.replace`` swaps the credential's inode out from under
    any lock held on it. POSIX-only (fcntl); siftd's clients run on macOS/Linux.
    """

    def __init__(self, lock_path) -> None:
        self._lock_path = lock_path
        self._fd = None

    def __enter__(self):
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.parent.chmod(0o700)
        self._fd = open(self._lock_path, "w")  # noqa: SIM115 — released in __exit__
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        import fcntl

        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None


# Keep ``replace`` importable for tests constructing credential variants.
__all__ = [
    "AuthLoginError",
    "Credential",
    "TokenRefError",
    "device_login",
    "refresh",
    "refresh_after_rejection",
    "resolve_live_bearer",
    "resolve_token_ref",
    "load",
    "save",
    "delete",
    "replace",
]
