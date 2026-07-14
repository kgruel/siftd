# siftd.cli

<!-- TODO(preamble): authored in slice 3 -->
CLI package — thin dispatcher plus per-command modules.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [_common.py](_common.py) | Shared CLI utilities. |
| [_filters.py](_filters.py) | Shared CLI filter arguments for conversation commands. |
| [auth.py](auth.py) | CLI for client-side token acquisition: `siftd auth login/status/logout`. |
| [data.py](data.py) | CLI handlers for data operations (ingest, backfill, migrate, doctor, copy). |
| [db.py](db.py) | CLI handlers for 'siftd db' namespace — container-level operations. |
| [embed.py](embed.py) | CLI handler for 'siftd embed' — build and inspect the semantic-search index. |
| [export.py](export.py) | CLI handler for export command (export conversations as markdown or JSON). |
| [id_cmd.py](id_cmd.py) | CLI handler for the 'id' command - classify and display ULID information. |
| [install.py](install.py) | CLI handler for 'siftd install' — install optional extras and bundled components. |
| [meta.py](meta.py) | CLI handlers for meta commands (config, adapters) and db-delegated functions. |
| [peek.py](peek.py) | CLI handler for peek command (inspect live sessions from disk). |
| [query.py](query.py) | CLI handlers for query commands (query). |
| [report.py](report.py) | CLI handler for the `report` command — run saved parameterized SQL queries. |
| [search.py](search.py) | CLI handler for 'siftd search' — unified search over conversations. |
| [serve.py](serve.py) | CLI dispatcher for siftd serve. |
| [sessions.py](sessions.py) | CLI handlers for session-related commands. |
| [show.py](show.py) | CLI handler for `show` — read one conversation (or event) in detail. |
| [tags.py](tags.py) | CLI handlers for tag command (apply, remove, list, rename, delete). |
| [upgrade.py](upgrade.py) | CLI handler for 'siftd upgrade' — check for and install updates. |
<!-- gen:end -->
