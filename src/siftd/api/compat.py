"""Deprecated surface still accepted, each with its stated removal version.

The release rule: a breaking change ships behind this ledger, so every state
of `main` between deprecation and removal is a working surface. An entry
leaves when its removal version ships. CHANGELOG.md's "Deprecated" section
says the same thing to users.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

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
REBUILD_FTS_REMOVAL = "0.13.0"


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


# --- debug_ids (#94) ---------------------------------------------------------
# The `--debug-ids` CLI flag went in v0.10.0 as promised; its trail did not:
#   - `debug_ids=` on domain.search_types.SearchChunk.to_render_dict
#   - serve's `?debug_ids=` query parameter on search (accepted, ignored)
# Missed its v0.10.0 removal; restated here so a release can enforce it.
DEBUG_IDS_REMOVAL = "0.13.0"


# --- gemini_cli adapter (#93) ------------------------------------------------
# Gemini CLI became Antigravity CLI; `antigravity_cli` is its successor and reads
# the logs the tool writes now. The `gemini_cli` adapter stays loaded until
# GEMINI_CLI_REMOVAL, so conversations already ingested keep resolving and any
# chat file still on disk keeps ingesting. Its notice is named where a user meets
# the adapter (`siftd adapters`, the ingest summary) through DEPRECATED_ADAPTERS.
GEMINI_CLI_REMOVAL = "0.13.0"


@dataclass(frozen=True)
class Deprecation:
    """A deprecated adapter: the successor to name, and the version it leaves."""

    successor: str
    removed_in: str

    def message(self, adapter: str) -> str:
        return f"Adapter '{adapter}' is deprecated and is removed in {self.removed_in}"

    def hint(self) -> str:
        return f"Its successor is '{self.successor}'."


# An entry leaves with the `*_REMOVAL` constant it names, when that version ships.
DEPRECATED_ADAPTERS: dict[str, Deprecation] = {
    "gemini_cli": Deprecation(successor="antigravity_cli", removed_in=GEMINI_CLI_REMOVAL),
}


def adapter_deprecation(name: str) -> Deprecation | None:
    """The deprecation recorded for adapter ``name``, or None."""
    return DEPRECATED_ADAPTERS.get(name)
