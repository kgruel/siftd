"""CLI handlers for session-related commands."""

import argparse
import os
from pathlib import Path

from siftd.api import create_database, open_database
from siftd.api.sessions import find_active_session, register_session
from siftd.cli_common import resolve_db
from siftd.paths import ensure_dirs, session_id_file


def cmd_register(args) -> int:
    """Register an active session for live tagging."""
    db = resolve_db(args)
    ensure_dirs()

    # Create database if it doesn't exist
    conn = create_database(db)

    session_id = args.session
    adapter_name = args.adapter
    workspace_path = args.workspace or os.getcwd()

    # Resolve workspace to absolute path
    workspace_path = str(Path(workspace_path).resolve())

    # Register the session
    register_session(conn, session_id, adapter_name, workspace_path, commit=True)

    # Write session ID to XDG state dir
    sid_file = session_id_file(workspace_path)
    sid_file.parent.mkdir(parents=True, exist_ok=True)
    sid_file.write_text(session_id)

    conn.close()
    print(f"Registered session {session_id[:8]}... for {adapter_name}")
    return 0


def cmd_session_id(args) -> int:
    """Print the session ID for the current workspace."""
    workspace_path = args.workspace or os.getcwd()
    workspace_path = str(Path(workspace_path).resolve())

    sid_file = session_id_file(workspace_path)
    if sid_file.exists():
        session_id = sid_file.read_text().strip()
        if session_id:
            print(session_id)
            return 0

    # Fallback: query active_sessions table
    db = resolve_db(args)
    if db.exists():
        conn = open_database(db, read_only=True)
        try:
            session_id = find_active_session(conn, workspace_path)
            if session_id:
                print(session_id)
                return 0
        finally:
            conn.close()

    # Exit silently with non-zero for scripting
    return 1


def build_sessions_parser(subparsers) -> None:
    """Add 'register' and 'session-id' subparsers."""
    p_register = subparsers.add_parser(
        "register",
        help="Register an active session for live tagging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd register --session abc123 --adapter claude_code
  siftd register --session abc123 --adapter claude_code --workspace /path/to/project""",
    )
    p_register.add_argument("--session", "-s", required=True, metavar="ID", help="Harness session ID")
    p_register.add_argument("--adapter", "-a", required=True, metavar="NAME", help="Adapter name (e.g., claude_code)")
    p_register.add_argument("--workspace", "-w", metavar="PATH", help="Workspace path (default: current directory)")
    p_register.set_defaults(func=cmd_register)

    p_session_id = subparsers.add_parser(
        "session-id",
        help="Print the session ID for the current workspace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd session-id                    # print session ID for current directory
  siftd session-id --workspace /path  # print session ID for specific workspace

Exits with code 1 if no session ID found (for scripting).""",
    )
    p_session_id.add_argument("--workspace", "-w", metavar="PATH", help="Workspace path (default: current directory)")
    p_session_id.set_defaults(func=cmd_session_id)
