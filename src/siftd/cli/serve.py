"""CLI dispatcher for siftd serve."""

from __future__ import annotations

import sys

from siftd.output import status


def cmd_serve(args) -> int:
    """Start the HTTP team sync server."""
    from siftd.serve import require_serve

    try:
        require_serve()
    except ImportError as e:
        from siftd.cli.install import install_hint

        status.error(str(e), hint=f"Install with: {install_hint('serve')}")
        return 1

    from pathlib import Path

    from siftd.config import get_config, parse_size_bytes
    from siftd.serve.app import create_app

    # Resolve DB path: CLI arg > config > default
    db_path = getattr(args, "db", None)
    if db_path:
        db_path = Path(db_path)
    else:
        db_config = get_config("serve.db")
        if db_config:
            db_path = Path(str(db_config))
        else:
            from siftd.paths import db_path as default_db_path

            db_path = default_db_path()

    host = getattr(args, "host", None) or str(get_config("serve.host") or "127.0.0.1")
    port = int(getattr(args, "port", None) or get_config("serve.port") or 8484)
    fts_rebuild = str(get_config("serve.fts_rebuild") or "on_push")
    request_max_body_size = parse_size_bytes(
        str(get_config("serve.request_max_body_size") or "500MB")
    )

    # Rate limit (finding F4): per-client requests/minute. Default 600 (generous
    # for the htmx UI, restrictive for brute force). Set serve.rate_limit_per_minute
    # to 0 to disable.
    rl_cfg = get_config("serve.rate_limit_per_minute")
    try:
        rate_limit_per_minute = int(rl_cfg) if rl_cfg is not None else 600
    except (ValueError, TypeError):
        rate_limit_per_minute = 600

    # Auth config
    auth_config = None
    if not args.no_auth:
        from siftd.config import get_config_table

        auth_config = get_config_table("serve.auth")

    # Fail closed: refuse to bind a non-loopback address with auth disabled.
    # An unauthenticated server on a public interface exposes the entire
    # multi-user corpus for read AND write. Require an explicit opt-out so the
    # dangerous combination can never happen by misconfiguration (e.g. the
    # Docker image's `--host 0.0.0.0` with no [serve.auth] mounted). See
    # security finding F2.
    is_public = host not in ("127.0.0.1", "::1", "localhost")
    auth_off = args.no_auth or not auth_config
    if is_public and auth_off and not getattr(args, "unsafe_public_no_auth", False):
        status.error(
            f"refusing to bind public address {host!r} with authentication disabled",
            hint="configure [serve.auth] (or pass --unsafe-public-no-auth to override); "
            "an unauthenticated public server exposes the entire corpus for read and write.",
        )
        return 2

    # Ensure the team DB exists with a server-created schema before serving, so
    # the first push *merges* into it rather than adopting the uploaded SQLite
    # file wholesale (receive_database's _create_from_source path). See finding F9.
    if not db_path.exists():
        from siftd.api import create_database

        db_path.parent.mkdir(parents=True, exist_ok=True)
        create_database(db_path).close()

    # Live session endpoints (/peek, /follow) read the server host's session
    # files and bypass owner scoping (finding F7). Default off on a public bind;
    # serve.allow_live_endpoints overrides explicitly.
    live_cfg = get_config("serve.allow_live_endpoints")
    if live_cfg is None:
        allow_live_endpoints = not is_public
    else:
        allow_live_endpoints = str(live_cfg).strip().lower() in ("1", "true", "yes", "on")

    try:
        app = create_app(
            db_path=db_path,
            auth_config=auth_config,
            fts_rebuild=fts_rebuild,
            request_max_body_size=request_max_body_size,
            rate_limit_per_minute=rate_limit_per_minute,
            allow_live_endpoints=allow_live_endpoints,
        )
    except ValueError as e:
        status.error(f"invalid configuration — {e}")
        return 1

    import uvicorn

    print(f"siftd serve — listening on {host}:{port}", file=sys.stderr)
    print(f"  db: {db_path}", file=sys.stderr)
    if args.no_auth:
        auth_state = "disabled (--no-auth)"
    elif auth_config:
        auth_state = "enabled"
        if auth_config.get("issuer") and auth_config.get("browser_client_id"):
            auth_state += " (browser SSO: auth-code+PKCE)"
    else:
        auth_state = "disabled (no [serve.auth] config)"
    print(f"  auth: {auth_state}", file=sys.stderr)

    # Runtime discovery for CLI delegation: write serve state for `siftd search`.
    import json
    import os

    from siftd.paths import state_dir

    serve_state_file = state_dir() / "serve.json"
    serve_state_file.parent.mkdir(parents=True, exist_ok=True)
    serve_state_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "db_path": str(db_path.resolve()),
            }
        )
    )

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        serve_state_file.unlink(missing_ok=True)
    return 0


def build_serve_parser(subparsers) -> None:
    """Register the serve subcommand."""
    parser = subparsers.add_parser(
        "serve",
        help="Start the HTTP team sync server",
        description="Serve the siftd database over HTTP for team sync.",
    )
    parser.add_argument(
        "--host", metavar="ADDR",
        help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", metavar="PORT", type=int,
        help="Listen port (default: 8484)",
    )
    parser.add_argument(
        "--no-auth", action="store_true",
        help="Disable authentication (development only)",
    )
    parser.add_argument(
        "--unsafe-public-no-auth", action="store_true",
        help="Allow binding a non-loopback address with NO authentication. "
             "Dangerous: exposes the entire corpus for read and write. "
             "Without this flag, a public bind without [serve.auth] is refused.",
    )
    parser.set_defaults(func=cmd_serve)
