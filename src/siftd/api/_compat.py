"""Deprecated surface still accepted, each with its stated removal version.

The release rule: a breaking change ships behind this ledger, so every state
of `main` between deprecation and removal is a working surface. An entry
leaves when its removal version ships. CHANGELOG.md's "Deprecated" section
says the same thing to users.
"""

from __future__ import annotations

import warnings

# `rebuild_fts` on merge/receive, `db merge --no-fts`, `serve.fts_rebuild`:
# no-ops since #49 indexed merged content as part of the merge (#74).
REBUILD_FTS_REMOVAL = "0.14.0"


def warn_rebuild_fts_deprecated(func: str, value: bool | None) -> None:
    """Warn when a caller still passes ``rebuild_fts`` to ``func``."""
    if value is None:
        return
    warnings.warn(
        f"{func}(rebuild_fts=...) has no effect: merged content is always "
        f"indexed. It will be removed in {REBUILD_FTS_REMOVAL}.",
        DeprecationWarning,
        stacklevel=3,
    )
