# siftd.cli

The CLI is a thin dispatcher. [`__init__.py`](__init__.py) assembles the argparse
parser by calling one `build_<command>_parser()` per command, each of which
attaches its handler with `set_defaults(func=...)`; `main()` parses `argv` and
invokes `args.func(args)`. All command logic lives in the per-command modules
(`search.py`, `show.py`, `query.py`, …), not here — a new subcommand is a new
`build_*_parser` plus its handler module, wired into `__init__.py`. The
`_LANES` tuple defines the grouped ("six-lane") layout of the root `--help`.

Command handlers do not touch the database. They normalize flags into a call on
the [`api/`](../api/) layer and hand the result to a formatter from
[`output/`](../output/); importing `siftd.storage` or `siftd.search` directly is
rejected by the architecture tests
([`test_cli_no_direct_storage_import`, `test_cli_and_serve_no_direct_search_import`](../../../tests/architecture/test_hard_rules.py)).
Note that this argparse layer is where parse-time behavior (defaults, mutually
exclusive groups, type coercion) actually lives, so CLI tests should drive
`main()`/the parser rather than calling handlers with hand-built args.

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
