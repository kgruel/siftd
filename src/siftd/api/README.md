# siftd.api

This package is the boundary every read and write surface passes through. The
CLI, the serve routes, the HTML routes, and any programmatic caller reach the
database only by calling functions here; none of them import `storage/`,
`search`, `peek`, `embeddings`, or `adapters` directly. That rule is enforced by
[`tests/architecture/test_hard_rules.py`](../../../tests/architecture/test_hard_rules.py)
(`test_cli_no_direct_storage_import`, `test_serve_no_direct_storage_import`,
`test_cli_and_serve_no_direct_search_import`) — a direct import fails the build
unless suppressed with an explicit `# arch: allow-storage` comment. Working
inside this folder means you sit on the storage side of that line and may import
freely; adding a new capability means giving the outer layers a function to call
rather than letting them descend.

The spine is the Operation IR in [`dispatch.py`](dispatch.py): each input context
(CLI args, an HTTP request, a direct call) normalizes into an `Operation`, which
the dispatch loop executes and renders — `normalize(input) → Operation → execute
→ render(format, fidelity)`. [`op_spec.py`](op_spec.py) holds the per-operation
rules that let the same `Operation` run locally or be forwarded over HTTP to a
running server with matching wire and local parameter shapes; see the
[delegation contract](../../../docs/guides/delegation-contract.md) for how that
parity is specified. `caveats.py` and `target_ref.py` are the shared grammars
threaded through many operations (editorial annotations, and "which thing gets
the tag" respectively).

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
