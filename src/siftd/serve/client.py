"""Stdlib-only HTTP client for talking to a running siftd-serve.

This module intentionally has no dependency on the optional ``[serve]`` extra.
It is used by the CLI to opportunistically delegate expensive operations (e.g.
semantic search) to a warm, persistent siftd-serve process.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlparse


class ServeUnavailable(RuntimeError):
    """Raised when siftd-serve is unavailable or unhealthy."""


class ServeRequest4xx(Exception):
    """Raised when siftd-serve returns a 4xx response.

    Distinct from ServeUnavailable (network/5xx/health failures), which signals
    a legitimate local-fallback condition. A 4xx means the server received and
    rejected the request — the client must surface this rather than retrying locally.
    """

    def __init__(self, status: int, message: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {message}")
        self.status = status
        self.message = message
        self.url = url


@dataclass(frozen=True)
class ServeTarget:
    scheme: str
    host: str
    port: int
    path_prefix: str

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.path_prefix}"


def _parse_target(base_url: str) -> ServeTarget:
    parsed = urlparse(base_url)
    scheme = parsed.scheme or "http"
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported serve URL scheme: {scheme!r}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    path_prefix = parsed.path.rstrip("/")
    return ServeTarget(scheme=scheme, host=host, port=port, path_prefix=path_prefix)


def default_base_url() -> str:
    """Resolve base URL from env, then fall back to localhost default."""
    return os.environ.get("SIFTD_SERVE_URL") or "http://127.0.0.1:8484"


def _conn(target: ServeTarget, timeout_s: float):
    if target.scheme == "https":
        return HTTPSConnection(target.host, target.port, timeout=timeout_s)
    return HTTPConnection(target.host, target.port, timeout=timeout_s)


def _configured_issuer() -> str | None:
    """Return the client-side acquisition issuer ([auth].issuer), or None."""
    try:
        from siftd.config import get_config

        return get_config("auth.issuer") or None
    except Exception:
        return None


def _resolve_bearer_token() -> str | None:
    """Resolve a bearer token for serve delegation.

    Precedence:
    1) Env var: SIFTD_SERVE_TOKEN, then SIFTD_SERVE_DELEGATION_TOKEN
    2) Device-code credential acquired via `siftd auth login`, proactively
       refreshed when near expiry (only when [auth].issuer is configured)
    3) Config: serve.auth.delegation_token, then serve.auth.static_token
       (supports env:VAR syntax for both)
    """
    env = os.environ.get("SIFTD_SERVE_TOKEN") or os.environ.get("SIFTD_SERVE_DELEGATION_TOKEN")
    if env:
        return env

    issuer = _configured_issuer()
    if issuer:
        try:
            from siftd.credentials import resolve_live_bearer

            token = resolve_live_bearer(issuer)
            if token:
                return token
        except Exception:
            pass  # never let acquisition break the read path

    try:
        from siftd.config import get_config
    except Exception:
        return None

    cfg = get_config("serve.auth.delegation_token") or get_config("serve.auth.static_token")
    if not cfg:
        return None
    cfg = str(cfg)
    if cfg.startswith("env:"):
        return os.environ.get(cfg[4:], "") or None
    return cfg


def _send(target: ServeTarget, method: str, full_path: str,
          headers: dict[str, str], body: bytes | None, timeout_s: float) -> tuple[int, bytes]:
    """Issue one request, returning (status, raw_body). Connection always closed."""
    conn = _conn(target, timeout_s)
    try:
        if body is not None:
            conn.request(method, full_path, body=body, headers=headers)
        else:
            conn.request(method, full_path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _send_authed(target: ServeTarget, method: str, full_path: str, *,
                 base_headers: dict[str, str], body: bytes | None = None,
                 timeout_s: float) -> tuple[int, bytes]:
    """Send with a resolved bearer; on 401 with a stored device-code credential,
    refresh once and retry.

    The reactive retry is GATED on an issuer credential existing: static-token
    users (no [auth].issuer, or no stored credential — refresh_after_rejection
    returns None) hit no retry, so the strict 4xx-propagation behaviour is
    unchanged for them.
    """
    token = _resolve_bearer_token()
    headers = dict(base_headers)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    status, raw = _send(target, method, full_path, headers, body, timeout_s)

    if status == 401 and token:
        issuer = _configured_issuer()
        if issuer:
            try:
                from siftd.credentials import refresh_after_rejection

                new_token = refresh_after_rejection(issuer, token)
            except Exception:
                new_token = None
            if new_token and new_token != token:
                headers["Authorization"] = f"Bearer {new_token}"
                status, raw = _send(target, method, full_path, headers, body, timeout_s)
    return status, raw


def _raise_for_status(status: int, raw: bytes, target: ServeTarget, path: str,
                      *, ok: tuple[int, ...]) -> dict[str, Any]:
    """Shared response handling for the JSON helpers."""
    if 400 <= status <= 499:
        try:
            err_body = json.loads(raw.decode("utf-8"))
            msg = err_body.get("error") or str(status)
        except Exception:
            msg = str(status)
        raise ServeRequest4xx(status, msg, f"{target.base_url}{path}")
    if status not in ok:
        raise ServeUnavailable(f"HTTP {status} from {target.base_url}{path}")

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ServeUnavailable(f"Invalid JSON from {target.base_url}{path}: {e}") from e

    if not isinstance(body, dict):
        raise ServeUnavailable(f"Invalid JSON shape from {target.base_url}{path}: expected object")

    return body


def _get_json(
    base_url: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    target = _parse_target(base_url)
    query = urlencode(params or {}, doseq=True)
    full_path = f"{target.path_prefix}{path}"
    if query:
        full_path = f"{full_path}?{query}"

    status, raw = _send_authed(
        target, "GET", full_path,
        base_headers={"Accept": "application/json"}, timeout_s=timeout_s,
    )
    return _raise_for_status(status, raw, target, path, ok=(200,))


def _post_json(
    base_url: str,
    path: str,
    *,
    body: dict[str, Any],
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    target = _parse_target(base_url)
    full_path = f"{target.path_prefix}{path}"
    payload = json.dumps(body).encode("utf-8")

    status, raw = _send_authed(
        target, "POST", full_path, body=payload,
        base_headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout_s=timeout_s,
    )
    return _raise_for_status(status, raw, target, path, ok=(200, 201))


def probe_health(*, base_url: str, timeout_s: float = 0.02) -> dict[str, Any]:
    """Return health payload if siftd-serve is running, else raise ServeUnavailable."""
    body = _get_json(base_url, "/api/v1/health", timeout_s=timeout_s)
    if body.get("status") != "ok" or body.get("service") != "siftd":
        raise ServeUnavailable("unrecognized health payload")
    return body


def search(
    *,
    base_url: str,
    params: dict[str, Any],
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    """Call the serve search endpoint and return parsed JSON body."""
    return _get_json(base_url, "/api/v1/search", params=params, timeout_s=timeout_s)


def stats(
    *,
    base_url: str,
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    """Call the serve stats endpoint and return parsed JSON body."""
    return _get_json(base_url, "/api/v1/stats", timeout_s=timeout_s)
