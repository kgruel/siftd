Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Architecture

Core loop: **Ingest → Store → Query**

- **Adapters** own parsing and raw format knowledge. Storage is adapter-agnostic.
- **Storage** is normalized SQLite. Schema is fixed for core entities, extensible via `*_attributes` tables.
- **Queries** are user-defined `.sql` files with `$var` substitution. The system is a data platform, not a reporting tool.

## Design Principles

1. **Manual first, automate when patterns emerge** — labels are user-applied, enrichment is deferred, cost is approximate. Don't build automation until real usage reveals what's worth automating.
2. **Query-time computation over stored redundancy** — cost is derived via JOIN, not pre-computed. Avoids stale data and schema coupling.
3. **Attributes for variable metadata** — when the field set varies by provider or adapter, use key/value `*_attributes` tables instead of adding nullable columns.
4. **Adapters are the parsing boundary** — each adapter knows its raw format, dedup strategy, and provider source. Everything downstream is normalized.
5. **Approximate is fine when labeled** — approximate cost is useful. Don't over-engineer precision until billing context demands it.

## Conventions

- `commit=False` default on storage functions; caller controls transaction boundaries
- ULIDs for all primary keys
- XDG paths: data `~/.local/share/tbd`, config `~/.config/tbd`
- New CLI commands follow existing argparse patterns in `src/cli.py`
- New adapters implement `can_handle(source)`, `parse(source)`, `discover()`, set `HARNESS_SOURCE`
- Queries go in `~/.config/tbd/queries/*.sql`, use `$var` for parameters