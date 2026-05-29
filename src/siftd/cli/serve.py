"""CLI dispatcher for siftd serve."""

from __future__ import annotations

import sys


def cmd_serve(args) -> int:
    """Start the HTTP team sync server."""
    from siftd.serve import require_serve

    try:
        require_serve()
    except ImportError as e:
        from siftd.cli.install import install_hint

        print(f"{e} Install with: {install_hint('serve')}", file=sys.stderr)
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

    # Auth config
    auth_config = None
    if not args.no_auth:
        from siftd.config import get_config_table

        auth_config = get_config_table("serve.auth")

    try:
        app = create_app(
            db_path=db_path,
            auth_config=auth_config,
            fts_rebuild=fts_rebuild,
            request_max_body_size=request_max_body_size,
        )
    except ValueError as e:
        print(f"siftd serve: invalid configuration — {e}", file=sys.stderr)
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
    parser.set_defaults(func=cmd_serve)
