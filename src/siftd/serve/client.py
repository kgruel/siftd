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


def _resolve_bearer_token() -> str | None:
    """Resolve a bearer token for serve delegation.

    Precedence:
    1) Env var: SIFTD_SERVE_TOKEN, then SIFTD_SERVE_DELEGATION_TOKEN
    2) Config: serve.auth.delegation_token, then serve.auth.static_token
       (supports env:VAR syntax for both)
    """
    env = os.environ.get("SIFTD_SERVE_TOKEN") or os.environ.get("SIFTD_SERVE_DELEGATION_TOKEN")
    if env:
        return env

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

    headers: dict[str, str] = {"Accept": "application/json"}
    token = _resolve_bearer_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    conn = _conn(target, timeout_s)
    try:
        conn.request("GET", full_path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()

    if 400 <= resp.status <= 499:
        try:
            err_body = json.loads(raw.decode("utf-8"))
            msg = err_body.get("error") or str(resp.status)
        except Exception:
            msg = str(resp.status)
        raise ServeRequest4xx(resp.status, msg, f"{target.base_url}{path}")
    if resp.status != 200:
        raise ServeUnavailable(f"HTTP {resp.status} from {target.base_url}{path}")

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ServeUnavailable(f"Invalid JSON from {target.base_url}{path}: {e}") from e

    if not isinstance(body, dict):
        raise ServeUnavailable(f"Invalid JSON shape from {target.base_url}{path}: expected object")

    return body


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

    headers: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    token = _resolve_bearer_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    conn = _conn(target, timeout_s)
    try:
        conn.request(
            "POST", full_path, body=payload,
            headers=headers,
        )
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()

    if 400 <= resp.status <= 499:
        try:
            err_body = json.loads(raw.decode("utf-8"))
            msg = err_body.get("error") or str(resp.status)
        except Exception:
            msg = str(resp.status)
        raise ServeRequest4xx(resp.status, msg, f"{target.base_url}{path}")
    if resp.status not in (200, 201):
        raise ServeUnavailable(f"HTTP {resp.status} from {target.base_url}{path}")

    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ServeUnavailable(f"Invalid JSON from {target.base_url}{path}: {e}") from e

    if not isinstance(result, dict):
        raise ServeUnavailable(f"Invalid JSON shape from {target.base_url}{path}: expected object")

    return result


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
