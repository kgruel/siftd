"""Deprecated surface still accepted, each with its stated removal version.

The release rule: a breaking change ships behind this ledger, so every state
of `main` between deprecation and removal is a working surface. An entry
leaves when its removal version ships. CHANGELOG.md's "Deprecated" section
says the same thing to users.
"""

from __future__ import annotations

import warnings

# --- rebuild_fts (#74) -------------------------------------------------------
# No-ops since 0.12.2, when #49 made the merge index what it writes. Removed in
# REBUILD_FTS_REMOVAL:
#   - `rebuild_fts=` on api.merge.merge_database / api.receive.receive_database
#   - `siftd db merge --no-fts`
#   - the `serve.fts_rebuild` config key
#
# NOT removed, ever, without a protocol decision: `siftd db receive --no-fts`.
# api/sync.py::_push_ssh sends it to the remote, so dropping it breaks pushes
# from every client that predates the drop. It is accepted silently and hidden
# from --help; a warning would reach the client's error report on a failed push.
REBUILD_FTS_REMOVAL = "0.14.0"


def rebuild_fts_notice(subject: str) -> tuple[str, str]:
    """``(message, hint)`` for a caller still using one of the knobs above."""
    return (
        f"{subject} is deprecated and has no effect",
        f"Merged content is always indexed. It is removed in {REBUILD_FTS_REMOVAL}.",
    )


def warn_rebuild_fts_deprecated(func: str, value: bool | None) -> None:
    """Warn when a caller still passes ``rebuild_fts`` to ``func``."""
    if value is None:
        return
    message, hint = rebuild_fts_notice(f"{func}(rebuild_fts=...)")
    warnings.warn(f"{message}. {hint}", DeprecationWarning, stacklevel=3)
