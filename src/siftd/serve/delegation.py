"""Serve delegation policy — transparently delegate CLI commands to siftd-serve.

Extracted from cli_search.py. This module has no dependency on [serve] extras.
It uses serve/client.py for HTTP transport (stdlib-only).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _parse_bool_like(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return None


def delegation_enabled() -> bool:
    """Check if serve delegation is enabled.

    Precedence: SIFTD_SERVE_DELEGATE env > serve.delegate config > default True.
    """
    env = _parse_bool_like(os.environ.get("SIFTD_SERVE_DELEGATE"))
    if env is not None:
        return env

    try:
        from siftd.config import get_config
    except Exception:
        return True

    cfg = _parse_bool_like(get_config("serve.delegate"))
    return True if cfg is None else cfg


def is_loopback_url(base_url: str) -> bool:
    """Check if a URL points to the loopback interface."""
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


def resolve_serve_url() -> tuple[str, bool]:
    """Resolve siftd-serve base URL.

    Returns (base_url, explicit) where explicit means it came from
    SIFTD_SERVE_URL or config ``serve.url`` (not the localhost default).
    """
    try:
        from siftd.config import get_config
    except Exception:
        get_config = None  # type: ignore[assignment]

    env_url = os.environ.get("SIFTD_SERVE_URL")
    if env_url:
        return env_url, True

    if get_config is not None:
        cfg_url = get_config("serve.url")
        if cfg_url:
            return cfg_url, True

    port = 8484
    port_from_config = False
    if get_config is not None:
        port_cfg = get_config("serve.port")
        if port_cfg:
            try:
                port = int(port_cfg)
                port_from_config = True
            except (ValueError, TypeError):
                pass

    # Runtime fallback: only consult the state file when serve.port is NOT
    # configured, so config remains authoritative over stale/other state files.
    if not port_from_config:
        import json

        from siftd.paths import state_dir

        serve_state = state_dir() / "serve.json"
        try:
            data = json.loads(serve_state.read_text())
            pid = data.get("pid")
            if isinstance(pid, int):
                os.kill(pid, 0)  # raises OSError if process doesn't exist
                state_port = data.get("port")
                if isinstance(state_port, int):
                    port = state_port
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass

    return f"http://127.0.0.1:{port}", False


def can_delegate(*, db: Path) -> bool:
    """Check if delegation preconditions are met.

    Guards: delegation enabled, loopback-only for auto-discovered URLs.
    """
    if not delegation_enabled():
        return False

    base_url, explicit = resolve_serve_url()

    # Only auto-delegate to loopback to keep the cold-path probe bounded.
    if not explicit and not is_loopback_url(base_url):
        return False

    return True


def try_delegate(
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    db: Path,
    timeout_s: float = 1.0,
) -> dict[str, Any] | None:
    """Attempt to delegate a GET request to siftd-serve.

    Returns parsed JSON dict on success, None on any failure.
    The caller falls back to local computation on None.
    """
    if not can_delegate(db=db):
        return None

    base_url, explicit = resolve_serve_url()

    from siftd.serve.client import ServeUnavailable, probe_health

    try:
        probe_timeout = 0.5 if explicit else 0.02
        health = probe_health(base_url=base_url, timeout_s=probe_timeout)
    except (ServeUnavailable, Exception):
        return None

    # Verify DB path match
    served_db_path = health.get("db_path")
    if not isinstance(served_db_path, str):
        return None
    if served_db_path != str(db.resolve()):
        return None

    from siftd.serve.client import _get_json

    try:
        return _get_json(base_url, endpoint, params=params, timeout_s=timeout_s)
    except Exception:
        return None


def try_delegate_post(
    endpoint: str,
    body: dict[str, Any],
    *,
    db: Path,
    timeout_s: float = 1.0,
) -> dict[str, Any] | None:
    """Attempt to delegate a POST request to siftd-serve.

    Returns parsed JSON dict on success, None on any failure.
    """
    if not can_delegate(db=db):
        return None

    base_url, explicit = resolve_serve_url()

    from siftd.serve.client import ServeUnavailable, probe_health

    try:
        probe_timeout = 0.5 if explicit else 0.02
        health = probe_health(base_url=base_url, timeout_s=probe_timeout)
    except (ServeUnavailable, Exception):
        return None

    served_db_path = health.get("db_path")
    if not isinstance(served_db_path, str):
        return None
    if served_db_path != str(db.resolve()):
        return None

    from siftd.serve.client import _post_json

    try:
        return _post_json(base_url, endpoint, body=body, timeout_s=timeout_s)
    except Exception:
        return None


# API fn kwarg → serve route query param.  Only non-identity mappings.
# After param alignment (unified CLI/HTTP/API names), only lambda_
# remains because `lambda` is a Python keyword.
_SERVE_PARAM_MAP: dict[str, str] = {
    "lambda_": "lambda",
}


def _remap_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remap API fn kwargs to serve route query param names."""
    return {_SERVE_PARAM_MAP.get(k, k): v for k, v in params.items()}


def try_serve(op: Any) -> Any | None:
    """Try delegating an Operation to siftd-serve.

    Accepts an Operation (from api.dispatch) and delegates based on
    its path, method, params, and db. Returns the raw serve response
    on success, None on any failure.

    Params are remapped from API fn kwargs to HTTP conventions
    (e.g. limit→n, tags→tag) via _SERVE_PARAM_MAP.
    """
    try:
        raw = {k: v for k, v in op.params.items() if k != "db_path"}

        if op.method == "GET":
            # GET query params use HTTP conventions (n, tag, id)
            return try_delegate(op.path, _remap_params(raw), db=op.db)
        elif op.method == "POST":
            # POST bodies use API conventions (tags, entity_id) — no remapping
            return try_delegate_post(op.path, raw, db=op.db)
    except Exception:
        pass
    return None
