# siftd.ingestion

This package coordinates the ingest pipeline; it does not parse formats and does not write rows. [discovery.py](discovery.py) fans out across every enabled adapter to find candidate source files, and [orchestration.py](orchestration.py) runs the discover → dedup → `parse()` → store sequence, delegating the actual format parsing to `adapters/` and the actual persistence to `storage/`. The layering matters: keep tool-specific logic in adapters and SQL in storage, so this folder stays a thin coordinator.

The invariant to protect is one source file → at most one conversation, enforced by a `UNIQUE` constraint on `ingested_files.path`. If an adapter's `parse()` yields multiple conversations from a single source, orchestration fails that source explicitly rather than silently dropping data — supporting multi-conversation sources would require a schema change and is deliberately deferred. Ingest is idempotent: files whose hash matches a prior run are skipped, so re-running is safe and cheap. Disabled adapters (via config) are excluded from discovery here, which is why the skip is visible at ingest time.

Two invariants govern the replace paths, which every dedup strategy has its own copy of. First, **a conversation replacement carries its tag assignments**: a changed transcript is replaced with delete-then-insert, and the `tr_polymorphic_*_cleanup` triggers take every assignment with the old ULIDs, so both strategies snapshot before the delete and re-point after the insert, in the same transaction (`_snapshot_tags_for_replacement` / `_restore_tags_after_replacement`, ratcheted by `tests/test_live_tagging.py::TestReplacementPreservesTagsPerDedupStrategy`). Second, **a queued tag is consumed only once it has been applied** — the drain resolves targets first and leaves the rest in `pending_tags` for the next ingest or for `siftd doctor fix --pending-tags`, because deleting a queued tag is data loss, not a repair. Both the drain and that doctor path resolve targets through `storage/sessions.py`'s `_resolve_pending_target`, so they cannot disagree about where a session tag lands.

See [Adapters — Adapter lifecycle](../../../docs/concepts/adapters.md#adapter-lifecycle) for the end-to-end ingest walkthrough.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [discovery.py](discovery.py) | Discovery: find sources across all adapters. |
| [orchestration.py](orchestration.py) | Orchestration: coordinate ingestion pipeline. |
<!-- gen:end -->
