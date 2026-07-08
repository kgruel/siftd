"""Shared search-log capture helpers used by api/search.py and api/conversations.py.

Issuer/session resolution (OJ-7, amended): issuer derives from session
registration rather than a bare env var. A live registered session for the
current workspace (the same state-dir file `siftd session-id` reads) means
issuer='agent'; absent that, issuer='cli'. `SIFTD_ISSUER` remains an explicit
override for harnesses that can't run the session-start hook. Serve routes
pass an explicit issuer='web', which wins over the cli/agent inference but
not over the env override (an unhooked serve deployment is not expected to
set SIFTD_ISSUER, so in practice 'web' always wins there).

See docs/dev/search-log-design-2026-07-07.md for the full design.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

_VALID_ISSUERS = ("cli", "agent", "web")


def resolve_session_id() -> str | None:
    """Best-effort session-id lookup for the current process, mirroring
    `siftd session-id`'s state-dir-file path (the DB fallback is skipped here —
    the file is written on every `siftd register`, which the session-start
    hook always runs first, so it is the path that matters for capture)."""
    try:
        from siftd.paths import session_id_file

        workspace_path = str(Path(os.getcwd()).resolve())
        sid_file = session_id_file(workspace_path)
        if sid_file.exists():
            sid = sid_file.read_text().strip()
            if sid:
                return sid
    except OSError:
        pass
    return None


def resolve_issuer_and_session(explicit_issuer: str | None) -> tuple[str, str | None]:
    """Resolve (issuer, session_id) for a search-log or open-signal capture call.

    Precedence: SIFTD_ISSUER env override > explicit_issuer (serve's 'web') >
    session-registration inference ('agent' if a live session is registered,
    else 'cli').
    """
    session_id = resolve_session_id()

    env_issuer = os.environ.get("SIFTD_ISSUER")
    if env_issuer in _VALID_ISSUERS:
        return env_issuer, session_id

    if explicit_issuer is not None:
        return explicit_issuer, session_id

    return ("agent" if session_id else "cli"), session_id
