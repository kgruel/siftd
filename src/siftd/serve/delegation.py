"""Serve delegation policy — transparently delegate CLI commands to siftd-serve.

Extracted from cli_search.py. This module has no dependency on [serve] extras.
It uses serve/client.py for HTTP transport (stdlib-only).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


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


def _db_identities_match(health: dict[str, Any], local_db: Path) -> bool:
    """Compare the server's health-reported DB identity against the local DB.

    Used as a safety check on auto-discovered (loopback) delegation — when the
    user hasn't explicitly named a remote, we don't want to silently delegate
    to a sidecar pointed at a different DB. For explicit `serve.url`
    configuration this check is skipped (see :func:`try_delegate`).
    """
    served_db_id = health.get("db_id")
    if isinstance(served_db_id, str):
        import hashlib

        local_db_id = hashlib.sha256(str(local_db.resolve()).encode("utf-8")).hexdigest()
        return served_db_id == local_db_id
    # Backward compat: older servers returned db_path.
    served_db_path = health.get("db_path")
    if not isinstance(served_db_path, str):
        return False
    return served_db_path == str(local_db.resolve())


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

    from siftd.serve.client import ServeRequest4xx, ServeUnavailable, probe_health

    try:
        probe_timeout = 0.5 if explicit else 0.02
        health = probe_health(base_url=base_url, timeout_s=probe_timeout)
    except ServeRequest4xx:
        raise  # 4xx from /health is a real error (e.g. 401 auth failure)
    except (ServeUnavailable, Exception):
        return None

    # DB identity check: only applied for auto-discovered (loopback) delegation,
    # where we don't want to silently delegate to a sidecar pointed at a
    # different DB. For explicit `serve.url` configuration (the homelab
    # thin-client topology), the user has named a specific remote — its DB
    # path is *expected* to differ from the local DB path, so the SHA256
    # comparison would always fail and block delegation entirely.
    if not explicit and not _db_identities_match(health, db):
        return None

    from siftd.serve.client import _get_json

    try:
        return _get_json(base_url, endpoint, params=params, timeout_s=timeout_s)
    except ServeRequest4xx:
        raise  # propagate — caller must surface this, not fall back locally
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

    from siftd.serve.client import ServeRequest4xx, ServeUnavailable, probe_health

    try:
        probe_timeout = 0.5 if explicit else 0.02
        health = probe_health(base_url=base_url, timeout_s=probe_timeout)
    except ServeRequest4xx:
        raise  # 4xx from /health is a real error (e.g. 401 auth failure)
    except (ServeUnavailable, Exception):
        return None

    # See try_delegate's rationale for the explicit-bypass.
    if not explicit and not _db_identities_match(health, db):
        return None

    from siftd.serve.client import _post_json

    try:
        return _post_json(base_url, endpoint, body=body, timeout_s=timeout_s)
    except ServeRequest4xx:
        raise  # propagate — caller must surface this, not fall back locally
    except Exception:
        return None


# API fn kwarg → serve route query param.  Only non-identity mappings.
# After param alignment (unified CLI/HTTP/API names), only lambda_
# remains because `lambda` is a Python keyword.
_SERVE_PARAM_MAP: dict[str, str] = {
    "lambda_": "lambda",
}


def _expand_for_wire(params: dict[str, Any]) -> dict[str, Any]:
    """Translate non-scalar API params into wire-friendly query fields.

    Fidelity objects are opaque to urlencode (they'd be str()'d into garbage
    that serve routes ignore — historically leading to silent fidelity drift
    on delegated reads). Expand them into the `include_thinking` /
    `include_tool_content` boolean flags that serve routes accept.

    Also drops keys whose value is ``None``: ``urlencode`` would otherwise
    emit them as the literal string ``"None"``, which the route then parses
    as a real value (e.g. ``tool_filter=None`` would be treated as the
    pattern "None" by ``_matches_tool_filter`` and filter out every tool).
    The CLI's intent for ``None`` is "param omitted," not "param literally
    None." Litestar routes use their declared defaults when a key is absent.
    """
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if k == "fidelity" and hasattr(v, "shows"):
            # Expand Fidelity → wire axis fields. Drop the opaque object.
            out["include_thinking"] = bool(v.shows("thinking"))
            out["include_tool_content"] = bool(v.shows("tools"))
            continue
        out[k] = v
    return out


def _remap_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remap API fn kwargs to serve route query param names."""
    expanded = _expand_for_wire(params)
    return {_SERVE_PARAM_MAP.get(k, k): v for k, v in expanded.items()}


# Keys in op.params that exist for local execution but must NOT travel on the
# wire.
#
# - db_path / embed_db: local paths to SQLite files; the server uses its own
#   configured DB. Sending these leaks local filesystem state to the remote
#   and the server would ignore them anyway.
# - around: pure CLI post-processing annotation. The server has no use for it
#   today and the search route doesn't declare it; sending it would be
#   silently dropped by Litestar.
#
# Note: `mode` was previously excluded here because the search route had no
# matching Parameter — it derived mode from the `embeddings_only` bool instead.
# ST-4a added `mode` to the route, so `mode` now travels on the wire.
# ST-5 (wire-form-dissolution) will convert this frozenset to per-op declarations.
_WIRE_EXCLUDE = frozenset({"db_path", "embed_db", "around"})


def wire_query(op: Any) -> dict[str, Any]:
    """Return the query-params dict for HTTP delegation of this Operation.

    This is the wire form of ``op.params`` — the result of:

    1. Dropping local-only keys (``db_path``).
    2. Dropping ``None`` values (urlencode would emit them as the literal
       string "None", which Litestar would treat as a real value).
    3. Expanding non-scalar types (``Fidelity`` →
       ``include_thinking`` + ``include_tool_content``).
    4. Applying Python-keyword renames (``lambda_`` → ``lambda``).

    The output is ready for ``urlencode(..., doseq=True)`` and HTTP
    transport. See ``docs/guides/delegation-contract.md`` for the contract
    this implements and :func:`siftd.api.dispatch.local_kwargs` for the
    sibling that produces the local-execution form.
    """
    raw = {k: v for k, v in op.params.items() if k not in _WIRE_EXCLUDE}
    return _remap_params(raw)


# POST bodies use API conventions (tags, entity_id) — no remapping, no
# Fidelity expansion (POST routes in this codebase don't currently carry
# Fidelity). The function exists so future POST routes have a named entry
# point that can be extended without touching call sites.
def wire_body(op: Any) -> dict[str, Any]:
    """Return the JSON body dict for HTTP POST delegation of this Operation."""
    return {k: v for k, v in op.params.items() if k not in _WIRE_EXCLUDE}


def try_serve(op: Any) -> Any | None:
    """Try delegating an Operation to siftd-serve.

    Accepts an Operation (from api.dispatch) and delegates based on
    its path, method, params, and db. Returns the raw serve response
    on success, None on any failure.

    Raises:
        ServeRequest4xx: If the server returns a 4xx response. Callers must
            catch this and surface it — do not fall back to local execution.

    Uses :func:`wire_query` / :func:`wire_body` to produce the wire
    form from ``op.params``.
    """
    from siftd.serve.client import ServeRequest4xx

    try:
        if op.method == "GET":
            return try_delegate(op.path, wire_query(op), db=op.db)
        elif op.method == "POST":
            return try_delegate_post(op.path, wire_body(op), db=op.db)
    except ServeRequest4xx:
        raise  # propagate — callers must surface, not swallow
    except Exception as e:
        log.warning("try_serve unexpected error for %s %s: %s", op.method, op.path, e)
    return None


def print_serve_4xx(exc: Any) -> None:
    """Print a uniform server-4xx error message to stderr.

    Callers catch ServeRequest4xx and call this to produce a consistent
    error surface: the server URL, the HTTP status, and the server's own
    error message. The format is stable — the smoke probe asserts on it.
    """
    import sys

    print(f"siftd-serve returned HTTP {exc.status}: {exc.message} ({exc.url})", file=sys.stderr)
