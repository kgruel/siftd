# CLI Reference

_Auto-generated from `--help` output._

## siftd

```
siftd 0.9.1 - Aggregate and query LLM conversation logs

usage: siftd [-h] [--version] [--db PATH] <command> ...

positional arguments:
  <command>
    query     List and filter conversations by metadata
    show      Read one conversation (or event) in detail
    report    Run saved SQL reports (parameterized .sql queries)
    search    Search conversations (auto-selects FTS5 or semantic based on
              what's installed)
    peek      Inspect live sessions from disk (bypasses SQLite)
    tag       Manage tags: apply, remove, list, rename, delete
    export    Export conversations as markdown or JSON
    ingest    Ingest logs from all sources
    doctor    Run health checks and maintenance
    config    View or modify config settings
    adapters  List discovered adapters
    db        Database operations (info, schema-version, backup, restore,
              vacuum, slice, merge, send, receive, remote, push, pull)
    serve     Start the HTTP team sync server
    auth      Acquire and manage a bearer token for a remote siftd serve
    install   Install optional dependencies or bundled components

options:
  -h, --help  show this help message and exit
  --version   show program's version number and exit
  --db PATH   Database path (default:
              /Users/kaygee/.local/share/siftd/siftd.db)

lanes:
  EXPLORE   query · search · show · report · peek
  CURATE    tag · export
  INGEST    ingest · adapters
  MAINTAIN  doctor · db
  SHARE     serve · auth
  SETUP     install · config

Run 'siftd <command> --help' for details.
Advanced (hidden): backfill, copy, id, migrate, register, session-id, upgrade
```
