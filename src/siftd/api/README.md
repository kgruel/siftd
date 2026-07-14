# siftd.api

<!-- TODO(preamble): authored in slice 3 -->
Public API layer — CLI and serve consume this, neither touches storage directly.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [_search_log_capture.py](_search_log_capture.py) | Shared search-log capture helpers used by api/search.py and api/conversations.py. |
| [adapters.py](adapters.py) | Adapter discovery API. |
| [auth.py](auth.py) | Authentication helpers for sync remotes and serve. |
| [backfill.py](backfill.py) | Backfill API wrappers. |
| [caveats.py](caveats.py) | Caveats — editorial annotations threaded from execute through render. |
| [conversations.py](conversations.py) | Conversation listing and detail API. |
| [database.py](database.py) | Database lifecycle API for siftd. |
| [deserialize.py](deserialize.py) | Deserializers — inverse of the wire-form serializers. |
| [dispatch.py](dispatch.py) | Operation dispatch — the IR between input contexts and output formats. |
| [doctor.py](doctor.py) | API for health checks and maintenance. |
| [events.py](events.py) | Event detail API. |
| [export.py](export.py) | Export API for siftd. |
| [file_refs.py](file_refs.py) | File reference queries for search results. |
| [inbox.py](inbox.py) | Sync inbox — stage received payloads for deferred merge. |
| [ingest.py](ingest.py) | Ingest API wrappers. |
| [merge.py](merge.py) | Merge an external SQLite database (slice) into the main siftd database. |
| [migrations.py](migrations.py) | Migration API wrappers for workspace identity maintenance. |
| [op_spec.py](op_spec.py) | Per-operation wire/local serialization rules. |
| [peek.py](peek.py) | API for live session inspection. |
| [receive.py](receive.py) | Receive a database file and create-or-merge into the target. |
| [resources.py](resources.py) | Resource copy API for adapters, queries, and formatters. |
| [search.py](search.py) | Search API extensions. |
| [serve_status.py](serve_status.py) | API helpers for serve status and audit logging. |
| [sessions.py](sessions.py) | Session management API for siftd. |
| [slice.py](slice.py) | Filtered database slice — export a subset of conversations into a standalone SQLite DB. |
| [stats.py](stats.py) | Database statistics API. |
| [sync.py](sync.py) | Sync local conversations with a remote siftd database. |
| [tags.py](tags.py) | Tag management API for siftd. |
| [target_ref.py](target_ref.py) | TargetRef — one grammar for "which thing gets the tag". |
<!-- gen:end -->
