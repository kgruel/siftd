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


@dataclass(frozen=True)
class ServeTarget:
    scheme: str
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def _parse_target(base_url: str) -> ServeTarget:
    parsed = urlparse(base_url)
    scheme = parsed.scheme or "http"
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported serve URL scheme: {scheme!r}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    return ServeTarget(scheme=scheme, host=host, port=port)


def default_base_url() -> str:
    """Resolve base URL from env, then fall back to localhost default."""
    return os.environ.get("SIFTD_SERVE_URL") or "http://127.0.0.1:8484"


def _conn(target: ServeTarget, timeout_s: float):
    if target.scheme == "https":
        return HTTPSConnection(target.host, target.port, timeout=timeout_s)
    return HTTPConnection(target.host, target.port, timeout=timeout_s)


def _get_json(
    base_url: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    target = _parse_target(base_url)
    query = urlencode(params or {}, doseq=True)
    full_path = f"{path}?{query}" if query else path

    conn = _conn(target, timeout_s)
    try:
        conn.request("GET", full_path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()

    if resp.status != 200:
        raise ServeUnavailable(f"HTTP {resp.status} from {target.base_url}{path}")

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ServeUnavailable(f"Invalid JSON from {target.base_url}{path}: {e}") from e

    if not isinstance(body, dict):
        raise ServeUnavailable(f"Invalid JSON shape from {target.base_url}{path}: expected object")

    return body


def probe_health(*, base_url: str, timeout_s: float = 0.008) -> dict[str, Any]:
    """Return health payload if siftd-serve is running, else raise ServeUnavailable."""
    body = _get_json(base_url, "/v1/health", timeout_s=timeout_s)
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
    return _get_json(base_url, "/v1/search", params=params, timeout_s=timeout_s)

