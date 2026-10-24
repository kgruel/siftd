"""Deprecated surface still accepted, each with its stated removal version.

The release rule: a breaking change ships behind this ledger, so every state
of `main` between deprecation and removal is a working surface. An entry
leaves when its removal version ships. CHANGELOG.md's "Deprecated" section
says the same thing to users.
"""

from __future__ import annotations

from dataclasses import dataclass

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


# --- version-1 adapter interface (#93) --------------------------------------
# One lifetime for everything a version-1 adapter needs: the interface itself
# (adapters/validation.py, LEGACY_VERSION), the record folds that give such an
# adapter peek (sdk.peek_scan_from_records, sdk.peek_exchanges_from_records and
# the derivation in adapters/contract.py, LegacyAdapter), and the SDK helpers
# only a legacy parse() builds its tree with (sdk.load_jsonl, sdk.ToolCallLinker,
# sdk.timestamp_bounds). They leave together with V1_INTERFACE_REMOVAL. The one
# built-in still on version 1 is gemini_cli, which has its own entry above.
V1_INTERFACE_REMOVAL = "1.0.0"


# --- `<root>::agent::<id>` sub-agent parent (#93) -----------------------------
# Schema v13 records a sub-agent's parent as a conversation_links edge, and its
# migration writes that edge for every row that named its parent only in the
# marker. The marker fallback (storage/links.py, subagent_parent_sql) is then
# redundant for any migrated database, and leaves with LEGACY_AGENT_EDGE_REMOVAL.
LEGACY_AGENT_EDGE_REMOVAL = "1.0.0"


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
